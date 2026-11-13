"""Locating optional external tools (ffmpeg, ffprobe, ...).

BEA never bundles third-party binaries. When a feature needs one, it is looked up, in
this order:

1. the environment variable ``BEA_<NAME>`` (for example ``BEA_FFMPEG``), which must
   point at the executable itself;
2. the local tools directory (``--tools-dir`` / ``BEA_TOOLS_DIR``, by default ``tools``
   in the current directory), at ``<dir>/<name>``, ``<dir>/bin/<name>``,
   ``<dir>/<name>/<name>`` or ``<dir>/<name>/bin/<name>``;
3. the ``PATH``.

A missing tool is never fatal by itself: the features that need it report their files as
unsupported and say how to enable them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bea.errors import ToolError

__all__ = ["KNOWN_TOOLS", "ToolInfo", "Toolbox"]

#: Tools BEA knows how to use, with what they enable (shown by ``bea doctor``).
KNOWN_TOOLS: Mapping[str, str] = {
    "ffmpeg": "transcoding videos (media rules)",
    "ffprobe": "reading video/audio properties (analysis and media rules)",
}

_ENV_TOOLS_DIR = "BEA_TOOLS_DIR"
_DEFAULT_TOOLS_DIRNAME = "tools"
_VERSION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """Where a tool was found (or not)."""

    name: str
    #: Absolute path of the executable, or ``None`` when it was not found.
    path: Path | None
    #: ``"env"``, ``"tools-dir"`` or ``"path"``; ``None`` when not found.
    origin: str | None
    #: First line printed by ``<tool> -version``; only filled by :meth:`Toolbox.describe`.
    version: str | None = None

    @property
    def available(self) -> bool:
        """Tell whether the tool can be run."""
        return self.path is not None


class Toolbox:
    """Finds external tools and remembers the answers."""

    def __init__(
        self,
        tools_dir: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        if tools_dir is None:
            tools_dir = self._environ.get(_ENV_TOOLS_DIR) or _DEFAULT_TOOLS_DIRNAME
        self.tools_dir = Path(tools_dir)
        self._cache: dict[str, ToolInfo] = {}

    def locate(self, name: str) -> ToolInfo:
        """Return where ``name`` is (cached). Never raises for a missing tool."""
        cached = self._cache.get(name)
        if cached is None:
            cached = self._locate(name)
            self._cache[name] = cached
        return cached

    def find(self, name: str) -> Path | None:
        """Return the path of ``name``, or ``None`` when it is not installed."""
        return self.locate(name).path

    def require(self, name: str) -> Path:
        """Return the path of ``name``.

        Raises:
            ToolError: the tool is not installed; the message says how to provide it.
        """
        path = self.find(name)
        if path is None:
            raise ToolError(self.missing_hint(name))
        return path

    def missing_hint(self, name: str) -> str:
        """Return the sentence shown to the user when ``name`` is needed but missing."""
        variable = self._env_name(name)
        override = self._environ.get(variable)
        if override:
            # Pointing at PATH would be wrong advice: the user chose a file explicitly.
            return (
                f'{name} was not found: {variable} is set to "{override}", '
                "which is not an executable file; fix or unset that variable"
            )
        return (
            f"{name} was not found: install it and make sure it is on PATH, "
            f"or set {variable} to the executable"
        )

    def describe(self, names: Sequence[str] | None = None) -> list[ToolInfo]:
        """Return the status of every known tool, including its version line."""
        result = []
        for name in names if names is not None else tuple(KNOWN_TOOLS):
            info = self.locate(name)
            if info.path is not None:
                info = ToolInfo(info.name, info.path, info.origin, _version_line(info.path))
            result.append(info)
        return result

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _env_name(name: str) -> str:
        return "BEA_" + "".join(char if char.isalnum() else "_" for char in name).upper()

    def _locate(self, name: str) -> ToolInfo:
        override = self._environ.get(self._env_name(name))
        if override:
            candidate = Path(override)
            if _is_executable_file(candidate):
                return ToolInfo(name, _absolute(candidate), "env")
            # An explicit override that does not exist is a configuration mistake worth
            # surfacing, not something to paper over with a different binary.
            return ToolInfo(name, None, None)

        for filename in _executable_names(name):
            for candidate in (
                self.tools_dir / filename,
                self.tools_dir / "bin" / filename,
                self.tools_dir / name / filename,
                self.tools_dir / name / "bin" / filename,
            ):
                if _is_executable_file(candidate):
                    return ToolInfo(name, _absolute(candidate), "tools-dir")

        # shutil.which() falls back to the real PATH when given None: an explicit
        # environment without PATH must mean "no PATH", not "the process's PATH".
        search_path = self._environ.get("PATH") or ""
        found = shutil.which(name, path=search_path) if search_path else None
        if found:
            return ToolInfo(name, _absolute(Path(found)), "path")
        return ToolInfo(name, None, None)


def _absolute(path: Path) -> Path:
    """Make ``path`` absolute without following links.

    Launchers that serve several programs pick one from the name they are started
    under, so the path where a tool was found is the one to run.
    """
    return Path(os.path.abspath(path))


def _executable_names(name: str) -> tuple[str, ...]:
    if sys.platform == "win32":
        return (f"{name}.exe", name)
    return (name,)


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _version_line(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None
