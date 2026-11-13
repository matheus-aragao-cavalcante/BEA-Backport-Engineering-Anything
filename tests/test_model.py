"""Tests for bea.model: logical paths and the plain data types shared by every stage."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from bea import model
from bea.model import (
    EXEFS_PATCHES_PREFIX,
    EXEFS_PREFIX,
    ROMFS_PREFIX,
    FileEntry,
    GameInput,
    ModuleInfo,
    SegmentInfo,
    TitleInfo,
    split_logical,
)


def make_module(name: str, exefs_dir: Path, *, build_id: str = "AB00CD") -> ModuleInfo:
    """Build a synthetic module description (no file is needed behind it)."""
    segments = (
        SegmentInfo(name="text", memory_offset=0, size=0x2000, file_size=0x1000, compressed=True),
        SegmentInfo(
            name="rodata", memory_offset=0x2000, size=0x1000, file_size=0x1000, compressed=False
        ),
        SegmentInfo(
            name="data", memory_offset=0x3000, size=0x800, file_size=0x200, compressed=True
        ),
    )
    return ModuleInfo(
        name=name,
        path=exefs_dir / name,
        size=0x2300,
        build_id=build_id,
        segments=segments,
        bss_size=0x400,
    )


def make_game(
    root: Path,
    *,
    romfs: bool = True,
    exefs: bool = True,
    modules: tuple[str, ...] = ("rtld", "main", "sdk"),
) -> GameInput:
    """Build a synthetic indexed game folder under ``root`` (directories are not created)."""
    exefs_dir = root / "ExeFS" if exefs else None
    return GameInput(
        root=root,
        exefs_dir=exefs_dir,
        romfs_dir=root / "RomFS" if romfs else None,
        nacp_path=None,
        title=TitleInfo(),
        modules=tuple(make_module(name, root / "ExeFS") for name in modules) if exefs else (),
        exefs_files=(*sorted(modules), "main.npdm") if exefs else (),
        romfs_files=(
            FileEntry("data/Level1.pak", 1024, 1_700_000_000_000_000_000),
            FileEntry("ui/icons/OK.PNG", 77, 1_700_000_000_000_000_001),
        )
        if romfs
        else (),
    )


# ----------------------------------------------------------------------------------
# Logical paths
# ----------------------------------------------------------------------------------


def test_tree_prefixes() -> None:
    assert (ROMFS_PREFIX, EXEFS_PREFIX, EXEFS_PATCHES_PREFIX) == (
        "romfs/",
        "exefs/",
        "exefs_patches/",
    )


@pytest.mark.parametrize(
    ("logical", "expected"),
    [
        ("romfs/a.bin", ("romfs", "a.bin")),
        ("romfs/ui/icons/ok.png", ("romfs", "ui/icons/ok.png")),
        # The remainder is returned verbatim: original case, dots and spaces included.
        ("romfs/UI/Icons/OK.PNG", ("romfs", "UI/Icons/OK.PNG")),
        ("romfs/My Data/file v1.2.bin", ("romfs", "My Data/file v1.2.bin")),
        # A RomFS may contain folders named like the trees themselves.
        ("romfs/exefs/main", ("romfs", "exefs/main")),
        ("romfs/romfs/x", ("romfs", "romfs/x")),
        ("exefs/main", ("exefs", "main")),
        ("exefs/main.npdm", ("exefs", "main.npdm")),
        ("exefs/subsdk0", ("exefs", "subsdk0")),
        # "exefs_patches" must not be mistaken for "exefs".
        ("exefs_patches/AB00CD.ips", ("exefs_patches", "AB00CD.ips")),
        ("exefs_patches/name/AB00CD.ips", ("exefs_patches", "name/AB00CD.ips")),
    ],
)
def test_split_logical(logical: str, expected: tuple[str, str]) -> None:
    assert split_logical(logical) == expected
    # Splitting loses nothing: tree and remainder rebuild the logical path.
    assert "/".join(split_logical(logical)) == logical


@pytest.mark.parametrize(
    "logical",
    [
        "",
        "/",
        # A tree without a remainder names no file.
        "romfs",
        "romfs/",
        "exefs",
        "exefs/",
        "exefs_patches",
        "exefs_patches/",
        # Unknown trees, including near misses.
        "data/a.bin",
        "a.bin",
        "romfsx/a.bin",
        "romf/a.bin",
        "exefs_patch/a.ips",
        "exefs_patchesx/a.ips",
        "exefs-patches/a.ips",
        # The prefix is exact: no leading separator, no other case, no other separator.
        "/romfs/a.bin",
        "./romfs/a.bin",
        " romfs/a.bin",
        "ROMFS/a.bin",
        "RomFS/a.bin",
        "ExeFS/main",
        "romfs\\a.bin",
    ],
)
def test_split_logical_rejects_what_is_not_a_logical_path(logical: str) -> None:
    with pytest.raises(ValueError, match="not a logical path"):
        split_logical(logical)


# ----------------------------------------------------------------------------------
# FileEntry
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "name", "ext"),
    [
        ("a.png", "a.png", ".png"),
        ("ui/icons/ok.png", "ok.png", ".png"),
        # Upper-case extensions are folded, the name is not.
        ("ui/icons/OK.PNG", "OK.PNG", ".png"),
        ("Textures/Rock_D.DdS", "Rock_D.DdS", ".dds"),
        # Only the last extension counts.
        ("data/archive.tar.GZ", "archive.tar.GZ", ".gz"),
        ("a..b", "a..b", ".b"),
        # No extension.
        ("README", "README", ""),
        ("exe/main", "main", ""),
        # A dot in a directory name is not an extension of the file.
        ("dir.with.dots/file", "file", ""),
        ("v1.2/readme.TXT", "readme.TXT", ".txt"),
        # Dotfiles: the leading dot starts the name, not an extension.
        (".hidden", ".hidden", ""),
        ("docs/.gitignore", ".gitignore", ""),
        ("docs/.config.YML", ".config.YML", ".yml"),
        ("..a", "..a", ""),
        ("x/..hidden.txt", "..hidden.txt", ".txt"),
        # A name that ends in a dot has no extension (and never a bare ".").
        ("trailing/dot.", "dot.", ""),
        ("a.b.", "a.b.", ""),
        ("x/.a.", ".a.", ""),
        ("...", "...", ""),
        # Spaces and other punctuation are part of the name.
        ("My Data/file v1.2 (final).BIN", "file v1.2 (final).BIN", ".bin"),
    ],
)
def test_file_entry_name_and_ext(path: str, name: str, ext: str) -> None:
    entry = FileEntry(path=path, size=1, mtime_ns=2)
    assert entry.name == name
    assert entry.ext == ext
    assert entry.logical == "romfs/" + path
    # The logical path leads back to the entry.
    assert split_logical(entry.logical) == ("romfs", path)


def test_file_entry_is_a_plain_frozen_value() -> None:
    entry = FileEntry("ui/a.png", 10, 20)
    assert (entry.path, entry.size, entry.mtime_ns) == ("ui/a.png", 10, 20)
    assert entry == FileEntry("ui/a.png", 10, 20)
    assert entry != FileEntry("ui/a.png", 10, 21)
    assert hash(entry) == hash(FileEntry("ui/a.png", 10, 20))
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.size = 11  # type: ignore[misc]


# ----------------------------------------------------------------------------------
# ModuleInfo, SegmentInfo, TitleInfo
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["main", "rtld", "sdk", "subsdk0"])
def test_module_logical_path(tmp_path: Path, name: str) -> None:
    module = make_module(name, tmp_path)
    assert module.logical == "exefs/" + name
    assert split_logical(module.logical) == ("exefs", name)
    assert module.path == tmp_path / name
    assert [segment.name for segment in module.segments] == ["text", "rodata", "data"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        module.build_id = "00"  # type: ignore[misc]


def test_title_info_defaults_to_unknown() -> None:
    title = TitleInfo()
    assert (title.title_id, title.name, title.publisher, title.display_version) == (
        None,
        None,
        None,
        None,
    )
    assert title.sources == {}
    # Every instance gets its own mapping.
    assert TitleInfo().sources is not title.sources
    named = TitleInfo(name="Synthetic Quest", sources={"name": "nacp"})
    assert named.sources == {"name": "nacp"}
    assert named == TitleInfo(name="Synthetic Quest", sources={"name": "nacp"})


def test_every_public_name_is_exported() -> None:
    for name in model.__all__:
        assert hasattr(model, name), name
    defined = {
        name
        for name, value in vars(model).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == model.__name__
    }
    assert defined <= set(model.__all__)


# ----------------------------------------------------------------------------------
# GameInput
# ----------------------------------------------------------------------------------


def test_game_input_module_lookup(tmp_path: Path) -> None:
    game = make_game(tmp_path)
    main = game.module("main")
    assert main is not None and main.name == "main"
    assert main is game.modules[1]
    assert game.module("rtld") is game.modules[0]
    assert game.module("subsdk0") is None
    assert game.module("") is None
    # Module names are ExeFS file names: exact, case included.
    assert game.module("Main") is None
    assert game.module("main.npdm") is None  # present in the ExeFS, but not a module
    assert make_game(tmp_path, exefs=False).module("main") is None
    assert game.warnings == ()


def test_game_input_resolves_input_paths(tmp_path: Path) -> None:
    game = make_game(tmp_path)
    assert game.resolve("romfs/data/Level1.pak") == tmp_path / "RomFS" / "data" / "Level1.pak"
    assert game.resolve("romfs/top.bin") == tmp_path / "RomFS" / "top.bin"
    assert game.resolve("exefs/main") == tmp_path / "ExeFS" / "main"
    assert game.resolve("exefs/main.npdm") == tmp_path / "ExeFS" / "main.npdm"
    # A RomFS folder named like a tree stays inside the RomFS.
    assert game.resolve("romfs/exefs/main") == tmp_path / "RomFS" / "exefs" / "main"
    # What the index lists is what resolve() opens.
    for entry in game.romfs_files:
        assert game.resolve(entry.logical) == (tmp_path / "RomFS").joinpath(*entry.path.split("/"))
    for module in game.modules:
        assert game.resolve(module.logical) == module.path


def test_game_input_resolve_reaches_the_file_on_disk(tmp_path: Path) -> None:
    game = make_game(tmp_path)
    target = tmp_path / "RomFS" / "ui" / "icons"
    target.mkdir(parents=True)
    (target / "OK.PNG").write_bytes(b"synthetic")
    assert game.resolve("romfs/ui/icons/OK.PNG").read_bytes() == b"synthetic"


@pytest.mark.parametrize(
    "logical",
    [
        # Climbing out of the tree.
        "romfs/../ExeFS/main",
        "romfs/a/../../b",
        "romfs/..",
        "exefs/../RomFS/a.bin",
        "exefs/../../outside",
        # Absolute and drive-qualified remainders.
        "romfs//etc/passwd",
        "romfs/C:/Windows/system.ini",
        "romfs/a/C:/Windows/system.ini",
        "romfs/a/D:evil",
        "exefs/C:main",
        # Segments that are not names.
        "romfs/a//b",
        "romfs/./a",
        "romfs/a/.",
        "romfs/a/",
        "romfs/a\\..\\b",
        "romfs/a\x00b",
    ],
)
def test_game_input_resolve_refuses_to_leave_the_tree(tmp_path: Path, logical: str) -> None:
    game = make_game(tmp_path)
    with pytest.raises(ValueError):
        game.resolve(logical)


@pytest.mark.parametrize(
    "logical",
    ["", "romfs", "romfs/", "exefs/", "data/a.bin", "RomFS/a.bin", "/romfs/a.bin"],
)
def test_game_input_resolve_rejects_non_logical_paths(tmp_path: Path, logical: str) -> None:
    with pytest.raises(ValueError, match="not a logical path"):
        make_game(tmp_path).resolve(logical)


def test_game_input_resolve_only_knows_the_input_trees(tmp_path: Path) -> None:
    # Generated patches are an output: there is nothing to read for them in the input.
    with pytest.raises(ValueError, match=re.escape("exefs_patches/AB00CD.ips")):
        make_game(tmp_path).resolve("exefs_patches/AB00CD.ips")


def test_game_input_resolve_with_missing_trees(tmp_path: Path) -> None:
    only_romfs = make_game(tmp_path, exefs=False)
    assert only_romfs.resolve("romfs/a.bin") == tmp_path / "RomFS" / "a.bin"
    with pytest.raises(ValueError, match="exefs/main"):
        only_romfs.resolve("exefs/main")

    only_exefs = make_game(tmp_path, romfs=False)
    assert only_exefs.resolve("exefs/main") == tmp_path / "ExeFS" / "main"
    with pytest.raises(ValueError, match=r"romfs/a\.bin"):
        only_exefs.resolve("romfs/a.bin")


def test_game_input_is_frozen(tmp_path: Path) -> None:
    game = make_game(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        game.romfs_dir = None  # type: ignore[misc]
    replaced = dataclasses.replace(game, warnings=("no control.nacp found",))
    assert replaced.warnings == ("no control.nacp found",)
    assert game.warnings == ()
