"""Tests for bea.toolbox: where external tools are looked up, and in which order.

Every Toolbox here receives an explicit ``environ`` (or a patched process environment),
so the tools installed on the machine running the tests never influence a result.

The fake tools are tiny files created in ``tmp_path``. For a lookup any file will do,
because nothing runs it: it is named ``<name>.exe`` on Windows, which is what the
tools-directory rule looks for there. To read a version line a real program is needed:
a shell script on POSIX and a ``.cmd`` on Windows, where it is reached through the
environment override (the one place that takes the executable under any name).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bea import toolbox
from bea.errors import BeaError, ToolError
from bea.toolbox import KNOWN_TOOLS, Toolbox, ToolInfo

IS_WINDOWS = sys.platform == "win32"
#: No real program has this name, on any machine.
TOOL = "beafaketool"
OVERRIDE = "BEA_BEAFAKETOOL"


def tool_filename(name: str = TOOL) -> str:
    """File name under which the lookup rules find ``name`` on this platform."""
    return f"{name}.exe" if IS_WINDOWS else name


def place_tool(directory: Path, filename: str | None = None) -> Path:
    """Create a file that the lookup accepts as a tool (it is never executed)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename if filename is not None else tool_filename())
    path.write_bytes(b"not a real program\n")
    path.chmod(0o755)
    if not os.access(path, os.X_OK):
        pytest.skip("this temporary directory cannot hold executable files")
    return path


def write_program(
    directory: Path,
    basename: str,
    *,
    stdout: tuple[str, ...] = (),
    stderr: tuple[str, ...] = (),
    exit_code: int = 0,
) -> Path:
    """Create a runnable program that prints the given lines; ``{arg}`` is its first argument."""
    directory.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        path = directory / f"{basename}.cmd"
        lines = ["@echo off"]
        lines += [f"echo {line.format(arg='%1')}" if line else "echo." for line in stdout]
        lines += [f">&2 echo {line.format(arg='%1')}" for line in stderr]
        lines.append(f"exit /b {exit_code}")
        path.write_bytes("\r\n".join(lines).encode("ascii") + b"\r\n")
    else:
        path = directory / basename
        lines = ["#!/bin/sh"]
        lines += [f'echo "{line.format(arg="$1")}"' for line in stdout]
        lines += [f'echo "{line.format(arg="$1")}" >&2' for line in stderr]
        lines.append(f"exit {exit_code}")
        path.write_bytes("\n".join(lines).encode("ascii") + b"\n")
        path.chmod(0o755)
    return path


def runnable(program: Path) -> Path:
    """Return ``program``, or skip the test when this machine cannot run files from there."""
    try:
        subprocess.run(
            [str(program)], capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("programs cannot be executed from the temporary directory")
    return program


def link_directory(link: Path, target: Path) -> None:
    """Make ``link`` a directory link to ``target``, or skip the test when that is impossible."""
    if IS_WINDOWS:
        # A junction needs no privilege, unlike a symbolic link.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            pytest.skip("cannot create a directory junction here")
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symbolic links here")


def isolated(tools_dir: Path, **environ: str) -> Toolbox:
    """Build a Toolbox that sees only ``tools_dir`` and the given variables."""
    environ.setdefault("PATH", "")
    return Toolbox(tools_dir, environ=environ)


# ----------------------------------------------------------------------------------
# Lookup order
# ----------------------------------------------------------------------------------


def test_a_tool_that_is_nowhere_is_simply_missing(tmp_path: Path) -> None:
    box = isolated(tmp_path / "tools")
    info = box.locate(TOOL)
    assert info == ToolInfo(name=TOOL, path=None, origin=None, version=None)
    assert not info.available
    assert box.find(TOOL) is None


def test_environment_override_comes_first(tmp_path: Path) -> None:
    place_tool(tmp_path / "tools")
    place_tool(tmp_path / "on-path")
    chosen = place_tool(tmp_path / "elsewhere", "any-name-at-all.bin")
    box = isolated(tmp_path / "tools", PATH=str(tmp_path / "on-path"), **{OVERRIDE: str(chosen)})
    info = box.locate(TOOL)
    assert (info.path, info.origin) == (chosen, "env")
    assert info.available
    assert box.find(TOOL) == chosen
    assert box.require(TOOL) == chosen


def test_tools_directory_comes_before_path(tmp_path: Path) -> None:
    local = place_tool(tmp_path / "tools")
    place_tool(tmp_path / "on-path")
    info = isolated(tmp_path / "tools", PATH=str(tmp_path / "on-path")).locate(TOOL)
    assert (info.path, info.origin) == (local, "tools-dir")


def test_path_is_the_last_resort(tmp_path: Path) -> None:
    found = place_tool(tmp_path / "second")
    place_tool(tmp_path / "third")
    (tmp_path / "first").mkdir()  # exists, but does not hold the tool
    search = os.pathsep.join(
        str(tmp_path / name) for name in ("does-not-exist", "first", "second", "third")
    )
    info = isolated(tmp_path / "tools", PATH=search).locate(TOOL)
    assert (info.path, info.origin) == (found, "path")
    # Another tool on the same PATH is still missing.
    assert isolated(tmp_path / "tools", PATH=search).find("beaothertool") is None


#: The four documented places of the tools directory, in priority order.
LOCATIONS = ["", "bin", TOOL, f"{TOOL}/bin"]


@pytest.mark.parametrize("location", LOCATIONS)
def test_each_documented_place_of_the_tools_directory(tmp_path: Path, location: str) -> None:
    tools = tmp_path / "tools"
    expected = place_tool(tools / location)
    info = isolated(tools).locate(TOOL)
    assert (info.path, info.origin) == (expected, "tools-dir")
    # Only the tool's own folder is searched: another tool's folder does not count.
    assert isolated(tools).find("beaothertool") is None


@pytest.mark.parametrize(
    ("preferred", "other"),
    [(first, second) for index, first in enumerate(LOCATIONS) for second in LOCATIONS[index + 1 :]],
)
def test_places_of_the_tools_directory_are_tried_in_documented_order(
    tmp_path: Path, preferred: str, other: str
) -> None:
    # Elsewhere <dir>/<name> cannot be both the tool and the tool's folder.
    if not IS_WINDOWS and preferred == "" and other != "bin":
        pytest.skip("<dir>/<name> is either a file or a folder on this platform")
    tools = tmp_path / "tools"
    expected = place_tool(tools / preferred)
    place_tool(tools / other)
    assert isolated(tools).find(TOOL) == expected


def test_only_the_documented_places_are_searched(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    place_tool(tools / "sub")
    place_tool(tools / "bin" / "deeper")
    place_tool(tools / TOOL / "bin" / "x64")
    place_tool(tools / "beaothertool")  # another tool's folder
    assert isolated(tools).find(TOOL) is None


def test_a_folder_named_like_the_tool_is_not_the_tool(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    (tools / tool_filename()).mkdir(parents=True)
    (tools / "bin" / tool_filename()).mkdir(parents=True)
    assert isolated(tools).find(TOOL) is None
    on_path = tmp_path / "on-path"
    (on_path / tool_filename()).mkdir(parents=True)
    assert isolated(tmp_path / "none", PATH=str(on_path)).find(TOOL) is None


@pytest.mark.skipif(not IS_WINDOWS, reason="the .exe suffix is a Windows convention")
def test_windows_prefers_the_exe_and_accepts_the_bare_name(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    bare = place_tool(tools, TOOL)
    assert isolated(tools).find(TOOL) == bare
    executable = place_tool(tools, f"{TOOL}.exe")
    assert isolated(tools).find(TOOL) == executable


@pytest.mark.skipif(IS_WINDOWS, reason="Windows has no executable permission bit")
def test_a_file_without_the_executable_bit_is_not_a_tool(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    plain = place_tool(tools)
    plain.chmod(0o644)
    if os.access(plain, os.X_OK):
        pytest.skip("permission bits are not enforced here (running as root?)")
    assert isolated(tools).find(TOOL) is None
    assert not isolated(tools, **{OVERRIDE: str(plain)}).locate(TOOL).available


# ----------------------------------------------------------------------------------
# The environment override
# ----------------------------------------------------------------------------------


def test_an_override_that_points_nowhere_is_not_papered_over(tmp_path: Path) -> None:
    # The tool is available both locally and on PATH, but the user asked for another one.
    place_tool(tmp_path / "tools")
    place_tool(tmp_path / "on-path")
    nowhere = tmp_path / "nowhere" / tool_filename()
    box = isolated(tmp_path / "tools", PATH=str(tmp_path / "on-path"), **{OVERRIDE: str(nowhere)})
    info = box.locate(TOOL)
    assert info == ToolInfo(name=TOOL, path=None, origin=None)
    assert box.find(TOOL) is None

    # The message must name the real culprit: "put it on PATH" would be wrong advice here.
    hint = box.missing_hint(TOOL)
    assert OVERRIDE in hint
    assert str(nowhere) in hint
    with pytest.raises(ToolError) as raised:
        box.require(TOOL)
    assert str(nowhere) in str(raised.value)


def test_an_override_that_points_at_a_folder_is_not_a_tool(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    place_tool(folder)
    box = isolated(tmp_path / "none", **{OVERRIDE: str(folder)})
    assert not box.locate(TOOL).available
    assert str(folder) in box.missing_hint(TOOL)


def test_an_empty_override_is_ignored(tmp_path: Path) -> None:
    local = place_tool(tmp_path / "tools")
    info = isolated(tmp_path / "tools", **{OVERRIDE: ""}).locate(TOOL)
    assert (info.path, info.origin) == (local, "tools-dir")


def test_an_override_only_applies_to_its_own_tool(tmp_path: Path) -> None:
    chosen = place_tool(tmp_path / "elsewhere", "custom.bin")
    box = isolated(tmp_path / "none", **{OVERRIDE: str(chosen)})
    assert box.find(TOOL) == chosen
    assert box.find("beaothertool") is None


def test_override_variable_names() -> None:
    box = isolated(Path("unused"))
    assert "BEA_FFMPEG" in box.missing_hint("ffmpeg")
    assert "BEA_FFPROBE" in box.missing_hint("ffprobe")
    # Anything that cannot be part of a variable name becomes an underscore.
    assert "BEA_MY_TOOL_2_X" in box.missing_hint("my-tool.2+x")


def test_override_with_a_punctuated_tool_name(tmp_path: Path) -> None:
    chosen = place_tool(tmp_path / "elsewhere", "custom.bin")
    box = isolated(tmp_path / "none", BEA_MY_TOOL_2=str(chosen))
    assert box.locate("my-tool.2") == ToolInfo("my-tool.2", chosen, "env")


# ----------------------------------------------------------------------------------
# What the toolbox reports
# ----------------------------------------------------------------------------------


def test_require_explains_how_to_provide_a_missing_tool(tmp_path: Path) -> None:
    box = isolated(tmp_path / "tools")
    with pytest.raises(ToolError) as raised:
        box.require(TOOL)
    message = str(raised.value)
    assert message == box.missing_hint(TOOL)
    # What is missing, and the two ways to provide it.
    assert TOOL in message
    assert "PATH" in message
    assert OVERRIDE in message
    # A user-facing error: the command line reports it without a traceback.
    assert isinstance(raised.value, BeaError)
    assert raised.value.exit_code == 2


def test_require_returns_the_path_of_an_installed_tool(tmp_path: Path) -> None:
    local = place_tool(tmp_path / "tools" / "bin")
    assert isolated(tmp_path / "tools").require(TOOL) == local


def test_located_paths_are_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    place_tool(tmp_path / "tools")
    place_tool(tmp_path / "on-path")
    place_tool(tmp_path / "elsewhere", "custom.bin")
    monkeypatch.chdir(tmp_path)
    for box in (
        Toolbox(environ={"PATH": ""}),  # the default directory: "tools" in the current one
        Toolbox("tools", environ={"PATH": ""}),
        Toolbox("none", environ={"PATH": "on-path"}),
        Toolbox("none", environ={"PATH": "", OVERRIDE: os.path.join("elsewhere", "custom.bin")}),
    ):
        path = box.find(TOOL)
        assert path is not None
        assert path.is_absolute()
        assert path.is_file()
        assert os.path.samefile(path.parent.parent, tmp_path)


def test_located_path_is_where_the_tool_was_found_not_where_links_lead(tmp_path: Path) -> None:
    # A tool reached through a link must keep the path it was found at: launchers that
    # serve several programs choose one from the name they are started under.
    real = tmp_path / "real"
    place_tool(real)
    linked = tmp_path / "linked"
    link_directory(linked, real)
    for box in (
        isolated(linked),
        isolated(tmp_path / "none", PATH=str(linked)),
        isolated(tmp_path / "none", **{OVERRIDE: str(linked / tool_filename())}),
    ):
        assert box.find(TOOL) == linked / tool_filename()


@pytest.mark.skipif(IS_WINDOWS, reason="needs a symbolic link to a runnable script")
def test_a_linked_launcher_is_started_under_the_name_of_the_link(tmp_path: Path) -> None:
    launcher = tmp_path / "launchers" / "launcher"
    launcher.parent.mkdir()
    launcher.write_bytes(b'#!/bin/sh\necho "started as $(basename "$0")"\n')
    launcher.chmod(0o755)
    runnable(launcher)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    try:
        os.symlink(launcher, bin_dir / TOOL)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symbolic links here")
    box = isolated(tmp_path / "none", PATH=str(bin_dir))
    assert box.find(TOOL) == bin_dir / TOOL
    assert box.describe([TOOL])[0].version == f"started as {TOOL}"


def test_answers_are_remembered(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tool = place_tool(tools)
    box = isolated(tools)
    first = box.locate(TOOL)
    assert first.path == tool
    assert box.locate(TOOL) is first
    tool.unlink()
    assert box.locate(TOOL) is first  # remembered, not looked up again
    assert box.find(TOOL) == tool
    # A new toolbox looks again.
    assert isolated(tools).find(TOOL) is None


def test_a_miss_is_remembered_too(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    box = isolated(tools)
    assert box.find(TOOL) is None
    tool = place_tool(tools)
    assert box.find(TOOL) is None
    with pytest.raises(ToolError):
        box.require(TOOL)
    assert isolated(tools).find(TOOL) == tool


def test_tool_info_is_a_frozen_value() -> None:
    info = ToolInfo("x", None, None)
    assert info.version is None
    assert not info.available
    assert ToolInfo("x", Path("x"), "path").available
    with pytest.raises(AttributeError):
        info.name = "y"  # type: ignore[misc]


# ----------------------------------------------------------------------------------
# The tools directory and the environment
# ----------------------------------------------------------------------------------


def test_tools_directory_selection(tmp_path: Path) -> None:
    assert Toolbox(environ={}).tools_dir == Path("tools")
    assert Toolbox(environ={"BEA_TOOLS_DIR": ""}).tools_dir == Path("tools")
    from_env = Toolbox(environ={"BEA_TOOLS_DIR": str(tmp_path / "env-tools")})
    assert from_env.tools_dir == tmp_path / "env-tools"
    # An explicit directory (the --tools-dir option) beats the variable.
    explicit = Toolbox(tmp_path / "explicit", environ={"BEA_TOOLS_DIR": str(tmp_path / "env")})
    assert explicit.tools_dir == tmp_path / "explicit"
    assert Toolbox(str(tmp_path / "as-text"), environ={}).tools_dir == tmp_path / "as-text"


def test_tools_directory_from_the_environment_is_searched(tmp_path: Path) -> None:
    local = place_tool(tmp_path / "env-tools" / "bin")
    box = Toolbox(environ={"BEA_TOOLS_DIR": str(tmp_path / "env-tools"), "PATH": ""})
    assert box.locate(TOOL) == ToolInfo(TOOL, local, "tools-dir")


def test_an_explicit_environment_without_path_does_not_fall_back_to_the_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tool is on the PATH of this very process...
    place_tool(tmp_path / "process-path")
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"), prepend=os.pathsep)
    assert Toolbox(tmp_path / "none").find(TOOL) is not None
    # ...which a toolbox given its own environment must not look at.
    assert Toolbox(tmp_path / "none", environ={}).find(TOOL) is None
    assert Toolbox(tmp_path / "none", environ={"PATH": ""}).find(TOOL) is None


def test_the_process_environment_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = place_tool(tmp_path / "process-path")
    chosen = place_tool(tmp_path / "elsewhere", "custom.bin")
    local = place_tool(tmp_path / "env-tools")
    monkeypatch.delenv(OVERRIDE, raising=False)
    monkeypatch.delenv("BEA_TOOLS_DIR", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "process-path"), prepend=os.pathsep)
    assert Toolbox(tmp_path / "none").locate(TOOL) == ToolInfo(TOOL, on_path, "path")

    monkeypatch.setenv("BEA_TOOLS_DIR", str(tmp_path / "env-tools"))
    assert Toolbox().locate(TOOL) == ToolInfo(TOOL, local, "tools-dir")

    monkeypatch.setenv(OVERRIDE, str(chosen))
    assert Toolbox().locate(TOOL) == ToolInfo(TOOL, chosen, "env")


# ----------------------------------------------------------------------------------
# describe(): the version line
# ----------------------------------------------------------------------------------


def described(program: Path, tmp_path: Path) -> ToolInfo:
    """Describe ``program`` as the fake tool, reached through the environment override."""
    box = isolated(tmp_path / "none", **{OVERRIDE: str(runnable(program))})
    (info,) = box.describe([TOOL])
    assert (info.name, info.path, info.origin) == (TOOL, program, "env")
    return info


def test_describe_reads_the_first_line_printed_by_the_version_switch(tmp_path: Path) -> None:
    program = write_program(
        tmp_path / "bin", "fake", stdout=("faketool version 1.2.3 ({arg})", "built with care")
    )
    # The switch is passed, and only the first line is kept.
    assert described(program, tmp_path).version == "faketool version 1.2.3 (-version)"


def test_describe_skips_leading_blank_lines(tmp_path: Path) -> None:
    program = write_program(tmp_path / "bin", "fake", stdout=("", "faketool 2.0"))
    assert described(program, tmp_path).version == "faketool 2.0"


def test_describe_falls_back_to_the_error_stream(tmp_path: Path) -> None:
    quiet = write_program(tmp_path / "bin", "quiet", stderr=("faketool 3.1 on stderr",))
    assert described(quiet, tmp_path).version == "faketool 3.1 on stderr"
    both = write_program(tmp_path / "bin", "both", stdout=("from stdout",), stderr=("from stderr",))
    assert described(both, tmp_path).version == "from stdout"


def test_describe_ignores_the_exit_status(tmp_path: Path) -> None:
    program = write_program(tmp_path / "bin", "fake", stdout=("faketool 0.9",), exit_code=3)
    assert described(program, tmp_path).version == "faketool 0.9"


def test_describe_of_a_silent_tool_has_no_version(tmp_path: Path) -> None:
    program = write_program(tmp_path / "bin", "fake")
    info = described(program, tmp_path)
    assert info.version is None
    assert info.available


def test_describe_of_a_tool_that_cannot_be_started_has_no_version(tmp_path: Path) -> None:
    # Found by name, but not something the system can execute.
    broken = place_tool(tmp_path / "tools", TOOL)
    (info,) = isolated(tmp_path / "tools").describe([TOOL])
    assert (info.path, info.origin, info.version) == (broken, "tools-dir", None)
    assert info.available


def test_describe_of_a_tool_that_hangs_has_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = place_tool(tmp_path / "tools")
    commands: list[list[str]] = []

    def hang(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(toolbox.subprocess, "run", hang)
    (info,) = isolated(tmp_path / "tools").describe([TOOL])
    assert (info.path, info.version) == (tool, None)
    assert commands == [[str(tool), "-version"]]


def test_describe_lists_the_requested_tools_in_order(tmp_path: Path) -> None:
    program = runnable(write_program(tmp_path / "bin", "fake", stdout=("faketool 1.0",)))
    box = isolated(tmp_path / "none", **{OVERRIDE: str(program)})
    report = box.describe(["beaothertool", TOOL, "beathirdtool"])
    assert report == [
        ToolInfo("beaothertool", None, None, None),
        ToolInfo(TOOL, program, "env", "faketool 1.0"),
        ToolInfo("beathirdtool", None, None, None),
    ]
    assert box.describe([]) == []
    # The version belongs to the report only: the remembered location stays as it was.
    assert box.locate(TOOL).version is None


def test_describe_defaults_to_the_known_tools(tmp_path: Path) -> None:
    # docs/PROFILES.md: "Media rules need the external ffmpeg and ffprobe programs".
    assert {"ffmpeg", "ffprobe"} <= set(KNOWN_TOOLS)
    assert all(isinstance(purpose, str) and purpose for purpose in KNOWN_TOOLS.values())
    report = isolated(tmp_path / "tools").describe()
    assert [info.name for info in report] == list(KNOWN_TOOLS)
    assert all(info == ToolInfo(info.name, None, None, None) for info in report)
