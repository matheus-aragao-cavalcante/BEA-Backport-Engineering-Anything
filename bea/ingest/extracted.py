"""Ingest: turn a game folder that the user already extracted into a :class:`GameInput`.

The input is a directory holding an executable section (the *ExeFS*: ``main``,
``main.npdm``, usually ``rtld`` and ``sdk``) and/or a data section (the *RomFS*: the
game's asset tree). Extraction tools name these folders differently (``ExeFS``,
``exefs``, ``0`` ...), so:

* the ExeFS is found by **content**: a directory with a file ``main`` that starts with
  ``NSO0``;
* the RomFS is found by **name** (``romfs``, ``data``, ``1``, ``section1``), compared
  without regard to case;
* either can be given explicitly instead.

Identity comes from the files themselves: the title ID from ``main.npdm`` (the
program ID the system runs the game under), the build ID of each module from its NSO
header, and the name and version from ``control.nacp`` when one happens to be present
(most extractions do not have it). Every field records where it came from.

Ingest only reads, and reads little: module headers, not modules. Packaged dumps
(``.nsp``, ``.xci`` and their compressed forms) are recognized and refused with an
explanation; BEA does not open, decrypt or extract them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from bea.errors import BeaError, InputError, UnsupportedInputError
from bea.formats.nacp import Nacp, read_nacp
from bea.formats.npdm import Npdm, read_npdm
from bea.formats.nso import NSO_MAGIC, read_header
from bea.fsutil import fs_path, walk_files
from bea.hexutil import base_program_id, is_update_id, normalize_title_id
from bea.model import FileEntry, GameInput, ModuleInfo, SegmentInfo, TitleInfo

__all__ = ["MODULE_ORDER", "PACKAGE_EXTENSIONS", "open_input"]

#: Extensions of packaged dumps, which BEA does not read.
PACKAGE_EXTENSIONS = frozenset({".nsp", ".nsz", ".xci", ".xcz", ".nca", ".ncz"})
#: Load order of executable modules; any other module sorts after these, by name.
MODULE_ORDER: tuple[str, ...] = (
    "rtld",
    "main",
    *(f"subsdk{index}" for index in range(10)),
    "sdk",
)

_EXEFS_NAMES = ("exefs", "code", "0", "section0")
_ROMFS_NAMES = ("romfs", "data", "1", "section1")
_NPDM_NAME = "main.npdm"
_NACP_NAME = "control.nacp"
_PACKAGE_MAGICS = (b"PFS0", b"HFS0")
_CARD_MAGIC = b"HEAD"
_CARD_MAGIC_OFFSET = 0x100
_ID_IN_PATH = re.compile(r"(?<![0-9A-Fa-f])\[?([0-9A-Fa-f]{16})\]?(?![0-9A-Fa-f])")


def open_input(
    path: str | os.PathLike[str],
    *,
    title_id: str | None = None,
    exefs_dir: str | os.PathLike[str] | None = None,
    romfs_dir: str | os.PathLike[str] | None = None,
) -> GameInput:
    """Index the extracted game folder at ``path``.

    ``title_id`` overrides the identity read from the files; ``exefs_dir`` and
    ``romfs_dir`` point at the two trees when detection cannot find them.

    Raises:
        UnsupportedInputError: ``path`` is a packaged dump.
        InputError: ``path`` is missing or not a folder, the trees cannot be found, or an
            override does not name an existing directory.
    """
    root = Path(os.path.abspath(os.fspath(path)))
    if not os.path.exists(fs_path(root)):
        raise InputError(f"{root} does not exist")
    if not os.path.isdir(fs_path(root)):
        _refuse_file(root)

    warnings: list[str] = []
    exefs = _given_dir(exefs_dir, "--exefs") if exefs_dir is not None else _find_exefs(root)
    romfs = _given_dir(romfs_dir, "--romfs") if romfs_dir is not None else _find_romfs(root, exefs)
    if exefs is None and romfs is None:
        raise InputError(
            f"no ExeFS or RomFS found in {root}: BEA expects a folder you already extracted, "
            "holding a directory with the executable modules (a 'main' file starting with "
            "NSO0) and/or the asset tree (a directory named romfs or RomFS). Point at them "
            "with --exefs / --romfs if they are named differently"
        )

    modules: tuple[ModuleInfo, ...] = ()
    exefs_files: tuple[str, ...] = ()
    npdm: Npdm | None = None
    if exefs is None:
        warnings.append("no ExeFS was found: executable patches cannot be applied")
    else:
        exefs_files, modules, npdm = _read_exefs(exefs, warnings)

    nacp_path, nacp = _find_nacp(root, exefs, warnings)
    title = _identity(root, title_id, npdm, nacp, warnings)

    romfs_files: tuple[FileEntry, ...] = ()
    if romfs is None:
        warnings.append("no RomFS was found: there are no assets to process")
    else:
        romfs_files = tuple(
            FileEntry(found.path, found.size, found.mtime_ns) for found in walk_files(romfs)
        )
        if not romfs_files:
            warnings.append(f"the RomFS folder {romfs} is empty")

    if title.title_id is None:
        warnings.append(
            "the title ID could not be determined (no main.npdm, no control.nacp, no ID in "
            "the folder name): pass it with --title-id"
        )

    return GameInput(
        root=root,
        exefs_dir=exefs,
        romfs_dir=romfs,
        nacp_path=nacp_path,
        title=title,
        modules=modules,
        exefs_files=exefs_files,
        romfs_files=romfs_files,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------- inputs


def _refuse_file(path: Path) -> None:
    prefix = _read_prefix(path, _CARD_MAGIC_OFFSET + len(_CARD_MAGIC))
    packaged = (
        path.suffix.lower() in PACKAGE_EXTENSIONS
        or prefix[:4] in _PACKAGE_MAGICS
        or prefix[_CARD_MAGIC_OFFSET : _CARD_MAGIC_OFFSET + len(_CARD_MAGIC)] == _CARD_MAGIC
    )
    if packaged:
        raise UnsupportedInputError(
            f"{path.name} is a packaged dump. BEA only reads game folders that were already "
            "extracted (an ExeFS and/or a RomFS directory); it does not open, decrypt or "
            "extract packages"
        )
    raise InputError(f"{path} is a file; BEA expects the folder of an extracted game")


def _read_prefix(path: Path, size: int) -> bytes:
    try:
        with open(fs_path(path), "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _given_dir(value: str | os.PathLike[str], option: str) -> Path:
    directory = Path(os.path.abspath(os.fspath(value)))
    if not os.path.isdir(fs_path(directory)):
        raise InputError(f"{option} {directory} is not an existing directory")
    return directory


def _children(root: Path) -> list[Path]:
    try:
        with os.scandir(fs_path(root)) as scan:
            entries = sorted(
                (entry for entry in scan if entry.is_dir(follow_symlinks=False)),
                key=lambda entry: entry.name,
            )
    except OSError as exc:
        raise InputError(f"cannot list {root}: {exc}") from exc
    return [root / entry.name for entry in entries]


def _named(children: Iterable[Path], names: tuple[str, ...]) -> list[Path]:
    """Return the children whose name is one of ``names`` (any case), in ``names`` order."""
    found: list[Path] = []
    for wanted in names:
        matches = [child for child in children if child.name.casefold() == wanted]
        if len(matches) > 1:
            listing = ", ".join(child.name for child in matches)
            raise InputError(
                f"several folders differ only by letter case ({listing}); "
                "say which one to use with --exefs / --romfs"
            )
        found += matches
    return found


def _is_exefs(directory: Path) -> bool:
    return _read_prefix(directory / "main", len(NSO_MAGIC)) == NSO_MAGIC


def _holds_modules(directory: Path) -> bool:
    """Tell whether a folder holds any executable module or a ``main.npdm``."""
    try:
        with os.scandir(fs_path(directory)) as scan:
            files = [entry for entry in scan if entry.is_file(follow_symlinks=False)]
    except OSError:
        return False
    return any(
        entry.name == _NPDM_NAME
        or _read_prefix(directory / entry.name, len(NSO_MAGIC)) == NSO_MAGIC
        for entry in files
    )


def _find_exefs(root: Path) -> Path | None:
    children = _children(root)
    named = _named(children, _EXEFS_NAMES)
    for candidate in named:
        if _is_exefs(candidate):
            return candidate
    if _is_exefs(root):
        return root
    # A folder named like an ExeFS that lacks "main" is still the ExeFS (reported later).
    for candidate in named:
        if _holds_modules(candidate):
            return candidate
    others = [child for child in children if _is_exefs(child)]
    if len(others) > 1:
        listing = ", ".join(child.name for child in others)
        raise InputError(f"several folders hold a 'main' module ({listing}); pick one with --exefs")
    return others[0] if others else None


def _find_romfs(root: Path, exefs: Path | None) -> Path | None:
    for candidate in _named(_children(root), _ROMFS_NAMES):
        if candidate != exefs and not _is_exefs(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------- exefs


def _module_key(name: str) -> tuple[int, str]:
    try:
        return MODULE_ORDER.index(name), ""
    except ValueError:
        return len(MODULE_ORDER), name


def _read_exefs(
    exefs: Path, warnings: list[str]
) -> tuple[tuple[str, ...], tuple[ModuleInfo, ...], Npdm | None]:
    try:
        with os.scandir(fs_path(exefs)) as scan:
            names = sorted(entry.name for entry in scan if entry.is_file(follow_symlinks=False))
    except OSError as exc:
        raise InputError(f"cannot list {exefs}: {exc}") from exc

    modules: list[ModuleInfo] = []
    for name in names:
        path = exefs / name
        if _read_prefix(path, len(NSO_MAGIC)) != NSO_MAGIC:
            continue
        try:
            header = read_header(path)
            size = os.stat(fs_path(path)).st_size
        except (BeaError, OSError) as exc:
            warnings.append(f"module {name} cannot be read: {exc}")
            continue
        warnings += [f"module {name}: {warning}" for warning in header.warnings()]
        modules.append(
            ModuleInfo(
                name=name,
                path=path,
                size=size,
                build_id=header.build_id,
                segments=tuple(
                    SegmentInfo(
                        name=segment.name,
                        memory_offset=segment.memory_offset,
                        size=segment.size,
                        file_size=segment.stored_size,
                        compressed=segment.compressed,
                    )
                    for segment in header.segments
                ),
                bss_size=header.bss_size,
            )
        )
    modules.sort(key=lambda module: _module_key(module.name))
    if not any(module.name == "main" for module in modules):
        warnings.append(f"the ExeFS {exefs} has no readable 'main' module")

    npdm = None
    if _NPDM_NAME in names:
        try:
            npdm = read_npdm(exefs / _NPDM_NAME)
        except (BeaError, OSError) as exc:
            warnings.append(f"{_NPDM_NAME} cannot be read: {exc}")
        else:
            warnings += [f"{_NPDM_NAME}: {note}" for note in npdm.notes]
    return tuple(names), tuple(modules), npdm


# ----------------------------------------------------------------------------- nacp


def _files_named(
    directory: Path | None, *, exact: str | None = None, suffix: str = ""
) -> list[Path]:
    if directory is None:
        return []
    try:
        with os.scandir(fs_path(directory)) as scan:
            names = sorted(entry.name for entry in scan if entry.is_file(follow_symlinks=False))
    except OSError:
        return []
    if exact is not None:
        return [directory / name for name in names if name.casefold() == exact]
    return [directory / name for name in names if name.casefold().endswith(suffix)]


def _find_nacp(
    root: Path, exefs: Path | None, warnings: list[str]
) -> tuple[Path | None, Nacp | None]:
    """Probe the places where extraction tools leave ``control.nacp``; take the first readable."""
    control_dirs = [child for child in _children(root) if child.name.casefold() == "control"]
    candidates: list[Path] = []
    candidates += _files_named(root, exact=_NACP_NAME)
    candidates += _files_named(exefs, exact=_NACP_NAME)
    candidates += _files_named(exefs, suffix=".nacp")
    for directory in control_dirs:
        candidates += _files_named(directory, exact=_NACP_NAME)
    candidates += _files_named(root, suffix=".nacp")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            nacp = read_nacp(candidate)
        except (BeaError, OSError) as exc:
            warnings.append(f"{candidate.name} cannot be read: {exc}")
            continue
        warnings += [f"{candidate.name}: {note}" for note in nacp.notes]
        return candidate, nacp
    return None, None


# ------------------------------------------------------------------------- identity


def _id_from_path(root: Path) -> str | None:
    for part in reversed(root.parts):
        match = _ID_IN_PATH.search(part)
        if match is not None:
            return match.group(1).upper()
    return None


def _identity(
    root: Path,
    title_id: str | None,
    npdm: Npdm | None,
    nacp: Nacp | None,
    warnings: list[str],
) -> TitleInfo:
    sources: dict[str, str] = {}
    chosen: str | None = None
    if title_id is not None:
        chosen = normalize_title_id(title_id)
        sources["title_id"] = "option"
        if npdm is not None and npdm.title_id != chosen:
            warnings.append(
                f"--title-id {chosen} differs from the program ID {npdm.title_id} in "
                f"{_NPDM_NAME}; using {chosen}"
            )
    elif npdm is not None:
        chosen = npdm.title_id
        sources["title_id"] = "npdm"
    elif nacp is not None and nacp.inferred_program_id() is not None:
        inferred = nacp.inferred_program_id()
        assert inferred is not None
        chosen = normalize_title_id(inferred)
        sources["title_id"] = "nacp (inferred)"
    else:
        hinted = _id_from_path(root)
        if hinted is not None:
            chosen = hinted
            sources["title_id"] = "path"

    if chosen is not None and is_update_id(chosen):
        base = base_program_id(chosen)
        warnings.append(
            f"{chosen} is the ID of an update; mods and profiles use the base program ID {base}"
        )
        chosen = base

    name = publisher = version = None
    if nacp is not None:
        entry = nacp.title()
        if entry is not None:
            name, publisher = entry.name or None, entry.publisher or None
            if name:
                sources["name"] = "nacp"
            if publisher:
                sources["publisher"] = "nacp"
        if nacp.display_version:
            version = nacp.display_version
            sources["display_version"] = "nacp"
    return TitleInfo(
        title_id=chosen,
        name=name,
        publisher=publisher,
        display_version=version,
        sources=sources,
    )
