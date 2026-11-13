"""Tests for bea.synth, and end-to-end tests of BEA on the synthetic sample.

The sample is the only "game" the test suite ever builds. These tests check that it is
deterministic and well-formed, then run the real pipeline and the real command line on
it and verify every output independently (image sizes, edited values, the IPS applied to
the module image).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from bea.cli import main
from bea.errors import OutputError
from bea.formats import dds
from bea.formats.imageinfo import probe
from bea.formats.ips import apply_ips
from bea.formats.nacp import read_nacp
from bea.formats.npdm import read_npdm
from bea.formats.nso import HEADER_SIZE, NsoFile
from bea.fsutil import walk_files
from bea.hexutil import APPLICATION_ID_MIN, is_application_id
from bea.ingest import open_input
from bea.packagers.layeredfs import LayeredFSPackager
from bea.pipeline import NOTICE_NAME, BuildOptions, build
from bea.profile.loader import load_profile
from bea.synth import SynthResult, synthesize, synthetic_title_id
from bea.toolbox import Toolbox
from bea.transformers import default_transformers

EXPECTED_FILES = {
    "ExeFS/main",
    "ExeFS/main.npdm",
    "ExeFS/sdk",
    "control.nacp",
    "RomFS/config/graphics.ini",
    "RomFS/config/settings.json",
    "RomFS/data/archive.bin",
    "RomFS/fx/grade_lut.png",
    "RomFS/textures/crate_1024_rgba.dds",
    "RomFS/textures/rock_2048_bc1.dds",
    "RomFS/textures/sky_2048_bc7.dds",
    "RomFS/ui/icon_256.png",
    "RomFS/ui/panel_1920x1080.tga",
    "RomFS/ui/title_2560x1440.png",
}


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> SynthResult:
    base = tmp_path_factory.mktemp("synth")
    return synthesize(base / "game", profiles_dir=base / "profiles")


def tree(root: Path) -> dict[str, str]:
    return {
        found.path: hashlib.sha256((root / found.path).read_bytes()).hexdigest()
        for found in walk_files(root)
    }


def test_title_id_is_a_synthetic_application_id() -> None:
    for seed in range(50):
        text = synthetic_title_id(seed)
        assert len(text) == 16 and text == text.upper() and text.startswith("0100")
        value = int(text, 16)
        assert is_application_id(value) and value >= APPLICATION_ID_MIN and value & 0x1FFF == 0
    assert synthetic_title_id(1) == synthetic_title_id(1) != synthetic_title_id(2)


def test_the_sample_has_exactly_the_documented_files(sample: SynthResult) -> None:
    assert set(tree(sample.root)) == EXPECTED_FILES
    assert sample.files == len(EXPECTED_FILES)
    assert sample.profile_dir is not None
    assert sorted(path.name for path in sample.profile_dir.iterdir()) == [
        "NOTES.md",
        "patches.yaml",
        "profile.yaml",
    ]


def test_generation_is_deterministic(sample: SynthResult, tmp_path: Path) -> None:
    again = synthesize(tmp_path / "again", profiles_dir=tmp_path / "profiles")
    assert tree(again.root) == tree(sample.root)
    assert sample.profile_dir is not None and again.profile_dir is not None
    assert tree(again.profile_dir) == tree(sample.profile_dir)
    other = synthesize(tmp_path / "other", seed=2)
    assert other.title_id != sample.title_id
    assert tree(other.root)["ExeFS/main"] != tree(sample.root)["ExeFS/main"]


def test_refuses_a_non_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(OutputError, match="not empty"):
        synthesize(tmp_path / "busy")
    assert (tmp_path / "busy" / "file.txt").read_text(encoding="utf-8") == "mine"


def test_every_file_parses(sample: SynthResult) -> None:
    root = sample.root
    main_module = NsoFile.read(root / "ExeFS" / "main")  # verifies every segment hash
    assert any(segment.compressed for segment in main_module.header.segments)  # LZ4 is exercised
    sdk = NsoFile.read(root / "ExeFS" / "sdk")
    assert sdk.header.build_id != main_module.header.build_id
    assert read_npdm(root / "ExeFS" / "main.npdm").title_id == sample.title_id
    nacp = read_nacp(root / "control.nacp")
    title = nacp.title()
    assert (
        title is not None
        and title.name == "BEA Synthetic Sample"
        and nacp.display_version == "1.0.0"
    )

    sizes = {}
    for name in (
        "ui/title_2560x1440.png",
        "ui/panel_1920x1080.tga",
        "ui/icon_256.png",
        "fx/grade_lut.png",
    ):
        info = probe(root / "RomFS" / name)
        assert info is not None
        sizes[name] = (info.width, info.height)
    assert sizes == {
        "ui/title_2560x1440.png": (2560, 1440),
        "ui/panel_1920x1080.tga": (1920, 1080),
        "ui/icon_256.png": (256, 256),
        "fx/grade_lut.png": (4096, 16),
    }
    rock = dds.read_header(root / "RomFS" / "textures" / "rock_2048_bc1.dds")
    assert (rock.format.name, rock.mip_count) == ("BC1_UNORM", 12)
    assert (
        rock.trailing_bytes((root / "RomFS" / "textures" / "rock_2048_bc1.dds").stat().st_size) == 0
    )
    sky = dds.read_header(root / "RomFS" / "textures" / "sky_2048_bc7.dds")
    assert (sky.format.name, sky.mip_count) == ("BC7_UNORM", 1)


def test_profile_loads_and_its_windows_match_the_module(sample: SynthResult) -> None:
    assert sample.profile_dir is not None
    profile = load_profile(sample.profile_dir)
    assert profile.title_id == sample.title_id and profile.patches is not None
    (entry,) = profile.patches.builds
    module = NsoFile.read(sample.root / "ExeFS" / "main")
    assert entry.build_id == module.header.build_id
    assert [patch.category for patch in entry.patches] == ["resolution", "framerate"]
    raw = yaml.safe_load((sample.profile_dir / "patches.yaml").read_text(encoding="utf-8"))
    for patch in raw["builds"][0]["patches"]:
        for hunk in patch["hunks"]:
            assert "expected_bytes" not in hunk  # the profile holds no byte of the module
    for patch in entry.patches:
        for hunk in patch.hunks:
            window = module.read_memory(hunk.window_offset, hunk.window_length)
            assert hashlib.sha256(window).hexdigest() == hunk.expected_sha256
            assert (
                hunk.window_offset
                <= hunk.offset
                < hunk.end
                <= hunk.window_offset + hunk.window_length
            )
            assert module.read_memory(hunk.offset, 4) != hunk.patch


def test_build_of_the_sample(sample: SynthResult, tmp_path: Path) -> None:
    assert sample.profile_dir is not None
    game = open_input(sample.root)
    assert (game.title.title_id, game.title.name) == (sample.title_id, "BEA Synthetic Sample")
    assert [module.name for module in game.modules] == ["main", "sdk"]
    tools = Toolbox(
        tmp_path / "no-tools", environ={}
    )  # no ffmpeg: media is not part of this sample anyway
    options = BuildOptions(out_dir=tmp_path / "out", work_dir=tmp_path / "work")
    profile = load_profile(sample.profile_dir)

    def run() -> list[str]:
        result = build(
            game,
            profile,
            transformers=default_transformers(tools),
            packager=LayeredFSPackager(),
            tools=tools,
            options=options,
        )
        assert result.ok, [outcome.error for outcome in result.outcomes]
        skipped = {skip.source: skip.kind.value for skip in result.plan.skips}
        assert skipped == {"romfs/textures/sky_2048_bc7.dds": "unsupported"}
        return sorted(outcome.status.value for outcome in result.outcomes)

    assert run() == ["done"] * 7
    out = tmp_path / "out"
    romfs = out / "atmosphere" / "contents" / sample.title_id / "romfs"
    sizes = {}
    for name in (
        "ui/title_2560x1440.png",
        "ui/panel_1920x1080.tga",
        "textures/crate_1024_rgba.dds",
        "textures/rock_2048_bc1.dds",
    ):
        info = probe(romfs / name)
        assert info is not None
        sizes[name] = (info.width, info.height)
    assert sizes == {
        "ui/title_2560x1440.png": (1280, 720),
        "ui/panel_1920x1080.tga": (1280, 720),
        "textures/crate_1024_rgba.dds": (512, 512),
        "textures/rock_2048_bc1.dds": (512, 512),
    }
    assert not (romfs / "ui" / "icon_256.png").exists()  # within the limits
    assert not (romfs / "fx" / "grade_lut.png").exists()  # excluded by a rule
    assert not (romfs / "textures" / "sky_2048_bc7.dds").exists()  # unsupported
    ini = (romfs / "config" / "graphics.ini").read_bytes()
    assert (
        b"ResolutionScale=70\r\n" in ini
        and b"MaxFPS=30\r\n" in ini
        and b"ShadowQuality=High\r\n" in ini
    )
    assert json.loads((romfs / "config" / "settings.json").read_text(encoding="utf-8"))[
        "video"
    ] == {"maxFps": 30, "vsync": True}

    module = NsoFile.read(sample.root / "ExeFS" / "main")
    ips = (
        out
        / "atmosphere"
        / "exefs_patches"
        / f"bea-{sample.title_id}"
        / f"{module.header.build_id}.ips"
    )
    original = module.image()
    patched = apply_ips(original, ips.read_bytes(), bias=HEADER_SIZE)
    changed = [index for index in range(len(original)) if original[index] != patched[index]]
    assert changed and all(
        0x1000 <= index < 0x1004 or 0x2400 <= index < 0x2404 for index in changed
    )
    assert patched[0x1000:0x1004] == (0x52800000 | (720 << 5) | 8).to_bytes(4, "little")
    assert patched[0x2400:0x2404] == (0x52800000 | (30 << 5) | 9).to_bytes(4, "little")

    assert (out / NOTICE_NAME).is_file()
    assert run() == ["cached"] * 7


def test_command_line_on_the_sample(
    sample: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sample.profile_dir is not None
    profiles = str(sample.profile_dir.parent)
    assert main(["inspect", str(sample.root), "--profiles-dir", profiles, "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["title"]["title_id"] == sample.title_id
    assert inspected["modules"][0]["profile_build"]["patches_enabled"] == ["render-720p", "fps-30"]

    report = tmp_path / "report.json"
    assert (
        main(
            [
                "analyze",
                str(sample.root),
                "--profiles-dir",
                profiles,
                "-o",
                str(report),
                "--tools-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    analysis = json.loads(report.read_text(encoding="utf-8"))
    assert analysis["textures"]["over_target"] == 6 and analysis["plan"]["mismatches"] == []

    arguments = [
        "build",
        str(sample.root),
        "--profiles-dir",
        profiles,
        "--out",
        str(tmp_path / "out"),
        "--work",
        str(tmp_path / "work"),
    ]
    assert main([*arguments, "--dry-run", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["dry_run"] is True and {item["status"] for item in planned["actions"]} == {
        "planned"
    }
    assert not (tmp_path / "out").exists()

    assert main([*arguments, "-q"]) == 0
    assert "Built" in capsys.readouterr().out
    assert main(["profile", "validate", str(sample.profile_dir)]) == 0
    assert "1 valid, 0 invalid" in capsys.readouterr().out

    # A wrong verification window makes the build fail, and nothing is patched.
    patches = sample.profile_dir / "patches.yaml"
    original_text = patches.read_text(encoding="utf-8")
    first_hash = yaml.safe_load(original_text)["builds"][0]["patches"][0]["hunks"][0][
        "expected_sha256"
    ]
    try:
        patches.write_text(original_text.replace(first_hash, "0" * 64), encoding="utf-8")
        assert main([*arguments, "-q"]) == 1
        output = capsys.readouterr().out
        assert "MISMATCH" in output and "nothing is patched" in output
        assert not list((tmp_path / "out").rglob("*.ips"))
    finally:
        patches.write_text(original_text, encoding="utf-8")

    assert main(["build", str(sample.root / "ExeFS" / "main")]) == 2  # a file, not a folder
    assert "error:" in capsys.readouterr().err


def test_nso_hunk_command_reproduces_the_profile(
    sample: SynthResult, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sample.profile_dir is not None
    module = sample.root / "ExeFS" / "main"
    assert main(["nso", "hunk", str(module), "--offset", "0x1000", "--patch", "08 5A 80 52"]) == 0
    captured = capsys.readouterr()
    printed = yaml.safe_load(captured.out)[0]
    profile = load_profile(sample.profile_dir)
    assert profile.patches is not None
    hunk = profile.patches.builds[0].patches[0].hunks[0]
    assert printed["offset"] == hunk.offset and printed["patch_bytes"] == "08 5A 80 52"
    window = NsoFile.read(module).read_memory(printed["window_offset"], printed["window_length"])
    assert printed["expected_sha256"] == hashlib.sha256(window).hexdigest()
    assert (
        printed["window_offset"]
        <= 0x1000
        < 0x1004
        <= printed["window_offset"] + printed["window_length"]
    )


@pytest.mark.ffmpeg
def test_sample_with_media(tmp_path: Path) -> None:
    tools = Toolbox()
    if tools.find("ffmpeg") is None or tools.find("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are needed")
    result = synthesize(tmp_path / "game", with_media=True, tools=tools)
    movie = result.root / "RomFS" / "movies" / "intro.mp4"
    if not movie.exists():
        pytest.skip(f"ffmpeg could not encode the sample video: {result.notes}")
    assert movie.stat().st_size > 0 and result.files == len(EXPECTED_FILES) + 1


def test_media_without_ffmpeg_is_a_note(tmp_path: Path) -> None:
    result = synthesize(
        tmp_path / "game", with_media=True, tools=Toolbox(tmp_path / "none", environ={})
    )
    assert not (result.root / "RomFS" / "movies").exists()
    assert result.notes and "ffmpeg" in result.notes[0]
