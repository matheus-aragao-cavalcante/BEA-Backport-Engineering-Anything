"""The LayeredFS packager: a build laid out the way a console or a mod loader expects.

Two layouts are supported (``<ID>`` is the title ID, ``<SET>`` the patch-set name):

====================== ========================================= ====================
logical path           ``atmosphere`` layout                     ``flat`` layout
====================== ========================================= ====================
``romfs/<path>``       ``atmosphere/contents/<ID>/romfs/<path>`` ``<SET>/romfs/<path>``
``exefs/<name>``       ``atmosphere/contents/<ID>/exefs/<name>`` ``<SET>/exefs/<name>``
``exefs_patches/<f>``  ``atmosphere/exefs_patches/<SET>/<f>``    ``<SET>/exefs/<f>``
====================== ========================================= ====================

With ``atmosphere`` the ``atmosphere`` folder is copied to the root of the SD card; ``flat``
is a self-contained mod folder, the layout common mod loaders use.

Rules
-----
* **Content is never changed.** A file is hard-linked into the package when the filesystem
  allows it and copied otherwise.
* **The package folder is shared.** It may hold other mods, or be the SD card itself, so
  BEA only ever deletes what it created. What it created is recorded in a manifest
  (:data:`MANIFEST_NAME`) at the root of the folder; no file outside that list is modified
  or removed, and only directories that BEA's own deletions left empty are removed.
* **The manifest never lags behind the disk.** Before the first change it is written with
  the files of the previous run *and* the new ones; once everything is in place it is
  written again with the new ones only. A run that dies half-way therefore leaves no file
  that a later run could not account for.
* **A manifest found on disk is untrusted input.** One that cannot be read deletes
  nothing. Entries that are not clean relative paths, that name a directory, or that are
  reached through a link are ignored, each with a warning.
* **Stale files go first.** Files of the previous run that left the package are deleted
  before new ones are written. That is what lets a path change its letter case on a
  case-insensitive disk or turn from a file into a directory, and it frees space early.
* **No ambiguous package.** SD cards are formatted as FAT/exFAT, where letter case does
  not count: two destinations that differ only by case are refused, not merged.
* **Repeating a run changes nothing**, not even the manifest's modification time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from bea import fsutil
from bea.errors import OutputError
from bea.model import split_logical
from bea.packagers.base import PackageContext, Packager, PackageResult, StagedFile

__all__ = ["MANIFEST_NAME", "LayeredFSPackager", "Layout"]

#: Name of the file, at the root of the package folder, that lists what BEA created there.
MANIFEST_NAME = ".bea-package.json"

_MANIFEST_SCHEMA = 1
_TITLE_ID = re.compile(r"[0-9A-F]{16}")
_PATCHSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: How many paths a message quotes before it only counts the rest.
_SAMPLE_SIZE = 5
_QUOTE_LIMIT = 200


class Layout(StrEnum):
    """Folder structure of the package."""

    #: ``atmosphere/contents/<ID>/...``: copy the ``atmosphere`` folder to the SD card root.
    ATMOSPHERE = "atmosphere"
    #: ``<SET>/romfs`` and ``<SET>/exefs``: a self-contained mod folder for mod loaders.
    FLAT = "flat"


#: Directory of each logical tree, per layout (``str.format`` templates).
_TREE_ROOTS: Mapping[Layout, Mapping[str, str]] = {
    Layout.ATMOSPHERE: {
        "romfs": "atmosphere/contents/{title_id}/romfs",
        "exefs": "atmosphere/contents/{title_id}/exefs",
        "exefs_patches": "atmosphere/exefs_patches/{patchset}",
    },
    Layout.FLAT: {
        "romfs": "{patchset}/romfs",
        "exefs": "{patchset}/exefs",
        # A mod folder has no separate place for patches: they sit next to the executables.
        "exefs_patches": "{patchset}/exefs",
    },
}


@dataclass(frozen=True, slots=True)
class _PreviousRun:
    """What the manifest of the previous run says (nothing at all when there is none)."""

    #: Content of the manifest file as found; ``None`` when it is missing or unreadable.
    raw: bytes | None = None
    #: Files that run created: package-relative POSIX paths, already validated.
    files: frozenset[str] = frozenset()
    #: Title the previous package was made for, when the manifest says so.
    title_id: str | None = None


class LayeredFSPackager(Packager):
    """Lays the staged files out as a LayeredFS mod folder (see the module docstring)."""

    name: ClassVar[str] = "layeredfs"

    def __init__(self, layout: Layout | str = Layout.ATMOSPHERE) -> None:
        """Create a packager for ``layout``.

        Raises:
            ValueError: ``layout`` is not one of the :class:`Layout` values.
        """
        try:
            self.layout = Layout(layout)
        except ValueError:
            valid = ", ".join(repr(member.value) for member in Layout)
            raise ValueError(f"unknown layout {layout!r}: expected one of {valid}") from None

    def destination(self, logical: str, context: PackageContext) -> str:
        """Return the package-relative POSIX path where ``logical`` belongs.

        Raises:
            ValueError: ``logical`` does not start with a known tree; its path is not a
                clean relative path (the rules of :func:`bea.fsutil.resolve_relative`);
                the title ID of ``context`` is not 16 uppercase hex digits; or its
                patch-set name is not a safe directory name.
        """
        _check_context(context)
        tree, relative = split_logical(logical)
        template = _TREE_ROOTS[self.layout].get(tree)
        if template is None:
            raise ValueError(f"the {self.name} packager has no place for {logical!r}")
        if not _is_clean_relative(relative):
            raise ValueError(
                f"{logical!r} is not a safe logical path: after the tree name it must be a "
                "relative path with '/' separators and no empty, '.' or '..' segment"
            )
        root = template.format(title_id=context.title_id, patchset=context.patchset_name)
        return f"{root}/{relative}"

    def package(
        self, staged: Sequence[StagedFile], out_dir: Path, context: PackageContext
    ) -> PackageResult:
        """Write the package for ``staged`` into ``out_dir``.

        Running it again with the same input changes nothing. Files that a previous run
        put in ``out_dir`` and that are no longer staged are removed, together with the
        directories this leaves empty; anything BEA did not create is never touched. An
        empty ``staged`` is valid: it removes the previous package.

        Nothing is touched (``out_dir`` is not even created) when the input itself is
        refused: an invalid path or name, a collision, a missing staged file.

        Raises:
            ValueError: a logical path or ``context`` is invalid (see :meth:`destination`).
            OutputError: two staged files would end up at the same place (same
                destination, destinations that differ only by letter case, or a file
                where another one needs a directory); a staged file cannot be read;
                ``out_dir`` or the manifest name is taken by something else; or a file
                cannot be written or removed.
        """
        _check_context(context)
        planned = self._plan(staged, context)
        sources = {destination: _stat_source(item) for destination, item in planned.items()}
        root = Path(out_dir)
        _prepare_root(root)

        warnings: list[str] = []
        manifest = fsutil.resolve_relative(root, MANIFEST_NAME)
        previous = _read_manifest(manifest, self.name, warnings)
        new_files = sorted(planned)
        stale = previous.files.difference(planned)
        if stale and previous.title_id not in (None, context.title_id):
            warnings.append(
                f"{root} held the package of another title ({_quote(previous.title_id)}); "
                f"it was replaced by the package of {context.title_id}, because an output "
                "folder holds one package at a time"
            )

        # Crash safety: from here until the end, every file BEA has ever created in this
        # folder is listed in the manifest, whatever happens in between.
        on_disk = self._store_manifest(
            manifest, context, sorted(previous.files.union(planned)), previous.raw
        )
        result = PackageResult(root=root, files=new_files, warnings=warnings)
        # Stale files go before new ones are written: on a case-insensitive disk a file
        # whose name only changed case *is* the stale one, and a file may have to make
        # room for a directory of the same name (or the other way round).
        result.removed = _remove_stale(root, stale, warnings)
        for destination in new_files:
            target = fsutil.resolve_relative(root, destination)
            try:
                existing = os.stat(fsutil.fs_path(target))
            except OSError:
                existing = None
            if existing is not None:
                if stat.S_ISDIR(existing.st_mode):
                    raise OutputError(
                        f"cannot write {target}: a directory with that name is in the way, "
                        "and BEA only removes directories that it emptied itself; "
                        "move it away and run again"
                    )
                if _is_in_place(sources[destination], existing):
                    result.unchanged += 1
                    continue
            try:
                how = fsutil.link_or_copy(planned[destination].path, target)
            except OSError as exc:
                raise OutputError(f"cannot write {target}: {_reason(exc)}") from exc
            if how == "link":
                result.linked += 1
            else:
                result.copied += 1
        self._store_manifest(manifest, context, new_files, on_disk)
        return result

    # ------------------------------------------------------------------ internals

    def _plan(self, staged: Sequence[StagedFile], context: PackageContext) -> dict[str, StagedFile]:
        """Map every destination to its staged file, refusing sets that cannot coexist.

        Raises:
            ValueError: see :meth:`destination`.
            OutputError: two files would end up at the same place on a FAT/exFAT volume.
        """
        planned: dict[str, StagedFile] = {}
        #: Destination as a case-insensitive volume sees it -> destination as planned.
        spelling: dict[str, str] = {}
        for item in staged:
            destination = self.destination(item.logical, context)
            key = _fold_case(destination)
            taken = spelling.get(key)
            if taken is not None:
                if taken == destination:
                    raise OutputError(
                        f"{planned[taken].logical!r} and {item.logical!r} would both be "
                        f"written to {destination!r}; leave one of them out of the build"
                    )
                raise OutputError(
                    f"{planned[taken].logical!r} (packaged as {taken!r}) and {item.logical!r} "
                    f"(packaged as {destination!r}) differ only by letter case. SD cards are "
                    "formatted as FAT/exFAT, where case does not count, so one would "
                    "overwrite the other; leave one of them out of the build"
                )
            spelling[key] = destination
            planned[destination] = item

        seen: set[str] = set()
        for key, destination in spelling.items():
            for directory in _parents(key):
                if directory in seen:
                    break  # this directory and everything above it were checked already
                seen.add(directory)
                taken = spelling.get(directory)
                if taken is not None:
                    raise OutputError(
                        f"{planned[taken].logical!r} is packaged as the file {taken!r}, but "
                        f"{planned[destination].logical!r} (packaged as {destination!r}) "
                        "needs a directory of that name; leave one of them out of the build"
                    )
        return planned

    def _store_manifest(
        self, path: Path, context: PackageContext, files: list[str], on_disk: bytes | None
    ) -> bytes:
        """Make the manifest list ``files`` and return its content.

        ``on_disk`` is what the file holds right now. Nothing is written when that is
        already the wanted content, so that repeating a run leaves even the manifest alone.

        Raises:
            OutputError: the manifest cannot be written.
        """
        document = {
            "schema": _MANIFEST_SCHEMA,
            "packager": self.name,
            "layout": self.layout.value,
            "title_id": context.title_id,
            "files": files,
        }
        text = json.dumps(document, sort_keys=True, indent=2) + "\n"
        content = text.encode("utf-8")
        if content != on_disk:
            try:
                fsutil.atomic_write_text(path, text)
            except OSError as exc:
                raise OutputError(f"cannot write {path}: {_reason(exc)}") from exc
        return content


def _check_context(context: PackageContext) -> None:
    """Refuse names that could not be used as directory names.

    Raises:
        ValueError: the title ID or the patch-set name is not in its canonical form.
    """
    title_id = context.title_id
    if not isinstance(title_id, str) or _TITLE_ID.fullmatch(title_id) is None:
        raise ValueError(f"a title ID is 16 uppercase hex digits, got {title_id!r}")
    patchset = context.patchset_name
    if not isinstance(patchset, str) or _PATCHSET_NAME.fullmatch(patchset) is None:
        raise ValueError(
            f"a patch-set name starts with a letter or digit and holds at most 64 letters, "
            f"digits, '.', '_' or '-', got {patchset!r}"
        )


def _is_clean_relative(relative: str) -> bool:
    """Tell whether ``relative`` passes the rules of :func:`bea.fsutil.resolve_relative`."""
    try:
        fsutil.resolve_relative("", relative)
    except ValueError:
        return False
    return True


def _fold_case(path: str) -> str:
    """Return ``path`` as a FAT/exFAT volume compares it: letter case removed."""
    upper = path.upper()
    if len(upper) == len(path):
        return upper
    # Those filesystems fold names through a one-to-one table, so a character whose
    # uppercase form is several characters (the German sharp s) is left as it is: folding
    # it the Unicode way would report a collision between two names the card keeps apart.
    return "".join(folded if len(folded := char.upper()) == 1 else char for char in path)


def _parents(relative: str) -> Iterator[str]:
    """Yield the directories on the way to ``relative``, deepest first (``a/b/c``: ``a/b``, ``a``)."""
    cut = relative.rfind("/")
    while cut != -1:
        yield relative[:cut]
        cut = relative.rfind("/", 0, cut)


def _reason(error: OSError) -> str:
    """Return the operating system's explanation of ``error``, without the path."""
    return error.strerror or str(error)


def _quote(item: object) -> str:
    """Return ``item`` quoted for a message; text read from a manifest may be anything."""
    text = repr(item)
    return text if len(text) <= _QUOTE_LIMIT else f"{text[: _QUOTE_LIMIT - 3]}..."


def _sample(items: Sequence[object]) -> str:
    """Quote the first few ``items`` and count the others."""
    shown = ", ".join(_quote(item) for item in items[:_SAMPLE_SIZE])
    extra = len(items) - _SAMPLE_SIZE
    return f"{shown} and {extra} more" if extra > 0 else shown


def _stat_source(item: StagedFile) -> os.stat_result:
    """Return the ``stat`` of a staged file.

    Raises:
        OutputError: the file is missing, unreadable or not a regular file.
    """
    try:
        info = os.stat(fsutil.fs_path(item.path))
    except OSError as exc:
        raise OutputError(
            f"cannot package {item.logical}: the staged file {item.path} cannot be read "
            f"({_reason(exc)}); run the build again"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise OutputError(
            f"cannot package {item.logical}: {item.path} is not a regular file; run the build again"
        )
    return info


def _prepare_root(root: Path) -> None:
    """Create the package folder if it does not exist yet.

    Raises:
        OutputError: ``root`` exists and is not a directory, or it cannot be created.
    """
    target = fsutil.fs_path(root)
    if os.path.lexists(target) and not os.path.isdir(target):
        raise OutputError(
            f"cannot package into {root}: it exists and is not a directory; "
            "choose another output folder or move that file away"
        )
    try:
        fsutil.ensure_dir(root)
    except OSError as exc:
        raise OutputError(f"cannot create the output folder {root}: {_reason(exc)}") from exc


def _is_in_place(source: os.stat_result, existing: os.stat_result) -> bool:
    """Tell whether an existing destination already is the staged file.

    Either it is the very same file (the hard link an earlier run made) or it carries
    the fingerprint a copy keeps: the same size and the same modification time.
    """
    # Some network and virtual filesystems report no file identity at all (zero), which
    # would make any two files on them look like the same file.
    if source.st_ino != 0 and os.path.samestat(source, existing):
        return True
    return (
        stat.S_ISREG(existing.st_mode)
        and existing.st_size == source.st_size
        and existing.st_mtime_ns == source.st_mtime_ns
    )


def _read_manifest(path: Path, packager: str, warnings: list[str]) -> _PreviousRun:
    """Read the manifest a previous run left at ``path``, trusting nothing in it.

    A manifest that cannot be used is reported in ``warnings`` and yields no files, so
    that nothing is ever deleted on the strength of a list BEA could not read.

    Raises:
        OutputError: ``path`` is a directory, so no manifest could be written later.
    """
    target = fsutil.fs_path(path)
    if os.path.isdir(target):
        raise OutputError(
            f"cannot package into {path.parent}: {MANIFEST_NAME} there is a directory, and "
            "BEA needs that name for the list of the files it creates; move it away"
        )
    try:
        with open(target, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return _PreviousRun()
    except OSError as exc:
        warnings.append(_unusable(path, f"it cannot be read: {_reason(exc)}"))
        return _PreviousRun()
    try:
        entries, title_id = _parse_manifest(raw, packager)
    except ValueError as exc:
        warnings.append(_unusable(path, str(exc)))
        return _PreviousRun(raw=raw)

    files: set[str] = set()
    rejected: list[object] = []
    for entry in entries:
        if isinstance(entry, str) and _is_package_file(entry):
            files.add(entry)
        else:
            rejected.append(entry)
    if rejected:
        warnings.append(
            f"{path}: ignored {len(rejected)} entr{'y' if len(rejected) == 1 else 'ies'} "
            f"that cannot be the path of a file inside the package: {_sample(rejected)}"
        )
    return _PreviousRun(
        raw=raw, files=frozenset(files), title_id=title_id if isinstance(title_id, str) else None
    )


def _unusable(path: Path, why: str) -> str:
    """Word the warning for a manifest that had to be ignored as a whole."""
    return (
        f"{path}: the list of files written by the previous run was ignored ({why}), so "
        "nothing of that run was removed; delete its leftovers by hand if the package changed"
    )


def _parse_manifest(raw: bytes, packager: str) -> tuple[list[object], object]:
    """Return the ``files`` entries and the ``title_id`` of a manifest, both unchecked.

    Raises:
        ValueError: the content is not a manifest written by ``packager``; the message
            says why and completes the sentence "it was ignored (...)".
    """
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        # Invalid UTF-8 and invalid JSON both arrive as ValueError; absurdly deep nesting
        # overflows the parser's recursion.
        raise ValueError(f"it is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ValueError("it is not a JSON object")
    schema = document.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != _MANIFEST_SCHEMA:
        raise ValueError(
            f"its schema is {_quote(schema)} and this version of BEA reads schema "
            f"{_MANIFEST_SCHEMA}"
        )
    if document.get("packager") != packager:
        raise ValueError(
            f"it was written by the packager {_quote(document.get('packager'))}, "
            f"not by {packager!r}"
        )
    files = document.get("files")
    if not isinstance(files, list):
        raise ValueError('its "files" entry is not a list')
    return files, document.get("title_id")


def _is_package_file(entry: str) -> bool:
    """Tell whether a manifest entry can be the path of a file BEA put in the package."""
    if "\x00" in entry or not _is_clean_relative(entry):
        return False
    # The manifest never lists itself; an entry that does would have BEA delete its own
    # record of what it created.
    return _fold_case(entry) != _fold_case(MANIFEST_NAME)


def _remove_stale(root: Path, stale: Iterable[str], warnings: list[str]) -> list[str]:
    """Delete the files of the previous run that are no longer part of the package.

    Returns the paths that were actually deleted, sorted. Directories that these files
    leave empty are removed as well; nothing else below ``root`` is touched. An entry
    that is not (or no longer) a plain file BEA may delete is left alone with a warning.

    Raises:
        OutputError: a stale file exists but cannot be deleted.
    """
    removed: list[str] = []
    behind_links: list[str] = []
    directories: list[str] = []
    emptied: set[str] = set()
    links: dict[str, bool] = {}
    for relative in sorted(stale):
        if _is_behind_link(root, relative, links):
            behind_links.append(relative)
            continue
        target = fsutil.fs_path(fsutil.resolve_relative(root, relative))
        try:
            info = os.lstat(target)
        except (FileNotFoundError, NotADirectoryError):
            # Already gone (an interrupted run, or the user tidied up): only the
            # directories it may have left behind still matter.
            emptied.update(_parents(relative))
            continue
        except (OSError, ValueError) as exc:
            warnings.append(
                f"{relative}: this file of the previous run could not be examined ({exc}) "
                "and was left alone"
            )
            continue
        if stat.S_ISDIR(info.st_mode):
            directories.append(relative)
            continue
        try:
            if fsutil.remove_file(target):
                removed.append(relative)
        except OSError as exc:
            raise OutputError(
                f"cannot remove {fsutil.resolve_relative(root, relative)}, a file of the "
                f"previous run that is no longer part of the package: {_reason(exc)}; "
                "close any program that is using it and run again"
            ) from exc
        emptied.update(_parents(relative))

    # Deepest first, so that a directory holding nothing but emptied directories goes too.
    # Only the directories on the way to a deleted file are candidates: an empty directory
    # that was there for another reason is not BEA's to remove.
    for directory in sorted(emptied, key=lambda name: name.count("/"), reverse=True):
        with contextlib.suppress(OSError):
            os.rmdir(fsutil.fs_path(fsutil.resolve_relative(root, directory)))

    if behind_links:
        warnings.append(
            f"{len(behind_links)} file(s) of the previous run were left in {root} because "
            "they are reached through a symbolic link or junction, and BEA never deletes "
            f"through a link: {_sample(behind_links)}; delete them by hand if they are stale"
        )
    if directories:
        warnings.append(
            f"{len(directories)} entr{'y' if len(directories) == 1 else 'ies'} of the "
            f"previous run in {root} now name a directory, not a file BEA created, and were "
            f"left alone: {_sample(directories)}"
        )
    return removed


def _is_behind_link(root: Path, relative: str, known: dict[str, bool]) -> bool:
    """Tell whether a directory between ``root`` and the file ``relative`` is a link.

    A manifest entry is only text: followed blindly through a symbolic link or a
    junction it could name a file anywhere on the machine. ``known`` caches the answer
    per directory across calls.
    """
    behind = False
    for directory in reversed(list(_parents(relative))):
        cached = known.get(directory)
        if cached is None:
            target = fsutil.fs_path(fsutil.resolve_relative(root, directory))
            cached = behind or os.path.islink(target) or os.path.isjunction(target)
            known[directory] = cached
        behind = cached
        if behind:
            break
    return behind
