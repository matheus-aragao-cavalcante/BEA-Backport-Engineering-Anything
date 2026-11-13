"""Shared data model: what ingest produces and what every later stage consumes.

These types are deliberately plain (frozen dataclasses, no behaviour beyond small
conveniences) so that transformers, packagers, the analyzer and the CLI can exchange
them without importing each other.

Logical paths
-------------
Inside the pipeline every file is named by a *logical path*: a POSIX-style string whose
first segment says which tree it belongs to:

``romfs/<path>``
    a file of the game's RomFS (``<path>`` keeps the original case);
``exefs/<name>``
    a file of the game's ExeFS (``main``, ``sdk``, ``main.npdm``, ...);
``exefs_patches/<BUILD_ID>.ips``
    a generated executable patch, named after the canonical build ID it targets.

Packagers decide where each logical path ends up on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bea.fsutil import resolve_relative

__all__ = [
    "EXEFS_PATCHES_PREFIX",
    "EXEFS_PREFIX",
    "ROMFS_PREFIX",
    "FileEntry",
    "GameInput",
    "ModuleInfo",
    "SegmentInfo",
    "TitleInfo",
    "split_logical",
]

ROMFS_PREFIX = "romfs/"
EXEFS_PREFIX = "exefs/"
EXEFS_PATCHES_PREFIX = "exefs_patches/"


def split_logical(logical: str) -> tuple[str, str]:
    """Split a logical path into ``(tree, relative_path)``.

    Raises:
        ValueError: the path does not start with a known tree prefix or has no remainder.
    """
    for prefix in (ROMFS_PREFIX, EXEFS_PATCHES_PREFIX, EXEFS_PREFIX):
        if logical.startswith(prefix) and len(logical) > len(prefix):
            return prefix[:-1], logical[len(prefix) :]
    raise ValueError(f"not a logical path: {logical!r}")


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file of the game's RomFS, as found on disk."""

    #: RomFS-relative POSIX path, original case.
    path: str
    size: int
    #: Modification time in nanoseconds; with ``size`` it fingerprints the file for caching.
    mtime_ns: int

    @property
    def name(self) -> str:
        """File name without directories."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        """Lower-case extension including the dot (``".png"``), or ``""``.

        A leading dot starts a hidden name (``.gitignore`` has no extension) and a
        trailing dot is not an extension (``name.`` has none). Spelled out rather than
        taken from :mod:`pathlib`, whose answer for names ending in a dot changed
        between Python versions.
        """
        name = self.name
        dot = name.rfind(".")
        if dot <= 0 or dot == len(name) - 1 or not name[:dot].strip("."):
            return ""
        return name[dot:].lower()

    @property
    def logical(self) -> str:
        """Logical path of this file (``romfs/<path>``)."""
        return ROMFS_PREFIX + self.path


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """One segment of an executable module."""

    #: ``"text"``, ``"rodata"`` or ``"data"``.
    name: str
    #: Offset of the segment inside the module's memory image.
    memory_offset: int
    #: Size once decompressed (the size it occupies in memory).
    size: int
    #: Size as stored in the file.
    file_size: int
    compressed: bool


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    """One executable module (NSO) of the game's ExeFS."""

    #: ExeFS file name: ``main``, ``rtld``, ``sdk``, ``subsdk0`` ...
    name: str
    path: Path
    #: Size of the file on disk.
    size: int
    #: Canonical build ID: uppercase hex, trailing zero bytes removed.
    build_id: str
    segments: tuple[SegmentInfo, ...]
    bss_size: int

    @property
    def logical(self) -> str:
        """Logical path of this module (``exefs/<name>``)."""
        return EXEFS_PREFIX + self.name


@dataclass(frozen=True, slots=True)
class TitleInfo:
    """Identity of the game, with the origin of every field.

    ``sources`` maps a field name to where its value came from: ``"npdm"``, ``"nacp"``
    or ``"option"`` (given on the command line).
    """

    #: 16 uppercase hex digits, or ``None`` when it could not be determined.
    title_id: str | None = None
    name: str | None = None
    publisher: str | None = None
    display_version: str | None = None
    sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GameInput:
    """An extracted game folder, indexed.

    Either tree may be missing (``None``): a folder with only a RomFS can still have its
    assets processed, and one with only an ExeFS can still be patched.
    """

    root: Path
    exefs_dir: Path | None
    romfs_dir: Path | None
    #: Path of the ``control.nacp`` that was found, if any.
    nacp_path: Path | None
    title: TitleInfo
    #: Executable modules, in ExeFS load order.
    modules: tuple[ModuleInfo, ...]
    #: Names of every file present in the ExeFS folder (modules included), sorted.
    exefs_files: tuple[str, ...]
    #: Every RomFS file, in the deterministic order of :func:`bea.fsutil.walk_files`.
    romfs_files: tuple[FileEntry, ...]
    #: Things worth telling the user about the input (missing trees, unreadable files ...).
    warnings: tuple[str, ...] = ()

    def module(self, name: str) -> ModuleInfo | None:
        """Return the module called ``name``, or ``None``."""
        for module in self.modules:
            if module.name == name:
                return module
        return None

    def resolve(self, logical: str) -> Path:
        """Return the on-disk path of an *input* logical path (``romfs/...`` or ``exefs/...``).

        Raises:
            ValueError: unknown tree, unsafe path, or the corresponding tree is missing.
        """
        tree, relative = split_logical(logical)
        base = {"romfs": self.romfs_dir, "exefs": self.exefs_dir}.get(tree)
        if base is None:
            raise ValueError(f"this input has no tree for {logical!r}")
        return resolve_relative(base, relative)
