"""Architecture guardrails (the rules of docs/ARCHITECTURE.md, enforced).

These tests keep three promises of the project:

1. **The core is game-agnostic.** Nothing under ``bea/`` names a game: no title IDs, no
   build IDs, no game names, no path into the profile data directory. Supporting a new
   game means adding YAML under the profile directory and nothing else.
2. **The repository is clean-room.** It holds source text only: no game files, no keys,
   no binary assets, and profiles are small declarative data.
3. **The layers stay layered.** Low-level modules never import high-level ones.

Every detector is a plain function over a directory, and each one is also run against a
temporary tree with a planted violation, so a rule can never pass vacuously.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "bea"
PROFILES = REPO / "profiles"

# ----------------------------------------------------------------------------------
# Detectors
# ----------------------------------------------------------------------------------

#: An application title ID: "0100" followed by twelve more hex digits.
TITLE_ID_RE = re.compile(r"0100[0-9A-Fa-f]{12}")
#: Anything long enough to be a build ID (16 bytes or more written in hex).
LONG_HEX_RE = re.compile(r"[0-9A-Fa-f]{32,}")
#: A path that reaches into the profile data directory.
PROFILE_PATH_RE = re.compile(r"profiles[/\\]")

#: Directories never inspected (not part of the repository's content).
SKIPPED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".gamedump",
    "work",
    "out",
    "tools",
    "dist",
    "build",
    "htmlcov",
    ".idea",
    ".vscode",
}

#: The only kinds of file the repository may contain: source text.
ALLOWED_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".typed",
    ".lock",
}
#: Extension-less (or dot-) files that are allowed by exact name.
ALLOWED_NAMES = {
    "LICENSE",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".python-version",
    ".gitkeep",
}
MAX_TEXT_FILE_BYTES = 512 * 1024
#: Generated lock files may exceed the cap.
UNCAPPED_NAMES = {"uv.lock"}

#: Profiles are data: these are the only files a profile directory may hold.
PROFILE_SUFFIXES = {".yaml", ".yml", ".md"}
MAX_PROFILE_FILE_BYTES = 256 * 1024

#: Patterns the clean-room shield in .gitignore must keep.
REQUIRED_IGNORES = [
    ".gamedump/",
    "work/",
    "out/",
    "tools/",
    "*.nsp",
    "*.nsz",
    "*.xci",
    "*.xcz",
    "*.nca",
    "*.tik",
    "*.cert",
    "prod.keys",
    "title.keys",
]

#: Third-party packages the core may import (must mirror pyproject's dependencies).
ALLOWED_THIRD_PARTY = {"PIL", "yaml", "lz4"}

#: Layering: which first-level members of ``bea`` each member may import.
FOUNDATION = {"errors", "hexutil", "fsutil", "globmatch"}
LAYERS: dict[str, set[str]] = {
    "errors": set(),
    "hexutil": set(),
    "fsutil": set(),
    "globmatch": set(),
    "model": FOUNDATION,
    "toolbox": FOUNDATION,
    "formats": FOUNDATION,
    "profile": FOUNDATION,
    "ingest": FOUNDATION | {"model", "formats"},
    "transformers": FOUNDATION | {"model", "toolbox", "formats", "profile"},
    "packagers": FOUNDATION | {"model"},
}
#: Members that sit on top and may import anything except the command line.
ORCHESTRATION_FORBIDDEN = {"cli", "__main__"}


def iter_source_files(root: Path, suffixes: Iterable[str] = (".py",)) -> Iterator[Path]:
    """Yield the files under ``root`` with one of ``suffixes``, skipping caches."""
    wanted = set(suffixes)
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in wanted and not SKIPPED_DIRS & set(path.parts):
            yield path


def find_pattern(
    root: Path, pattern: re.Pattern[str], *, squeeze_underscores: bool = False
) -> list[str]:
    """Return ``file:line: match`` for every occurrence of ``pattern`` in ``root``'s sources."""
    hits = []
    for path in iter_source_files(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # ``0x0100_0000_...`` is the same literal as ``0x01000000...``.
            text = (
                re.sub(r"(?<=[0-9A-Fa-f])_(?=[0-9A-Fa-f])", "", line)
                if squeeze_underscores
                else line
            )
            for match in pattern.finditer(text):
                hits.append(f"{path.relative_to(root).as_posix()}:{number}: {match.group(0)}")
    return hits


def find_title_ids(root: Path) -> list[str]:
    return find_pattern(root, TITLE_ID_RE, squeeze_underscores=True)


def find_long_hex(root: Path) -> list[str]:
    return find_pattern(root, LONG_HEX_RE, squeeze_underscores=True)


def find_profile_paths(root: Path) -> list[str]:
    return find_pattern(root, PROFILE_PATH_RE)


def imported_modules(path: Path) -> set[str]:
    """Return every module name imported by a Python file (absolute form)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module)
                # ``from bea import cli`` imports the member ``bea.cli``.
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def find_foreign_imports(root: Path, package: str = "bea") -> list[str]:
    """Return imports of anything that is not stdlib, the package itself or an allowed dependency."""
    problems = []
    for path in iter_source_files(root):
        for name in sorted(imported_modules(path)):
            top = name.lstrip(".").split(".")[0]
            if name.startswith("."):
                problems.append(f"{path.relative_to(root).as_posix()}: relative import {name!r}")
            elif (
                top not in sys.stdlib_module_names
                and top != package
                and top not in ALLOWED_THIRD_PARTY
            ):
                problems.append(f"{path.relative_to(root).as_posix()}: imports {name!r}")
    return problems


def member_of(path: Path, root: Path) -> str:
    """Return the first-level member of the package a file belongs to (``formats``, ``cli`` ...)."""
    relative = path.relative_to(root)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def find_layer_violations(root: Path, package: str = "bea") -> list[str]:
    """Return imports that go against the layering table."""
    problems = []
    for path in iter_source_files(root):
        member = member_of(path, root)
        if member == "__init__":
            continue
        for name in sorted(imported_modules(path)):
            parts = name.split(".")
            if parts[0] != package or len(parts) < 2:
                continue
            target = parts[1]
            if target == member or (
                not (root / target).exists() and not (root / f"{target}.py").exists()
            ):
                continue  # same member, or a name imported from the package (not a member)
            where = f"{path.relative_to(root).as_posix()}: imports {package}.{target}"
            if member in LAYERS:
                if target not in LAYERS[member]:
                    problems.append(
                        f"{where} (layer {member!r} may only use {sorted(LAYERS[member])})"
                    )
            elif member not in ORCHESTRATION_FORBIDDEN and target in ORCHESTRATION_FORBIDDEN:
                problems.append(f"{where} (only the command line may depend on the command line)")
    return problems


def repository_files(root: Path) -> list[Path]:
    """Return the files that are (or would be) part of the repository.

    Uses git when ``root`` is a work tree (tracked files plus untracked files that are
    not ignored); otherwise walks the directory, skipping well-known local folders.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        completed = None
    if completed is not None and (root / ".git").exists():
        names = [name for name in completed.stdout.decode("utf-8").split("\0") if name]
        return sorted(root / name for name in names if (root / name).is_file())
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not SKIPPED_DIRS & set(path.relative_to(root).parts)
    )


def find_foreign_files(root: Path, files: Iterable[Path] | None = None) -> list[str]:
    """Return every repository file that is not small UTF-8 source text of an allowed kind."""
    problems = []
    for path in repository_files(root) if files is None else files:
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
            problems.append(f"{relative}: file type not allowed in the repository")
            continue
        data = path.read_bytes()
        if len(data) > MAX_TEXT_FILE_BYTES and path.name not in UNCAPPED_NAMES:
            problems.append(f"{relative}: {len(data)} bytes is too large for a source file")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{relative}: not UTF-8 text")
        else:
            if b"\x00" in data:
                problems.append(f"{relative}: contains NUL bytes (binary data)")
    return problems


def profile_dirs(profiles_root: Path) -> list[Path]:
    """Return the per-game profile directories (templates start with an underscore)."""
    if not profiles_root.is_dir():
        return []
    return sorted(
        p for p in profiles_root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
    )


def find_profile_tree_problems(profiles_root: Path) -> list[str]:
    """Return violations of "profiles hold small declarative data only"."""
    problems = []
    if not profiles_root.is_dir():
        return [f"{profiles_root.name}: directory missing"]
    for path in sorted(profiles_root.rglob("*")):
        relative = path.relative_to(profiles_root).as_posix()
        if path.is_dir():
            continue
        if path.suffix.lower() not in PROFILE_SUFFIXES and path.name != ".gitkeep":
            problems.append(
                f"{relative}: only {sorted(PROFILE_SUFFIXES)} files may live in a profile"
            )
        elif path.stat().st_size > MAX_PROFILE_FILE_BYTES:
            problems.append(f"{relative}: larger than {MAX_PROFILE_FILE_BYTES} bytes")
    for directory in sorted(p for p in profiles_root.iterdir() if p.is_dir()):
        if directory.name.startswith(("_", ".")):
            continue
        if re.fullmatch(r"[0-9A-F]{16}", directory.name) is None:
            problems.append(
                f"{directory.name}: a profile directory is named after a title ID (16 uppercase hex digits)"
            )
        elif not (directory / "profile.yaml").is_file():
            problems.append(f"{directory.name}: profile.yaml missing")
    return problems


def declared_by_profiles(profiles_root: Path) -> set[str]:
    """Return the game-specific strings declared by the profiles: IDs, build IDs, names."""
    declared: set[str] = set()
    for directory in profile_dirs(profiles_root):
        declared.add(directory.name)
        for filename in ("profile.yaml", "patches.yaml"):
            path = directory / filename
            if not path.is_file():
                continue
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(document, dict):
                continue
            name = document.get("name")
            if isinstance(name, str) and len(name.strip()) >= 4:
                declared.add(name.strip())
            for build in document.get("builds") or []:
                if isinstance(build, dict) and isinstance(build.get("build_id"), str):
                    declared.add(build["build_id"].strip())
    return declared


def find_declared_strings(root: Path, declared: Iterable[str]) -> list[str]:
    """Return occurrences in ``root``'s sources of anything a profile declares."""
    needles = sorted(needle for needle in declared if needle)
    hits = []
    for path in iter_source_files(root):
        haystack = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            if needle.lower() in haystack:
                hits.append(f"{path.relative_to(root).as_posix()}: mentions {needle!r}")
    return hits


def missing_ignore_patterns(gitignore: Path) -> list[str]:
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    return [pattern for pattern in REQUIRED_IGNORES if pattern not in lines]


def report(problems: list[str]) -> str:
    return "\n  " + "\n  ".join(problems)


# ----------------------------------------------------------------------------------
# 1. The core is game-agnostic
# ----------------------------------------------------------------------------------


def test_core_exists_and_is_scanned() -> None:
    sources = list(iter_source_files(CORE))
    assert len(sources) >= 5, (
        "the scanner found almost nothing under bea/: the guardrails would be vacuous"
    )


def test_core_has_no_title_ids() -> None:
    hits = find_title_ids(CORE)
    assert not hits, "title IDs belong in profiles, never in bea/:" + report(hits)


def test_core_has_no_build_ids_or_long_hex_blobs() -> None:
    hits = find_long_hex(CORE)
    assert not hits, "build IDs and hex blobs belong in profiles, never in bea/:" + report(hits)


def test_core_does_not_reach_into_the_profile_directory() -> None:
    hits = find_profile_paths(CORE)
    assert not hits, (
        "bea/ must receive the profile directory as a parameter, not hard-code a path into it:"
        + report(hits)
    )


def test_core_mentions_nothing_a_profile_declares() -> None:
    hits = find_declared_strings(CORE, declared_by_profiles(PROFILES))
    assert not hits, "game names / IDs declared by a profile leaked into bea/:" + report(hits)


# ----------------------------------------------------------------------------------
# 2. The repository is clean-room
# ----------------------------------------------------------------------------------


def test_repository_holds_only_small_text_sources() -> None:
    problems = find_foreign_files(REPO)
    assert not problems, (
        "the repository must contain source text only (no game files, keys or binary assets):"
        + report(problems)
    )


def test_profiles_hold_only_small_declarative_data() -> None:
    problems = find_profile_tree_problems(PROFILES)
    assert not problems, report(problems)


def test_every_profile_loads_and_matches_its_directory() -> None:
    from bea.profile.loader import load_profile

    for directory in profile_dirs(PROFILES):
        profile = load_profile(directory)
        assert profile.title_id == directory.name, (
            f"{directory.name}: title_id says {profile.title_id}"
        )


def test_gitignore_keeps_the_clean_room_shield() -> None:
    missing = missing_ignore_patterns(REPO / ".gitignore")
    assert not missing, f".gitignore lost protective patterns: {missing}"


def test_documented_layout_exists() -> None:
    expected = [
        "LICENSE",
        "README.md",
        "DISCLAIMER.md",
        "CLAUDE.md",
        "AGENTS.md",
        "pyproject.toml",
        "docs/ARCHITECTURE.md",
        "bea/cli.py",
        "bea/pipeline.py",
        "bea/ingest",
        "bea/formats",
        "bea/transformers",
        "bea/packagers",
        "profiles",
        "tests",
    ]
    missing = [name for name in expected if not (REPO / name).exists()]
    assert not missing, f"documented layout entries are missing: {missing}"


# ----------------------------------------------------------------------------------
# 3. The layers stay layered
# ----------------------------------------------------------------------------------


def test_core_imports_only_declared_dependencies() -> None:
    problems = find_foreign_imports(CORE)
    assert not problems, (
        "bea/ may import the standard library, itself and its declared dependencies only:"
        + report(problems)
    )


def test_allowed_third_party_matches_pyproject() -> None:
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        re.split(r"[<>=!~ \[;]", requirement, maxsplit=1)[0].lower()
        for requirement in project["dependencies"]
    }
    import_name = {"pillow": "PIL", "pyyaml": "yaml", "lz4": "lz4"}
    assert {import_name.get(name, name) for name in declared} == ALLOWED_THIRD_PARTY


def test_layers_only_depend_downwards() -> None:
    problems = find_layer_violations(CORE)
    assert not problems, report(problems)


# ----------------------------------------------------------------------------------
# The detectors detect (each rule is exercised against a planted violation)
# ----------------------------------------------------------------------------------


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _synthetic_title_id() -> str:
    # Built at run time so that this file itself stays free of title IDs.
    return f"{(0x0100 << 48) | 0xBEA0BEA0B000:016X}"


def test_detector_title_ids(tmp_path: Path) -> None:
    title_id = _synthetic_title_id()
    _write(tmp_path, "clean.py", "PREFIX = 0x0100 << 48\nVALUE = 0x0100\n")
    assert find_title_ids(tmp_path) == []
    _write(tmp_path, "pkg/leak.py", f'GAME = "{title_id}"\n')
    _write(tmp_path, "pkg/lower.py", f"x = 0x{title_id.lower()}\n")
    _write(
        tmp_path,
        "pkg/split.py",
        f"x = 0x{title_id[:4]}_{title_id[4:8]}_{title_id[8:12]}_{title_id[12:]}\n",
    )
    hits = find_title_ids(tmp_path)
    assert len(hits) == 3
    assert hits[0].startswith("pkg/leak.py:1:")


def test_detector_long_hex(tmp_path: Path) -> None:
    _write(tmp_path, "clean.py", 'MAGIC = b"NSO0"\nSIZE = 0x100\nSHORT = "DEADBEEFCAFEF00D"\n')
    assert find_long_hex(tmp_path) == []
    _write(tmp_path, "leak.py", f'BUILD = "{"AB" * 20}"\n')
    assert len(find_long_hex(tmp_path)) == 1


def test_detector_profile_paths(tmp_path: Path) -> None:
    _write(tmp_path, "clean.py", 'DIRNAME = "profiles"\nfrom bea.profile import schema\n')
    assert find_profile_paths(tmp_path) == []
    _write(tmp_path, "leak.py", 'path = "profiles/" + title\nother = r"profiles\\x"\n')
    assert len(find_profile_paths(tmp_path)) == 2


def test_detector_declared_strings(tmp_path: Path) -> None:
    title_id = _synthetic_title_id()
    profiles = tmp_path / "profiles"
    _write(
        profiles,
        f"{title_id}/profile.yaml",
        f'schema: 1\ntitle_id: "{title_id}"\nname: "Synthetic Quest"\n',
    )
    _write(
        profiles,
        f"{title_id}/patches.yaml",
        f'schema: 1\ntitle_id: "{title_id}"\nbuilds:\n  - build_id: "{"C0FFEE" * 4}"\n    patches: []\n',
    )
    _write(profiles, "_template/profile.yaml", 'name: "Template Name"\n')
    declared = declared_by_profiles(profiles)
    assert declared == {title_id, "Synthetic Quest", "C0FFEE" * 4}

    core = tmp_path / "core"
    _write(core, "clean.py", "x = 1\n")
    assert find_declared_strings(core, declared) == []
    _write(core, "leak.py", 'if name == "synthetic quest":\n    pass\n')
    assert len(find_declared_strings(core, declared)) == 1


def test_detector_foreign_files(tmp_path: Path) -> None:
    _write(tmp_path, "src/ok.py", "x = 1\n")
    _write(tmp_path, "docs/ok.md", "# ok\n")
    _write(tmp_path, "LICENSE", "MIT\n")
    assert find_foreign_files(tmp_path) == []

    (tmp_path / "game.nsp").write_bytes(b"PFS0")
    (tmp_path / "prod.keys").write_text("header_key = 00\n", encoding="utf-8")
    (tmp_path / "texture.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "blob.txt").write_bytes(b"\xff\xfe\x00binary")
    (tmp_path / "nul.md").write_bytes(b"text\x00more")
    (tmp_path / "huge.json").write_text("x" * (MAX_TEXT_FILE_BYTES + 1), encoding="utf-8")
    problems = find_foreign_files(tmp_path)
    flagged = {problem.split(":")[0] for problem in problems}
    assert flagged == {"game.nsp", "prod.keys", "texture.png", "blob.txt", "nul.md", "huge.json"}


def test_detector_profile_tree(tmp_path: Path) -> None:
    title_id = _synthetic_title_id()
    profiles = tmp_path / "profiles"
    _write(profiles, f"{title_id}/profile.yaml", "schema: 1\n")
    _write(profiles, f"{title_id}/NOTES.md", "notes\n")
    _write(profiles, "_template/profile.yaml", "schema: 1\n")
    _write(profiles, "README.md", "how to add a game\n")
    assert find_profile_tree_problems(profiles) == []

    _write(profiles, "not-a-title-id/profile.yaml", "schema: 1\n")
    _write(profiles, f"{title_id}/patch.py", "print('code is not data')\n")
    (profiles / title_id / "main.ips").write_bytes(b"PATCHEOF")
    _write(profiles, f"{title_id[:-1]}1/readme.md", "no profile.yaml here\n")
    _write(profiles, f"{title_id}/big.yaml", "a: " + "x" * MAX_PROFILE_FILE_BYTES + "\n")
    problems = find_profile_tree_problems(profiles)
    assert len(problems) == 5, problems
    assert find_profile_tree_problems(tmp_path / "missing") != []


def test_detector_foreign_imports_and_layers(tmp_path: Path) -> None:
    core = tmp_path / "bea"
    _write(core, "errors.py", "import os\n")
    _write(core, "formats/__init__.py", "")
    _write(
        core, "formats/thing.py", "import struct\nfrom bea.errors import X\nfrom PIL import Image\n"
    )
    _write(core, "transformers/__init__.py", "")
    _write(core, "transformers/tex.py", "from bea.formats import thing\nfrom bea import errors\n")
    _write(
        core, "pipeline.py", "from bea.transformers import tex\nfrom bea.formats.thing import Y\n"
    )
    _write(core, "cli.py", "import argparse\nfrom bea import pipeline\n")
    assert find_foreign_imports(core) == []
    assert find_layer_violations(core) == []

    _write(
        core,
        "formats/bad.py",
        "import requests\nfrom . import thing\nfrom bea.transformers import tex\n",
    )
    _write(core, "errors.py", "from bea import fsutil\n")
    _write(core, "fsutil.py", "import os\n")
    _write(core, "pipeline.py", "from bea import cli\n")
    foreign = find_foreign_imports(core)
    assert any("requests" in problem for problem in foreign)
    assert any("relative import" in problem for problem in foreign)
    layers = find_layer_violations(core)
    assert any(problem.startswith("formats/bad.py: imports bea.transformers") for problem in layers)
    assert any(problem.startswith("errors.py: imports bea.fsutil") for problem in layers)
    assert any(problem.startswith("pipeline.py: imports bea.cli") for problem in layers)


def test_detector_gitignore(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("\n".join(REQUIRED_IGNORES) + "\n", encoding="utf-8")
    assert missing_ignore_patterns(gitignore) == []
    gitignore.write_text(
        "\n".join(p for p in REQUIRED_IGNORES if p != "prod.keys") + "\n", encoding="utf-8"
    )
    assert missing_ignore_patterns(gitignore) == ["prod.keys"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0100ABCDEF123000", True),
        ("0100abcdef123000", True),
        ("x0100000000001000y", True),
        ("0100ABCDEF12300", False),
        ("0x0100 << 48", False),
        ("0200ABCDEF123000", False),
    ],
)
def test_title_id_pattern(text: str, expected: bool) -> None:
    assert (TITLE_ID_RE.search(text) is not None) is expected


# ----------------------------------------------------------------------------------
# 4. Profiles in the repository hold no byte of a game, and say what each patch is for
# ----------------------------------------------------------------------------------

#: A reference to a key file. The engine never reads keys, so it never names them.
#: (``name.keys`` followed by ``(`` is a method call such as ``mapping.keys()``, not a file.)
KEY_FILE_RE = re.compile(
    r"(?:prod|title|dev|console)\.keys\b|\bkeyset\b|\w\.keys\b(?!\s*\()", re.IGNORECASE
)


def find_key_file_references(root: Path) -> list[str]:
    return find_pattern(root, KEY_FILE_RE)


def find_profile_patch_problems(profiles_root: Path) -> list[str]:
    """Return patches without a category and hunks verified by literal game bytes."""
    problems = []
    for directory in profile_dirs(profiles_root):
        path = directory / "patches.yaml"
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        builds = document.get("builds") if isinstance(document, dict) else None
        for build_index, build in enumerate(builds or []):
            patches = build.get("patches") if isinstance(build, dict) else None
            for patch_index, patch in enumerate(patches or []):
                if not isinstance(patch, dict):
                    continue
                where = f"{directory.name}: builds[{build_index}].patches[{patch_index}]"
                if not patch.get("category"):
                    problems.append(f"{where}: every patch in the repository states its category")
                for hunk_index, hunk in enumerate(patch.get("hunks") or []):
                    if isinstance(hunk, dict) and "expected_bytes" in hunk:
                        problems.append(
                            f"{where}.hunks[{hunk_index}]: expected_bytes copies bytes of the game; "
                            "use expected_sha256 (bea nso hunk prints it)"
                        )
    return problems


def test_core_never_names_key_files() -> None:
    hits = find_key_file_references(CORE)
    assert not hits, "BEA has no code that reads keys, so bea/ never names key files:" + report(
        hits
    )


def test_repository_profiles_hold_no_game_bytes_and_state_categories() -> None:
    problems = find_profile_patch_problems(PROFILES)
    assert not problems, report(problems)


def test_detector_key_files(tmp_path: Path) -> None:
    _write(tmp_path, "clean.py", 'KEYS = ("a", "b")\nmonkeys = 1\nkeys_of_dict = {}.keys()\n')
    assert find_key_file_references(tmp_path) == []
    _write(tmp_path, "leak.py", 'PATH = "prod.keys"\nother = "Title.Keys"\nmine = "my.keys"\n')
    assert len(find_key_file_references(tmp_path)) == 3


def test_detector_profile_patches(tmp_path: Path) -> None:
    title_id = _synthetic_title_id()
    profiles = tmp_path / "profiles"
    good = (
        f'schema: 1\ntitle_id: "{title_id}"\nbuilds:\n  - build_id: "AB"\n    patches:\n'
        "      - name: p\n        category: framerate\n        hunks:\n"
        '          - {offset: 0, patch_bytes: "00", expected_sha256: "' + "0" * 64 + '"}\n'
    )
    _write(profiles, f"{title_id}/patches.yaml", good)
    _write(
        profiles, "_template/patches.yaml", good.replace("category: framerate", "description: x")
    )
    assert find_profile_patch_problems(profiles) == []

    bad = good.replace("        category: framerate\n", "").replace(
        'expected_sha256: "' + "0" * 64 + '"', 'expected_bytes: "11"'
    )
    _write(profiles, f"{title_id}/patches.yaml", bad)
    problems = find_profile_patch_problems(profiles)
    assert len(problems) == 2
    assert "category" in problems[0] and "expected_bytes" in problems[1]
