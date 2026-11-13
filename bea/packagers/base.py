"""The packager contract.

The pipeline leaves every produced file in a *staging tree* addressed by logical paths
(see :mod:`bea.model`). A packager turns that tree into something a console or a mod
loader understands. It only decides *where* each logical path goes and copies files; it
never changes their content.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

__all__ = ["PackageContext", "PackageResult", "Packager", "StagedFile"]


@dataclass(frozen=True, slots=True)
class StagedFile:
    """One finished file of the staging tree."""

    #: Logical path (``romfs/...``, ``exefs/...``, ``exefs_patches/...``).
    logical: str
    #: Where the file currently is on disk.
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class PackageContext:
    """Naming information a packager needs."""

    #: 16 uppercase hex digits.
    title_id: str
    #: Directory name for executable patches / the mod (already filesystem-safe).
    patchset_name: str


@dataclass(slots=True)
class PackageResult:
    """What a packager did."""

    #: Directory that now holds the package.
    root: Path
    #: Package-relative POSIX paths of every file of the package, sorted.
    files: list[str] = field(default_factory=list)
    #: Package-relative paths of files from a previous run that were removed.
    removed: list[str] = field(default_factory=list)
    #: How many files were hard-linked instead of copied.
    linked: int = 0
    copied: int = 0
    #: How many files were already in place and identical (nothing to do).
    unchanged: int = 0
    #: Things the user should know (for example an unreadable manifest from a previous run).
    warnings: list[str] = field(default_factory=list)


class Packager(ABC):
    """Base class of every packager."""

    #: Short unique identifier (``"layeredfs"``).
    name: ClassVar[str]

    @abstractmethod
    def destination(self, logical: str, context: PackageContext) -> str:
        """Return the package-relative POSIX path where ``logical`` belongs.

        Raises:
            ValueError: ``logical`` is not a logical path this packager understands.
        """

    @abstractmethod
    def package(
        self, staged: Sequence[StagedFile], out_dir: Path, context: PackageContext
    ) -> PackageResult:
        """Write the package for ``staged`` into ``out_dir``.

        Running it again with the same input changes nothing. Files that a previous run
        of the same packager put in ``out_dir`` and that are no longer staged are removed;
        files BEA did not create are never touched.

        Raises:
            OutputError: ``out_dir`` cannot be used safely.
        """
