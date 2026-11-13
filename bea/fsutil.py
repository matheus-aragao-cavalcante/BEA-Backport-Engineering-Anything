"""Filesystem helpers: long-path safety, atomic writes, deterministic walks.

All file I/O in BEA goes through these helpers for three reasons:

* game trees are deep: ``<out>/atmosphere/contents/<id>/romfs/<long engine path>`` easily
  exceeds the 260-character limit of classic Windows APIs, so paths are converted with
  :func:`fs_path` before they reach the OS;
* a build may be interrupted at any time, so outputs are written to a temporary sibling
  and moved into place (:func:`atomic_output`), never left half-written;
* builds must be reproducible, so directory walks are sorted (:func:`walk_files`).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

__all__ = [
    "WalkedFile",
    "atomic_output",
    "atomic_write_bytes",
    "atomic_write_text",
    "ensure_dir",
    "fs_path",
    "link_or_copy",
    "prune_empty_dirs",
    "remove_file",
    "resolve_relative",
    "sha256_file",
    "walk_files",
]

_IS_WINDOWS = sys.platform == "win32"
_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
# The Windows device namespace: such a path is complete as it is, and it is not a UNC
# path to a server called ".".
_DEVICE_PREFIX = "\\\\.\\"
# Classic Windows APIs fail at 260 characters for files and 248 for directories; convert
# a little earlier so that temporary sibling names still fit.
_LONG_PATH_THRESHOLD = 200
_TMP_MARKER = ".bea-tmp-"
# Longest file name the usual filesystems store: 255 bytes (POSIX) or UTF-16 units (NTFS).
_MAX_NAME_LENGTH = 255
_CHUNK = 1024 * 1024
# On Windows a virus scanner or the search indexer may hold a freshly written file open
# for a moment, which makes the final rename fail with a sharing violation. Retrying for
# a few seconds turns that into a non-event.
_REPLACE_ATTEMPTS = 20 if _IS_WINDOWS else 1
_REPLACE_DELAY_SECONDS = 0.25


def _replace(source: Path, destination: Path) -> None:
    """Rename ``source`` over ``destination``, riding out transient Windows locks."""
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
        except PermissionError:
            # A directory in the way is not a lock that goes away: waiting for it would
            # only delay the same failure by several seconds.
            if attempt == _REPLACE_ATTEMPTS or os.path.isdir(destination):
                raise
            time.sleep(_REPLACE_DELAY_SECONDS)
        else:
            break
    # A POSIX rename succeeds without doing anything when both names are hard links to
    # the same file (link_or_copy onto its own earlier result): the temporary name would
    # stay behind, one more on every run.
    with contextlib.suppress(FileNotFoundError):
        os.remove(source)


def _name_length(name: str) -> int:
    """Return the length a filesystem counts for ``name``, erring on the long side."""
    # UTF-8 bytes (what POSIX limits) are never fewer than UTF-16 units (what NTFS limits).
    return len(name.encode("utf-8", "surrogatepass"))


def _temporary_name(final: Path) -> str:
    """Return a fresh name for a temporary sibling of ``final`` that keeps its extension."""
    marker = f"{_TMP_MARKER}{secrets.token_hex(4)}"
    stem, suffix = final.stem, final.suffix
    excess = _name_length(stem) + len(marker) + _name_length(suffix) - _MAX_NAME_LENGTH
    if excess > 0:
        # A name close to the limit leaves no room for the marker. Make room in the stem
        # (every character dropped frees at least one unit), then in the extension.
        kept = max(0, len(stem) - excess)
        excess -= _name_length(stem[kept:])
        stem = stem[:kept]
        if excess > 0:
            suffix = suffix[excess:]
    return f"{stem}{marker}{suffix}"


def fs_path(path: str | os.PathLike[str], *, force: bool = False) -> str:
    """Return ``path`` as an absolute string that the OS accepts at any length.

    On Windows, long absolute paths get the extended-length prefix (``\\\\?\\`` or
    ``\\\\?\\UNC\\``); elsewhere this is just :func:`os.path.abspath`. With ``force`` the
    prefix is added regardless of length, which is what recursive walks need: the paths
    they build while descending can outgrow the limit even when the root is short.
    Paths that already name a Windows namespace (``\\\\?\\`` or ``\\\\.\\``) are returned
    as they are.
    """
    absolute = os.path.abspath(os.fspath(path))
    if not _IS_WINDOWS or absolute.startswith((_EXTENDED_PREFIX, _DEVICE_PREFIX)):
        return absolute
    if not force and len(absolute) < _LONG_PATH_THRESHOLD:
        return absolute
    if absolute.startswith("\\\\"):
        return _EXTENDED_UNC_PREFIX + absolute[2:]
    return _EXTENDED_PREFIX + absolute


def ensure_dir(path: str | os.PathLike[str]) -> None:
    """Create a directory (and its parents) if it does not exist yet."""
    os.makedirs(fs_path(path), exist_ok=True)


def remove_file(path: str | os.PathLike[str]) -> bool:
    """Delete a file if it exists. Return ``True`` when something was deleted."""
    try:
        os.remove(fs_path(path))
    except (FileNotFoundError, NotADirectoryError):
        # NotADirectoryError is what POSIX reports when a parent of the path is a file:
        # no such file can exist, which Windows reports as "path not found".
        return False
    return True


@contextlib.contextmanager
def atomic_output(path: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield a temporary sibling of ``path`` to write into.

    When the ``with`` block finishes normally the temporary file replaces ``path`` in a
    single rename; when it raises, the temporary file is deleted and ``path`` is left
    untouched. The temporary name keeps the final extension, so tools that pick a format
    from the file name (image libraries, ffmpeg) behave the same as for ``path``.

    The yielded path is already long-path safe, and its name is never longer than a file
    name may be; parent directories are created.
    """
    final = Path(fs_path(path))
    ensure_dir(final.parent)
    temporary = final.with_name(_temporary_name(final))
    try:
        yield temporary
        _replace(temporary, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes | bytearray | memoryview) -> None:
    """Write ``data`` to ``path`` atomically (see :func:`atomic_output`)."""
    with atomic_output(path) as temporary, open(temporary, "wb") as handle:
        handle.write(data)


def atomic_write_text(path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically, with ``\\n`` line endings on every platform."""
    atomic_write_bytes(path, text.encode(encoding))


def link_or_copy(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> str:
    """Make ``destination`` a copy of ``source``, hard-linking when the filesystem allows it.

    Returns ``"link"`` or ``"copy"``. An existing ``destination`` is replaced atomically.
    Hard links are safe here because BEA never modifies a finished file in place: every
    rewrite goes through :func:`atomic_output`, which creates a new file.
    """
    src = fs_path(source)
    with atomic_output(destination) as temporary:
        try:
            os.link(src, temporary)
            return "link"
        except OSError:
            shutil.copy2(src, temporary)
            return "copy"


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return the SHA-256 of a file as lowercase hex."""
    digest = hashlib.sha256()
    with open(fs_path(path), "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class WalkedFile(NamedTuple):
    """One regular file found by :func:`walk_files`."""

    #: Path relative to the walked root, POSIX separators, original case.
    path: str
    size: int
    mtime_ns: int


def walk_files(root: str | os.PathLike[str]) -> Iterator[WalkedFile]:
    """Yield every regular file under ``root``, depth first, in sorted order.

    Sorting is by name within each directory (files and sub-directories interleaved in
    plain code-point order), which makes the sequence identical on every platform.
    Symbolic links and Windows directory junctions are neither followed nor reported.
    Leftover temporary files from an interrupted :func:`atomic_output` are skipped.
    """
    yield from _walk(fs_path(root, force=True), "")


def _walk(directory: str, prefix: str) -> Iterator[WalkedFile]:
    with os.scandir(directory) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        # A junction is a directory link in all but name: following one could leave the
        # tree, or never end when it points at one of its own parents.
        if entry.is_symlink() or entry.is_junction():
            continue
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(entry.path, f"{prefix}{entry.name}/")
        elif entry.is_file(follow_symlinks=False):
            if _TMP_MARKER in entry.name:
                continue
            stat = entry.stat(follow_symlinks=False)
            yield WalkedFile(f"{prefix}{entry.name}", stat.st_size, stat.st_mtime_ns)


def resolve_relative(root: str | os.PathLike[str], relative: str) -> Path:
    """Join a POSIX-style relative path onto ``root``, refusing anything that escapes it.

    Raises:
        ValueError: ``relative`` is empty, absolute, drive-qualified, or contains ``..``,
            ``.`` or empty segments, a backslash, a NUL, or a colon in any segment (on
            Windows a segment such as ``D:`` or ``name:stream`` selects another drive or
            an alternate data stream instead of a file below ``root``).
    """
    if not relative or "\\" in relative or "\x00" in relative or relative.startswith("/"):
        raise ValueError(f"not a clean relative path: {relative!r}")
    segments = relative.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in segments):
        raise ValueError(f"not a clean relative path: {relative!r}")
    return Path(os.fspath(root)).joinpath(*segments)


def _is_directory_link(path: str) -> bool:
    """Tell whether ``path`` is a symbolic link or a Windows directory junction."""
    return os.path.islink(path) or os.path.isjunction(path)


def prune_empty_dirs(root: str | os.PathLike[str]) -> int:
    """Remove empty directories below ``root`` (never ``root`` itself). Return the count.

    A link to a directory (a symbolic link, a Windows junction) is an entry of the
    directory that holds it: it is neither followed nor removed, and what it points at is
    never touched.
    """
    top = fs_path(root, force=True)
    if not os.path.isdir(top):
        return 0
    found: list[str] = []
    for directory, subdirs, _files in os.walk(top):
        # os.walk() lists directory links among the sub-directories and, on Windows,
        # descends into junctions. Walking top-down is what allows dropping them first.
        subdirs[:] = [
            name for name in subdirs if not _is_directory_link(os.path.join(directory, name))
        ]
        if directory != top:
            found.append(directory)
    removed = 0
    # Every directory was found after its parents, so the reverse order is bottom-up.
    for directory in reversed(found):
        try:
            os.rmdir(directory)
        except OSError:
            continue
        removed += 1
    return removed
