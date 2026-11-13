"""End-to-end tests of executable patching: NSO module + profile hunks -> verified IPS.

Modules are built with bea.formats.nso.build_nso from deterministic bytes; hashes are
computed here with hashlib; the IPS files produced are parsed back and applied with the
generic applier, and compared with an image patched by hand.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from bea.errors import PatchError
from bea.formats.ips import apply_ips, parse_ips
from bea.formats.npdm import build_npdm
from bea.formats.nso import HEADER_SIZE, NsoFile, build_nso
from bea.ingest import open_input
from bea.model import GameInput
from bea.packagers.layeredfs import LayeredFSPackager
from bea.pipeline import BuildOptions, build
from bea.profile.schema import (
    BinarySettings,
    BuildPatches,
    ExefsMode,
    Hunk,
    Patch,
    PatchSet,
    Profile,
)
from bea.toolbox import Toolbox
from bea.transformers.base import Action, PlanContext, Skip, SkipKind
from bea.transformers.binary import BinaryTransformer, hunks_to_params, verify_build

APP = (0x0100 << 48) | 0xBEA0C0DE0000
APP_TEXT = f"{APP:016X}"
MODULE_ID = bytes(range(0x30, 0x44))  # 20 significant bytes
BUILD_ID = MODULE_ID.hex().upper()


def pseudo_random(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


TEXT = pseudo_random(0x3000, 1)
RODATA = pseudo_random(0x800, 2)
DATA = pseudo_random(0x400, 3)
RODATA_START = 0x3000
DATA_START = 0x4000


def make_module(path: Path, text: bytes = TEXT, *, module_id: bytes = MODULE_ID) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_nso(text, RODATA, DATA, module_id=module_id))
    return path


def image() -> bytes:
    result = bytearray(DATA_START + len(DATA))
    result[0 : len(TEXT)] = TEXT
    result[RODATA_START : RODATA_START + len(RODATA)] = RODATA
    result[DATA_START:] = DATA
    return bytes(result)


def hashed(offset: int, patch: bytes, *, window: int = 32, source: bytes | None = None) -> Hunk:
    start = offset - offset % 4
    data = image() if source is None else source
    return Hunk(
        offset=offset,
        patch=patch,
        expected_sha256=hashlib.sha256(data[start : start + window]).hexdigest(),
        window_offset=start,
        window_length=window,
    )


def literal(offset: int, patch: bytes) -> Hunk:
    return Hunk(offset=offset, patch=patch, expected=image()[offset : offset + len(patch)])


def build_entry(*patches: Patch, build_id: str = BUILD_ID, module: str = "main") -> BuildPatches:
    return BuildPatches(build_id=build_id, patches=patches, module=module, label="1.0.0")


def profile_with(*builds: BuildPatches, mode: ExefsMode = ExefsMode.IPS) -> Profile:
    return Profile(
        title_id=APP_TEXT,
        binary=BinarySettings(mode=mode),
        patches=PatchSet(title_id=APP_TEXT, builds=builds),
    )


def game_folder(
    tmp_path: Path, *, title: int = APP, modules: dict[str, bytes] | None = None
) -> GameInput:
    root = tmp_path / "game"
    exefs = root / "ExeFS"
    make_module(exefs / "main")
    for name, module_id in (modules or {}).items():
        make_module(exefs / name, module_id=module_id)
    (exefs / "main.npdm").write_bytes(build_npdm(title))
    return open_input(root)


def plan(game: GameInput, profile: Profile) -> list[Action | Skip]:
    context = PlanContext(game=game, profile=profile, tools=Toolbox(environ={}))
    return list(BinaryTransformer().plan(context))


SET_W8 = bytes.fromhex("08 5A 80 52".replace(" ", ""))
NOP = bytes.fromhex("1F2003D5")


# ----------------------------------------------------------------------- verify_build


def test_literal_and_hash_hunks_verify() -> None:
    nso = NsoFile.parse(build_nso(TEXT, RODATA, DATA, module_id=MODULE_ID))
    entry = build_entry(Patch("a", (literal(0x100, SET_W8), hashed(0x204, NOP))))
    checks = verify_build(nso, entry)
    assert [(check.patch, check.index, check.offset, check.ok) for check in checks] == [
        ("a", 0, 0x100, True),
        ("a", 1, 0x204, True),
    ]


def test_literal_mismatch_names_offset_and_bytes() -> None:
    nso = NsoFile.parse(build_nso(TEXT, RODATA, DATA, module_id=MODULE_ID))
    wrong = Hunk(offset=0x100, patch=SET_W8, expected=b"\x00\x11\x22\x33")
    (check,) = verify_build(nso, build_entry(Patch("a", (wrong,))))
    assert not check.ok and check.problem is not None
    found = " ".join(f"{byte:02X}" for byte in TEXT[0x100:0x104])
    assert "0x100" in check.problem and "00 11 22 33" in check.problem and found in check.problem


def test_hash_mismatch_does_not_print_the_bytes() -> None:
    nso = NsoFile.parse(build_nso(TEXT, RODATA, DATA, module_id=MODULE_ID))
    other = bytearray(image())
    other[0x208] ^= 0xFF  # inside the window 0x204..0x224
    (check,) = verify_build(
        nso, build_entry(Patch("a", (hashed(0x204, NOP, source=bytes(other)),)))
    )
    assert not check.ok and check.problem is not None
    assert "SHA-256" in check.problem
    assert " ".join(f"{byte:02X}" for byte in TEXT[0x204:0x208]) not in check.problem


@pytest.mark.parametrize(
    ("hunk", "fragment"),
    [
        (
            Hunk(offset=len(TEXT) - 2, patch=NOP, expected=b"\x00" * 4),
            "patched range",
        ),  # crosses text end
        (
            Hunk(offset=RODATA_START + len(RODATA) + 0x100, patch=NOP, expected=b"\x00" * 4),
            "patched range",
        ),  # in the gap between rodata and data
        (
            Hunk(offset=DATA_START + 0x1000, patch=NOP, expected=b"\x00" * 4),
            "patched range",
        ),  # beyond the image
        (
            Hunk(
                offset=0x10,
                patch=NOP,
                expected_sha256="0" * 64,
                window_offset=len(TEXT) - 16,
                window_length=32,
            ),
            "verification window",
        ),
    ],
)
def test_ranges_must_stay_inside_one_segment(hunk: Hunk, fragment: str) -> None:
    nso = NsoFile.parse(build_nso(TEXT, RODATA, DATA, module_id=MODULE_ID))
    (check,) = verify_build(nso, build_entry(Patch("a", (hunk,))))
    assert not check.ok and check.problem is not None and fragment in check.problem


def test_overlaps_between_patches_and_disabled_patches() -> None:
    nso = NsoFile.parse(build_nso(TEXT, RODATA, DATA, module_id=MODULE_ID))
    first = Patch("first", (literal(0x100, SET_W8),))
    second = Patch("second", (literal(0x102, b"\x00\x00"),))
    checks = verify_build(nso, build_entry(first, second))
    assert [check.ok for check in checks] == [True, False]
    assert checks[1].problem is not None and "first hunk 0" in checks[1].problem

    disabled = Patch(
        "second", (Hunk(offset=0x102, patch=b"\x00", expected=b"\xff"),), enabled=False
    )
    assert [check.ok for check in verify_build(nso, build_entry(first, disabled))] == [True]


def test_hunks_to_params_lists_enabled_hunks_only() -> None:
    entry = build_entry(
        Patch("on", (literal(0x100, SET_W8), hashed(0x204, NOP))),
        Patch("off", (literal(0x300, NOP),), enabled=False),
    )
    params = hunks_to_params(entry)
    assert [item["patch"] for item in params] == ["on", "on"]
    assert params[0]["bytes"] == "085A8052" and params[0]["sha256"] is None
    assert params[1]["expected"] is None and len(params[1]["sha256"]) == 64
    assert params[1]["window_offset"] == 0x204 and params[1]["window_length"] == 32


# ------------------------------------------------------------------------------- plan


def test_no_patches_means_nothing(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    assert plan(game, Profile(title_id=APP_TEXT)) == []
    assert plan(game, profile_with()) == []


def test_system_software_is_never_patched(tmp_path: Path) -> None:
    game = game_folder(tmp_path, title=(0x0100 << 48) | 0x0123)
    items = plan(game, profile_with(build_entry(Patch("a", (literal(0x100, SET_W8),)))))
    assert len(items) == 1 and isinstance(items[0], Skip)
    assert items[0].kind is SkipKind.MISMATCH and "system software" in items[0].reason


def test_ips_action(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    entry = build_entry(
        Patch("res", (hashed(0x100, SET_W8),)), Patch("fps", (literal(0x204, NOP),))
    )
    (action,) = plan(game, profile_with(entry))
    assert isinstance(action, Action)
    assert (action.source, action.output) == ("exefs/main", f"exefs_patches/{BUILD_ID}.ips")
    assert action.params["mode"] == "ips" and action.params["patches"] == ["res", "fps"]
    assert action.params["build_id"] == BUILD_ID and len(action.params["hunks"]) == 2
    assert action.details == {"patches": ["res", "fps"], "hunks": 2, "patched_bytes": 8}


def test_replace_mode_action(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    entry = build_entry(Patch("res", (hashed(0x100, SET_W8),)))
    (action,) = plan(game, profile_with(entry, mode=ExefsMode.REPLACE))
    assert (
        isinstance(action, Action)
        and action.output == "exefs/main"
        and action.params["mode"] == "replace"
    )


def test_one_bad_hunk_means_no_output_at_all(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    good = Patch("good", (hashed(0x100, SET_W8), literal(0x204, NOP)))
    bad = Patch("bad", (Hunk(offset=0x300, patch=NOP, expected=b"\x01\x02\x03\x04"),))
    items = plan(game, profile_with(build_entry(good, bad)))
    assert len(items) == 1
    (skip,) = items
    assert isinstance(skip, Skip) and skip.kind is SkipKind.MISMATCH and skip.is_error
    assert (
        skip.source == "exefs/main"
        and "bad hunk 0" in skip.reason
        and "nothing is patched" in skip.reason
    )


def test_many_problems_are_listed_up_to_ten(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    hunks = tuple(
        Hunk(offset=0x400 + 8 * index, patch=NOP, expected=b"\xee" * 4) for index in range(13)
    )
    (skip,) = plan(game, profile_with(build_entry(Patch("p", hunks))))
    assert isinstance(skip, Skip) and "and 3 more" in skip.reason


def test_assertion_only_patches_produce_nothing(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    same = Hunk(offset=0x100, patch=TEXT[0x100:0x104], expected=TEXT[0x100:0x104])
    assert plan(game, profile_with(build_entry(Patch("check", (same,))))) == []


def test_unknown_build_lists_the_known_ones(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    other = build_entry(Patch("a", (literal(0x100, SET_W8),)), build_id="AA" * 20)
    (skip,) = plan(game, profile_with(other))
    assert isinstance(skip, Skip) and skip.kind is SkipKind.UNSUPPORTED
    assert BUILD_ID in skip.reason and "AA" * 20 + " (1.0.0)" in skip.reason


def test_build_ids_match_through_zero_padding(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    padded = build_entry(Patch("a", (literal(0x100, SET_W8),)), build_id=BUILD_ID + "00" * 12)
    (action,) = plan(game, profile_with(padded))
    assert isinstance(action, Action)


def test_entries_for_absent_modules_are_reported(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    entry = build_entry(Patch("a", (literal(0x100, SET_W8),)), module="subsdk3")
    (skip,) = plan(game, profile_with(entry))
    assert (
        isinstance(skip, Skip)
        and skip.source == "exefs/subsdk3"
        and "does not contain" in skip.reason
    )


def test_module_without_build_id_is_refused(tmp_path: Path) -> None:
    game = game_folder(tmp_path, modules={"sdk": b""})
    entry = build_entry(Patch("a", (literal(0x100, SET_W8),)), build_id="00", module="sdk")
    items = plan(game, profile_with(entry))
    assert [type(item) for item in items] == [Skip]
    assert isinstance(items[0], Skip) and "no build ID" in items[0].reason


def test_corrupt_module_is_invalid(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    raw = bytearray((tmp_path / "game" / "ExeFS" / "main").read_bytes())
    raw[0x120] ^= 0xFF  # inside the text segment: its checked hash no longer matches
    (tmp_path / "game" / "ExeFS" / "main").write_bytes(bytes(raw))
    (skip,) = plan(game, profile_with(build_entry(Patch("a", (literal(0x100, SET_W8),)))))
    assert isinstance(skip, Skip) and skip.kind is SkipKind.INVALID


# -------------------------------------------------------------------------- transform


def run_transform(
    tmp_path: Path, profile: Profile, game: GameInput | None = None
) -> tuple[bytes, dict]:
    game = game or game_folder(tmp_path)
    (action,) = plan(game, profile)
    assert isinstance(action, Action)
    destination = tmp_path / "out.bin"
    result = BinaryTransformer().transform(game.resolve(action.source), destination, action.params)
    return destination.read_bytes(), {"details": result.details, "notes": result.notes}


def test_ips_applies_exactly_the_declared_bytes(tmp_path: Path) -> None:
    entry = build_entry(
        Patch("res", (hashed(0x100, SET_W8),)), Patch("rw", (literal(DATA_START + 8, b"\x01\x02"),))
    )
    data, outcome = run_transform(tmp_path, profile_with(entry))
    patch = parse_ips(data)
    assert [(record.offset, record.data) for record in patch.records] == [
        (0x100 + HEADER_SIZE, SET_W8),
        (DATA_START + 8 + HEADER_SIZE, b"\x01\x02"),
    ]
    expected = bytearray(image())
    expected[0x100:0x104] = SET_W8
    expected[DATA_START + 8 : DATA_START + 10] = b"\x01\x02"
    assert apply_ips(image(), data, bias=HEADER_SIZE) == bytes(expected)
    assert outcome["details"] == {
        "format": "ips",
        "records": 2,
        "patched_bytes": 6,
        "original_bytes_included": 0,
        "contains_game_bytes": False,
    }
    assert outcome["notes"] == []


def test_unchanged_bytes_inside_a_hunk_still_count_but_identical_hunks_are_dropped(
    tmp_path: Path,
) -> None:
    same = Hunk(offset=0x200, patch=TEXT[0x200:0x204], expected=TEXT[0x200:0x204])
    entry = build_entry(Patch("p", (literal(0x100, SET_W8), same)))
    data, outcome = run_transform(tmp_path, profile_with(entry))
    assert [record.offset for record in parse_ips(data).records] == [0x100 + HEADER_SIZE]
    assert outcome["details"]["patched_bytes"] == 4


def test_large_module_gets_ips32(tmp_path: Path) -> None:
    big_text = b"\x1f\x20\x03\xd5" * (0x1100000 // 4)  # 17 MiB, compresses well
    root = tmp_path / "game" / "ExeFS"
    make_module(root / "main", big_text)
    (root / "main.npdm").write_bytes(build_npdm(APP))
    game = open_input(tmp_path / "game")
    offset = 0x1080000
    hunk = Hunk(offset=offset, patch=b"\x00\x00\x00\x00", expected=b"\x1f\x20\x03\xd5")
    data, outcome = run_transform(tmp_path, profile_with(build_entry(Patch("far", (hunk,)))), game)
    assert data.startswith(b"IPS32") and outcome["details"]["format"] == "ips32"
    assert [record.offset for record in parse_ips(data).records] == [offset + HEADER_SIZE]


def test_terminator_offset_includes_one_original_byte(tmp_path: Path) -> None:
    big_text = pseudo_random(0x460000, 7)
    root = tmp_path / "game" / "ExeFS"
    make_module(root / "main", big_text)
    (root / "main.npdm").write_bytes(build_npdm(APP))
    game = open_input(tmp_path / "game")
    offset = 0x454F46 - HEADER_SIZE  # its IPS offset spells "EOF"
    hunk = Hunk(offset=offset, patch=b"\xaa", expected=big_text[offset : offset + 1])
    data, outcome = run_transform(tmp_path, profile_with(build_entry(Patch("p", (hunk,)))), game)
    (record,) = parse_ips(data).records
    assert record.offset == 0x454F45 and record.data == big_text[offset - 1 : offset] + b"\xaa"
    assert outcome["details"]["original_bytes_included"] == 1
    assert outcome["details"]["contains_game_bytes"] is True
    assert outcome["notes"] and "do not share" in outcome["notes"][0]


def test_transform_verifies_again(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    (action,) = plan(game, profile_with(build_entry(Patch("res", (hashed(0x100, SET_W8),)))))
    assert isinstance(action, Action)
    other = bytearray(TEXT)
    other[0x104] ^= 0xFF  # inside the verification window
    make_module(tmp_path / "game" / "ExeFS" / "main", bytes(other))
    destination = tmp_path / "out.ips"
    with pytest.raises(PatchError, match="no longer matches"):
        BinaryTransformer().transform(game.resolve("exefs/main"), destination, action.params)
    assert not destination.exists()


def test_replace_mode_writes_a_valid_patched_module(tmp_path: Path) -> None:
    entry = build_entry(Patch("res", (hashed(0x100, SET_W8), literal(RODATA_START + 4, b"\x99"))))
    data, outcome = run_transform(tmp_path, profile_with(entry, mode=ExefsMode.REPLACE))
    patched = NsoFile.parse(data)  # verifies the recomputed hashes
    assert patched.header.build_id == BUILD_ID
    new_image, old_image = patched.image(), image()
    differing = [index for index in range(len(old_image)) if new_image[index] != old_image[index]]
    changed = {0x100 + i for i in range(4) if SET_W8[i] != TEXT[0x100 + i]}
    if RODATA[4] != 0x99:
        changed.add(RODATA_START + 4)
    assert differing == sorted(changed)
    assert data[HEADER_SIZE + 0x100 : HEADER_SIZE + 0x104] == SET_W8  # image layout
    assert (
        outcome["details"]["contains_game_bytes"] is True and "never share" in outcome["notes"][0]
    )


# ------------------------------------------------------------------- through the pipeline


def test_through_the_pipeline_and_the_packager(tmp_path: Path) -> None:
    game = game_folder(tmp_path)
    profile = profile_with(build_entry(Patch("res", (hashed(0x100, SET_W8),))))
    options = BuildOptions(out_dir=tmp_path / "out", work_dir=tmp_path / "work")
    result = build(
        game,
        profile,
        transformers=[BinaryTransformer()],
        packager=LayeredFSPackager(),
        tools=Toolbox(environ={}),
        options=options,
    )
    assert result.ok
    ips = tmp_path / "out" / "atmosphere" / "exefs_patches" / f"bea-{APP_TEXT}" / f"{BUILD_ID}.ips"
    assert ips.is_file()
    expected = bytearray(image())
    expected[0x100:0x104] = SET_W8
    assert apply_ips(image(), ips.read_bytes(), bias=HEADER_SIZE) == bytes(expected)

    again = build(
        game,
        profile,
        transformers=[BinaryTransformer()],
        packager=LayeredFSPackager(),
        tools=Toolbox(environ={}),
        options=options,
    )
    assert [outcome.status.value for outcome in again.outcomes] == ["cached"]
