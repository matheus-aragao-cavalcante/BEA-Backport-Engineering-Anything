"""Tests for bea.ingest: from an extracted game folder to a GameInput."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bea.errors import InputError, UnsupportedInputError
from bea.formats.nacp import build_nacp
from bea.formats.npdm import build_npdm
from bea.formats.nso import build_nso
from bea.ingest import MODULE_ORDER, open_input

#: A synthetic application ID (built at run time: no ID literal in the repository).
APP = (0x0100 << 48) | 0xABCDE0012000
APP_TEXT = f"{APP:016X}"
OTHER = APP + 0x4000


def module_id(seed: int) -> bytes:
    return bytes((seed * 7 + index) % 256 for index in range(20))


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def nso(seed: int = 1) -> bytes:
    return build_nso(bytes(range(64)) * 4, b"rodata" * 10, b"data" * 8, module_id=module_id(seed))


def make_game(
    root: Path,
    *,
    exefs: str | None = "ExeFS",
    romfs: str | None = "RomFS",
    modules: tuple[str, ...] = ("main",),
    npdm: int | None = APP,
    nacp: bool = True,
) -> Path:
    if exefs is not None:
        exefs_dir = root / exefs if exefs else root
        for index, name in enumerate(modules):
            write(exefs_dir / name, nso(index + 1))
        if npdm is not None:
            write(exefs_dir / "main.npdm", build_npdm(npdm))
    if romfs is not None:
        write(root / romfs / "ui" / "title.png", b"not really a png")
        write(root / romfs / "config.ini", b"[a]\nb=1\n")
    if nacp:
        titles = {
            0: ("Synthetic Game", "Example Publisher"),
            2: ("シンセティック", "パブリッシャー"),
        }
        write(
            root / "control.nacp", build_nacp(titles, display_version="1.2.3", application_id=APP)
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_standard_layout(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", modules=("sdk", "main", "subsdk1", "rtld", "subsdk0"))
    write(root / "ExeFS" / "readme.txt", b"not a module")
    game = open_input(root)

    assert (
        game.root == root and game.exefs_dir == root / "ExeFS" and game.romfs_dir == root / "RomFS"
    )
    assert game.nacp_path == root / "control.nacp"
    assert [module.name for module in game.modules] == ["rtld", "main", "subsdk0", "subsdk1", "sdk"]
    assert game.exefs_files == (
        "main",
        "main.npdm",
        "readme.txt",
        "rtld",
        "sdk",
        "subsdk0",
        "subsdk1",
    )

    main = game.module("main")
    assert main is not None
    assert main.build_id == module_id(2).hex().upper()  # main was the second module written
    assert main.path == root / "ExeFS" / "main"
    assert main.size == (root / "ExeFS" / "main").stat().st_size
    assert [segment.name for segment in main.segments] == ["text", "rodata", "data"]
    assert main.segments[0].size == 256 and main.segments[1].memory_offset == 0x1000

    title = game.title
    assert (title.title_id, title.name, title.publisher, title.display_version) == (
        APP_TEXT,
        "Synthetic Game",
        "Example Publisher",
        "1.2.3",
    )
    assert dict(title.sources) == {
        "title_id": "npdm",
        "name": "nacp",
        "publisher": "nacp",
        "display_version": "nacp",
    }
    assert [entry.path for entry in game.romfs_files] == ["config.ini", "ui/title.png"]
    assert game.romfs_files[0].size == len(b"[a]\nb=1\n")
    assert game.resolve("romfs/ui/title.png").read_bytes() == b"not really a png"
    assert game.warnings == ()


@pytest.mark.parametrize(
    ("exefs", "romfs"), [("exefs", "romfs"), ("0", "1"), ("code", "data"), ("EXEFS", "Romfs")]
)
def test_folder_names_are_matched_without_case(tmp_path: Path, exefs: str, romfs: str) -> None:
    game = open_input(make_game(tmp_path / "game", exefs=exefs, romfs=romfs))
    assert game.exefs_dir is not None and game.exefs_dir.name == exefs
    assert game.romfs_dir is not None and game.romfs_dir.name == romfs


def test_exefs_is_found_by_content_not_by_name(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", exefs="whatever")
    assert open_input(root).exefs_dir == root / "whatever"
    # A folder called "exefs" without a main module does not count.
    write(root / "exefs" / "notes.txt", b"x")
    assert open_input(root).exefs_dir == root / "whatever"


def test_the_folder_itself_can_be_the_exefs(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", exefs="", romfs=None, nacp=False)
    game = open_input(root)
    assert game.exefs_dir == root and game.romfs_dir is None
    assert game.title.title_id == APP_TEXT
    assert any("no RomFS" in warning for warning in game.warnings)


def test_romfs_only_with_an_explicit_title_id(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", exefs=None, nacp=False)
    game = open_input(root, title_id=APP_TEXT.lower())
    assert game.exefs_dir is None and game.modules == ()
    assert game.title.title_id == APP_TEXT and game.title.sources["title_id"] == "option"
    assert any("no ExeFS" in warning for warning in game.warnings)

    unknown = open_input(root)
    assert unknown.title.title_id is None
    assert any("--title-id" in warning for warning in unknown.warnings)


def test_explicit_trees(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", exefs="code-here", romfs="assets-here")
    game = open_input(root, exefs_dir=root / "code-here", romfs_dir=root / "assets-here")
    assert (game.exefs_dir, game.romfs_dir) == (root / "code-here", root / "assets-here")
    with pytest.raises(InputError, match="--romfs"):
        open_input(root, romfs_dir=root / "missing")


def test_ambiguous_exefs_folders(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", exefs="first")
    write(root / "second" / "main", nso(9))
    with pytest.raises(InputError, match="--exefs"):
        open_input(root)


def test_nothing_found(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(InputError, match="no ExeFS or RomFS"):
        open_input(tmp_path / "empty")
    with pytest.raises(InputError, match="does not exist"):
        open_input(tmp_path / "missing")


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("game.nsp", b"anything at all"),
        ("game.XCI", b""),
        ("game.bin", b"PFS0" + bytes(12)),
        ("card.bin", bytes(0x100) + b"HEAD" + bytes(16)),
        ("x.nsz", b"PFS0"),
    ],
)
def test_packaged_dumps_are_refused(tmp_path: Path, name: str, data: bytes) -> None:
    path = write(tmp_path / name, data)
    with pytest.raises(UnsupportedInputError) as raised:
        open_input(path)
    message = str(raised.value)
    assert "already" in message and "extracted" in message and "decrypt" in message


def test_a_plain_file_is_not_a_game_folder(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="folder") as raised:
        open_input(write(tmp_path / "notes.txt", b"hello"))
    assert not isinstance(raised.value, UnsupportedInputError)


def test_unreadable_and_missing_modules_are_warnings(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", modules=("sdk",))
    write(root / "ExeFS" / "subsdk0", b"NSO0" + bytes(8))  # header cut short
    game = open_input(root)
    assert [module.name for module in game.modules] == ["sdk"]
    assert any(warning.startswith("module subsdk0 cannot be read") for warning in game.warnings)
    assert any("no readable 'main' module" in warning for warning in game.warnings)


def test_module_order_constant() -> None:
    assert MODULE_ORDER[:3] == ("rtld", "main", "subsdk0") and MODULE_ORDER[-1] == "sdk"
    assert len(MODULE_ORDER) == 13  # rtld, main, subsdk0..subsdk9, sdk


def test_unknown_modules_sort_after_known_ones(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", modules=("zeta", "main", "alpha", "sdk"))
    assert [module.name for module in open_input(root).modules] == ["main", "sdk", "alpha", "zeta"]


def test_title_id_priority_and_disagreement(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    other = f"{OTHER:016X}"
    game = open_input(root, title_id=other)
    assert game.title.title_id == other and game.title.sources["title_id"] == "option"
    assert any(APP_TEXT in warning and other in warning for warning in game.warnings)


def test_title_id_inferred_from_nacp_without_npdm(tmp_path: Path) -> None:
    game = open_input(make_game(tmp_path / "game", npdm=None))
    assert game.title.title_id == APP_TEXT
    assert game.title.sources["title_id"] == "nacp (inferred)"


@pytest.mark.parametrize("folder", ["Game [{id}]", "{id}", "dumps/{id}/v0"])
def test_title_id_from_the_path(tmp_path: Path, folder: str) -> None:
    root = make_game(tmp_path / folder.format(id=APP_TEXT), npdm=None, nacp=False)
    if folder.endswith("v0"):
        root = tmp_path / folder.format(id=APP_TEXT)
    game = open_input(root)
    assert game.title.title_id == APP_TEXT and game.title.sources["title_id"] == "path"


def test_seventeen_hex_digits_are_not_a_title_id(tmp_path: Path) -> None:
    root = make_game(tmp_path / f"{APP_TEXT}0", npdm=None, nacp=False)
    assert open_input(root).title.title_id is None


def test_an_update_id_becomes_the_base_program_id(tmp_path: Path) -> None:
    game = open_input(make_game(tmp_path / "game", npdm=APP + 0x800, nacp=False))
    assert game.title.title_id == APP_TEXT
    assert any("update" in warning for warning in game.warnings)


def test_a_program_index_id_is_kept(tmp_path: Path) -> None:
    game = open_input(make_game(tmp_path / "game", npdm=APP + 1, nacp=False))
    assert game.title.title_id == f"{APP + 1:016X}"


@pytest.mark.parametrize(
    "location",
    ["control.nacp", "ExeFS/control.nacp", "ExeFS/other.nacp", "Control/control.nacp", "zz.NACP"],
)
def test_nacp_locations(tmp_path: Path, location: str) -> None:
    root = make_game(tmp_path / "game", nacp=False)
    write(root / location, build_nacp({0: ("Found It", "P")}, display_version="9"))
    game = open_input(root)
    assert game.nacp_path == root / location
    assert (game.title.name, game.title.display_version) == ("Found It", "9")


def test_a_corrupt_nacp_is_skipped_for_the_next_one(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", nacp=False)
    write(root / "control.nacp", b"too short")
    write(root / "ExeFS" / "control.nacp", build_nacp({0: ("Second", "P")}))
    game = open_input(root)
    assert game.nacp_path == root / "ExeFS" / "control.nacp" and game.title.name == "Second"
    assert any(warning.startswith("control.nacp cannot be read") for warning in game.warnings)


def test_no_nacp_is_not_a_warning(tmp_path: Path) -> None:
    game = open_input(make_game(tmp_path / "game", nacp=False))
    assert game.nacp_path is None and game.title.name is None
    assert game.warnings == ()


def test_corrupt_npdm_is_a_warning(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", npdm=None, nacp=False)
    write(root / "ExeFS" / "main.npdm", b"META" + bytes(4))
    game = open_input(root)
    assert any(warning.startswith("main.npdm cannot be read") for warning in game.warnings)
    assert game.title.title_id is None


def test_32_bit_programs_are_reported(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", npdm=None, nacp=False)
    write(root / "ExeFS" / "main.npdm", build_npdm(APP, is_64bit=False))
    game = open_input(root)
    assert any(warning.startswith("main.npdm:") for warning in game.warnings)


def test_empty_romfs_is_a_warning(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", romfs=None)
    (root / "RomFS").mkdir()
    game = open_input(root)
    assert game.romfs_dir == root / "RomFS" and game.romfs_files == ()
    assert any("is empty" in warning for warning in game.warnings)


def test_deep_romfs_tree(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    deep = "/".join(f"level{index}" for index in range(12)) + "/leaf.bin"
    write(root / "RomFS" / deep, b"x")
    game = open_input(root)
    assert deep in [entry.path for entry in game.romfs_files]
    assert game.resolve(f"romfs/{deep}").read_bytes() == b"x"


def test_ingest_writes_nothing(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    before = sorted((str(path), path.stat().st_mtime_ns) for path in root.rglob("*"))
    open_input(root)
    after = sorted((str(path), path.stat().st_mtime_ns) for path in root.rglob("*"))
    assert before == after


def test_relative_paths_are_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_game(tmp_path / "game")
    monkeypatch.chdir(tmp_path)
    game = open_input("game")
    assert game.root == tmp_path / "game" and os.path.isabs(game.root)
