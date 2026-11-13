"""Tests for bea.profile.loader: YAML text to the schema dataclasses, strictly validated.

Every document is written inside the test. Expectations are derived by hand from
docs/PROFILES.md: the values a valid document must produce, and for every rule of the
document the key path at which a violation must be reported.
"""

from __future__ import annotations

import codecs
import copy
import datetime
import re
import struct
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from bea.errors import BeaError, ProfileError
from bea.profile import loader
from bea.profile.loader import (
    MAX_PROFILE_FILE_BYTES,
    find_profile_dir,
    list_profile_dirs,
    load_profile,
    load_profile_text,
    validate_profile_dir,
)
from bea.profile.schema import (
    MAX_HUNK_BYTES,
    MAX_PATCH_BYTES_PER_BUILD,
    PATCH_CATEGORIES,
    AlphaMode,
    BinarySettings,
    BuildPatches,
    ConfigFormat,
    ConfigRule,
    ExefsMode,
    Hunk,
    IfMissing,
    IniEdit,
    JsonEdit,
    MediaParams,
    MediaRule,
    MediaSettings,
    Patch,
    PatchSet,
    PowerOfTwo,
    Profile,
    ResampleFilter,
    Strategy,
    Target,
    TextEdit,
    TextureDefaults,
    TextureParams,
    TextureRule,
    TextureSettings,
)

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "profiles" / "_template"

# Synthetic identifiers, built at run time so that this file holds no ID literal.
TITLE_ID = f"{(0x0100 << 48) | 0xBEA0BEA0B000:016X}"
OTHER_TITLE_ID = f"{(0x0100 << 48) | 0xBEA0BEA0C000:016X}"
#: A title ID made of the digits 0-7 only: unquoted, YAML 1.1 reads it as an octal number.
OCTAL_LOOKING_ID = "0100" + "7" * 8 + "0000"
BUILD_ID = bytes(range(0xA1, 0xB5)).hex().upper()  # 20 bytes, as most linkers emit
OTHER_BUILD_ID = bytes(range(0xC1, 0xD5)).hex().upper()
IMAGE_BASE = 0x7100000000

HEADER = f'schema: 1\ntitle_id: "{TITLE_ID}"\n'


def problems_of(profile_text: str, patches_text: str | None = None) -> list[str]:
    """Return the problems of a profile that must be invalid."""
    with pytest.raises(ProfileError) as caught:
        load_profile_text(profile_text, patches_text)
    return list(caught.value.problems)


def the_problem(profile_text: str, patches_text: str | None = None) -> tuple[str, str, str]:
    """Return ``(file label, key path, message)`` of a profile with exactly one problem."""
    problems = problems_of(profile_text, patches_text)
    assert len(problems) == 1, problems
    label, path, message = problems[0].split(": ", 2)
    return label, path, message


def located(problems: list[str]) -> list[tuple[str, str]]:
    """Return ``(file label, key path)`` of every problem."""
    return [(label, path) for label, path, _message in (p.split(": ", 2) for p in problems)]


# ----------------------------------------------------------------------------------
# Valid documents
# ----------------------------------------------------------------------------------

FULL_PROFILE = (
    HEADER
    + r"""name: "Synthetic Quest"
description: >
  Lower resolution.
  Tested on nothing.
target:
  max_width: 1600
  max_height: 900
textures:
  enabled: false
  defaults:
    scale: 0.75
    strategy: resample
    filter: bicubic
    power_of_two: ignore
    alpha: weighted
  rules:
    - match: ["ui/**"]
      max_width: 1920
      max_height: 1080
    - match: ["**/*_lut.*", "fonts/**"]
      skip: true
    - match: "characters/**/*.dds"
      exclude: "characters/**/*_n.dds"
      scale: 0.5
      strategy: mip_drop
      filter: nearest
      power_of_two: preserve
      alpha: straight
media:
  enabled: false
  rules:
    - match: "movies/**/*.mp4"
      exclude: ["movies/credits/**", "*_hd.mp4"]
      max_width: 1280
      max_height: 720
      max_fps: 30
      crf: 23
      audio_bitrate: "128k"
    - match: "movies/intro.webm"
      video_bitrate: 2500000
      audio_bitrate: 96000
    - match: "movies/**"
      skip: true
configs:
  - match: "config/graphics.ini"
    format: ini
    edits:
      - section: "Rendering"
        key: "ResolutionScale"
        value: "70"
      - section: null
        key: "ShadowQuality"
        value: 2
        if_missing: add
      - key: "Global"
        value: ""
        if_missing: ignore
  - match: ["config/*.json"]
    exclude: "config/user.json"
    format: json
    encoding: "utf-16"
    required: false
    edits:
      - path: ["video", "modes", 0, "maxFps"]
        value: 30
      - path: ["video", "hdr"]
        value: null
        if_missing: add
      - path: ["audio"]
        value: {channels: [2, 6], gain: 0.5, enabled: true, name: "x"}
  - match: "boot/engine.cfg"
    format: text
    edits:
      - find: '^(vsync_interval\s*=\s*)\d+'
        replace: '\g<1>2'
      - find: "fps=60"
        replace: "fps=30"
        count: 3
binary:
  patchset_name: "synthetic-backport_1.0"
  mode: replace
"""
)

FULL_PATCHES = (
    HEADER
    + f"""builds:
  - build_id: "{BUILD_ID.lower()}{"00" * 12}"
    module: main
    label: "v1.0.0"
    image_base: 0x7100000000
    patches:
      - name: "render-720p"
        description: "Internal resolution"
        enabled: true
        hunks:
          - offset: 0x1A2B3C
            expected_bytes: "08 87 80 52"
            patch_bytes: "08 5A 80 52"
          - address: 0x71001A2B40
            expected_bytes: "0xE9, 0x86, 0x80, 0x52"
            patch_bytes: "095A8052"
      - name: "off.by_default-2"
        enabled: false
        hunks:
          - offset: "0x10"
            expected_bytes: "AA"
            patch_bytes: "AA"
  - build_id: "{OTHER_BUILD_ID}"
    module: subsdk0
    patches: []
"""
)


def test_public_names() -> None:
    assert loader.PROFILE_FILENAME == "profile.yaml"
    assert loader.PATCHES_FILENAME == "patches.yaml"
    assert {
        "PATCHES_FILENAME",
        "PROFILE_FILENAME",
        "find_profile_dir",
        "list_profile_dirs",
        "load_profile",
        "load_profile_text",
        "validate_profile_dir",
    } <= set(loader.__all__)


def test_minimal_profile_gets_every_documented_default() -> None:
    profile = load_profile_text(HEADER)
    assert isinstance(profile, Profile)
    assert profile.title_id == TITLE_ID
    assert profile.name is None
    assert profile.description is None
    assert profile.target == Target(max_width=1280, max_height=720)
    assert profile.textures == TextureSettings(
        enabled=True,
        defaults=TextureDefaults(
            scale=None,
            strategy=Strategy.AUTO,
            filter=ResampleFilter.LANCZOS,
            power_of_two=PowerOfTwo.PRESERVE,
            alpha=AlphaMode.STRAIGHT,
        ),
        rules=(),
    )
    assert profile.media == MediaSettings(enabled=True, rules=())
    assert profile.configs == ()
    assert profile.binary == BinarySettings(patchset_name=None, mode=ExefsMode.IPS)
    assert profile.patches is None
    assert profile.source_dir is None
    assert not profile.is_generic


def test_sections_that_are_present_but_empty_keep_the_defaults() -> None:
    empty = load_profile_text(
        HEADER + "target: {}\ntextures: {defaults: {}, rules: []}\nmedia: {}\nconfigs: []\n"
        "binary: {}\n"
    )
    assert empty == load_profile_text(HEADER)
    partial = load_profile_text(HEADER + "target: {max_width: 1920}\n")
    assert partial.target == Target(max_width=1920, max_height=720)


def test_full_profile_maps_every_key() -> None:
    profile = load_profile_text(FULL_PROFILE)
    assert profile.title_id == TITLE_ID
    assert profile.name == "Synthetic Quest"
    # A folded scalar: lines joined by a space, one final line break. Text is kept as is.
    assert profile.description == "Lower resolution. Tested on nothing.\n"
    assert profile.target == Target(max_width=1600, max_height=900)

    textures = profile.textures
    assert textures.enabled is False
    assert textures.defaults == TextureDefaults(
        scale=0.75,
        strategy=Strategy.RESAMPLE,
        filter=ResampleFilter.BICUBIC,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )
    assert textures.rules == (
        TextureRule(match=("ui/**",), max_width=1920, max_height=1080),
        TextureRule(match=("**/*_lut.*", "fonts/**"), skip=True),
        TextureRule(
            match=("characters/**/*.dds",),
            exclude=("characters/**/*_n.dds",),
            scale=0.5,
            strategy=Strategy.MIP_DROP,
            filter=ResampleFilter.NEAREST,
            power_of_two=PowerOfTwo.PRESERVE,
            alpha=AlphaMode.STRAIGHT,
        ),
    )
    assert all(isinstance(rule, TextureRule) for rule in textures.rules)

    media = profile.media
    assert media.enabled is False
    assert media.rules == (
        MediaRule(
            match=("movies/**/*.mp4",),
            exclude=("movies/credits/**", "*_hd.mp4"),
            max_width=1280,
            max_height=720,
            max_fps=30.0,
            crf=23,
            audio_bitrate="128k",
        ),
        MediaRule(match=("movies/intro.webm",), video_bitrate="2500000", audio_bitrate="96000"),
        MediaRule(match=("movies/**",), skip=True),
    )

    assert profile.configs == (
        ConfigRule(
            match=("config/graphics.ini",),
            format=ConfigFormat.INI,
            edits=(
                IniEdit(section="Rendering", key="ResolutionScale", value="70"),
                IniEdit(section=None, key="ShadowQuality", value="2", if_missing=IfMissing.ADD),
                IniEdit(section=None, key="Global", value="", if_missing=IfMissing.IGNORE),
            ),
        ),
        ConfigRule(
            match=("config/*.json",),
            format=ConfigFormat.JSON,
            edits=(
                JsonEdit(path=("video", "modes", 0, "maxFps"), value=30),
                JsonEdit(path=("video", "hdr"), value=None, if_missing=IfMissing.ADD),
                JsonEdit(
                    path=("audio",),
                    value={"channels": [2, 6], "gain": 0.5, "enabled": True, "name": "x"},
                ),
            ),
            exclude=("config/user.json",),
            encoding="utf-16",
            required=False,
        ),
        ConfigRule(
            match=("boot/engine.cfg",),
            format=ConfigFormat.TEXT,
            edits=(
                TextEdit(find=r"^(vsync_interval\s*=\s*)\d+", replace=r"\g<1>2", count=1),
                TextEdit(find="fps=60", replace="fps=30", count=3),
            ),
        ),
    )
    first, second, _third = profile.configs
    assert (first.encoding, first.required, first.exclude) == (None, True, ())
    assert first.edits[0] == IniEdit("Rendering", "ResolutionScale", "70", IfMissing.ERROR)
    assert second.edits[0] == JsonEdit(("video", "modes", 0, "maxFps"), 30, IfMissing.ERROR)

    assert profile.binary == BinarySettings(
        patchset_name="synthetic-backport_1.0", mode=ExefsMode.REPLACE
    )
    assert profile.patches is None


def test_numbers_accept_integers_and_are_stored_as_floats() -> None:
    profile = load_profile_text(
        HEADER + "textures: {defaults: {scale: 1}, rules: [{match: a, scale: 1}]}\n"
        "media: {rules: [{match: b, max_fps: 24}, {match: c, max_fps: 29.97}]}\n"
    )
    scales = [profile.textures.defaults.scale, profile.textures.rules[0].scale]
    assert scales == [1.0, 1.0]
    assert all(type(scale) is float for scale in scales)
    rates = [rule.max_fps for rule in profile.media.rules]
    assert rates == [24.0, 29.97]
    assert all(type(rate) is float for rate in rates)


def test_one_glob_and_a_list_of_globs_mean_the_same() -> None:
    single = load_profile_text(HEADER + 'textures: {rules: [{match: "ui/**", exclude: "*.bak"}]}\n')
    listed = load_profile_text(
        HEADER + 'textures: {rules: [{match: ["ui/**"], exclude: ["*.bak"]}]}\n'
    )
    assert single == listed
    assert single.textures.rules[0].match == ("ui/**",)
    assert single.textures.rules[0].exclude == ("*.bak",)
    # An empty exclude list is simply no exclusion.
    bare = load_profile_text(HEADER + 'textures: {rules: [{match: "ui/**", exclude: []}]}\n')
    assert bare.textures.rules[0].exclude == ()


def test_texture_rules_resolve_first_match_wins() -> None:
    profile = load_profile_text(
        HEADER
        + """\
target: {max_width: 1280, max_height: 720}
textures:
  defaults: {filter: box}
  rules:
    - match: ["ui/**"]
      max_width: 1920
      max_height: 1080
    - match: ["**/*_lut.*", "fonts/**"]
      skip: true
    - match: "characters/**/*.dds"
      exclude: "characters/**/*_n.dds"
      scale: 0.5
      strategy: mip_drop
    - match: "ui/**"
      max_width: 64
"""
    )
    textures, target = profile.textures, profile.target

    def params(width: int, height: int, scale: float | None, strategy: Strategy) -> TextureParams:
        return TextureParams(
            max_width=width,
            max_height=height,
            scale=scale,
            strategy=strategy,
            filter=ResampleFilter.BOX,
            power_of_two=PowerOfTwo.PRESERVE,
            alpha=AlphaMode.STRAIGHT,
        )

    ui = params(1920, 1080, None, Strategy.AUTO)
    untouched_by_rules = params(1280, 720, None, Strategy.AUTO)
    assert textures.resolve("ui/hud/frame.png", target) == ui
    # Selected by the first rule and by the skip rule: the first one wins.
    assert textures.resolve("ui/grade_lut.png", target) == ui
    # The later, stricter rule for the same files never gets a say.
    assert textures.resolve("UI/Hud/Frame.PNG", target) == ui
    assert textures.resolve("fx/grade_lut.png", target) is None
    assert textures.resolve("fonts/latin.bin", target) is None
    assert textures.resolve("characters/hero/body.dds", target) == params(
        1280, 720, 0.5, Strategy.MIP_DROP
    )
    # Excluded from its rule, the file falls through to "no rule": defaults and target.
    assert textures.resolve("characters/hero/body_n.dds", target) == untouched_by_rules
    assert textures.resolve("world/rock.dds", target) == untouched_by_rules


def test_media_rules_resolve_only_matched_files() -> None:
    profile = load_profile_text(
        HEADER
        + """\
target: {max_width: 1024, max_height: 576}
media:
  rules:
    - match: "movies/credits/**"
      skip: true
    - match: "movies/**/*.mp4"
      max_height: 480
      max_fps: 30
      video_bitrate: "2.5M"
    - match: "movies/**"
      crf: 30
"""
    )
    media, target = profile.media, profile.target
    assert media.resolve("movies/credits/roll.mp4", target) is None
    assert media.resolve("movies/intro/logo.mp4", target) == MediaParams(
        max_width=1024,
        max_height=480,
        max_fps=30.0,
        crf=None,
        video_bitrate="2.5M",
        audio_bitrate=None,
    )
    assert media.resolve("movies/intro/logo.webm", target) == MediaParams(
        max_width=1024, max_height=576, max_fps=None, crf=30, video_bitrate=None, audio_bitrate=None
    )
    assert media.resolve("sound/theme.ogg", target) is None


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ('"128k"', "128k"),
        ('"4M"', "4M"),
        ('"2.5m"', "2.5m"),
        ('"1K"', "1K"),
        ('"96000"', "96000"),
        ("96000", "96000"),
        ("1", "1"),
        ("0x10", "16"),
    ],
)
def test_bitrates_are_stored_as_text(written: str, stored: str) -> None:
    profile = load_profile_text(
        HEADER
        + f"media: {{rules: [{{match: a, video_bitrate: {written}, audio_bitrate: {written}}}]}}\n"
    )
    rule = profile.media.rules[0]
    assert (rule.video_bitrate, rule.audio_bitrate) == (stored, stored)


def test_full_patches_document() -> None:
    profile = load_profile_text(FULL_PROFILE, FULL_PATCHES)
    patches = profile.patches
    assert isinstance(patches, PatchSet)
    # AArch64 instruction words, laid out little-endian the way they sit in a module.
    mov_w8_1080 = struct.pack("<I", 0x52808708)
    mov_w8_720 = struct.pack("<I", 0x52805A08)
    assert mov_w8_1080 == bytes([0x08, 0x87, 0x80, 0x52])
    assert patches == PatchSet(
        title_id=TITLE_ID,
        builds=(
            BuildPatches(
                # Lower case and zero padding are spelling: the canonical form has neither.
                build_id=BUILD_ID,
                module="main",
                label="v1.0.0",
                patches=(
                    Patch(
                        name="render-720p",
                        description="Internal resolution",
                        enabled=True,
                        hunks=(
                            Hunk(offset=0x1A2B3C, expected=mov_w8_1080, patch=mov_w8_720),
                            # address 0x71001A2B40 minus image_base 0x7100000000
                            Hunk(
                                offset=0x1A2B40,
                                expected=struct.pack("<I", 0x528086E9),
                                patch=struct.pack("<I", 0x52805A09),
                            ),
                        ),
                    ),
                    Patch(
                        name="off.by_default-2",
                        description="",
                        enabled=False,
                        hunks=(Hunk(offset=16, expected=b"\xaa", patch=b"\xaa"),),
                    ),
                ),
            ),
            BuildPatches(build_id=OTHER_BUILD_ID, module="subsdk0", label=None, patches=()),
        ),
    )
    assert len(BUILD_ID) == 40
    assert patches.for_module("main", BUILD_ID) is patches.builds[0]
    assert patches.for_module("main", BUILD_ID.lower() + "00" * 12) is patches.builds[0]
    assert patches.for_module("subsdk0", BUILD_ID) is None


def test_patches_defaults_and_an_empty_build_list() -> None:
    empty = load_profile_text(HEADER, HEADER + "builds: []\n")
    assert empty.patches == PatchSet(title_id=TITLE_ID, builds=())

    profile = load_profile_text(
        HEADER,
        HEADER
        + f"""\
builds:
  - build_id: "{BUILD_ID}"
    patches:
      - name: only-required-keys
        hunks:
          - {{offset: 4, expected_bytes: "1F", patch_bytes: "2A"}}
""",
    )
    assert profile.patches is not None
    (build,) = profile.patches.builds
    assert (build.module, build.label) == ("main", None)
    assert build.patches == (
        Patch(
            name="only-required-keys",
            hunks=(Hunk(offset=4, expected=b"\x1f", patch=b"\x2a"),),
            description="",
            enabled=True,
        ),
    )


def hunk_document(hunk: str, build_keys: str = "") -> str:
    """Return a patches.yaml with one build, one patch and the given hunk (a flow mapping)."""
    return (
        HEADER
        + f'builds:\n  - build_id: "{BUILD_ID}"\n{build_keys}'
        + f"    patches:\n      - name: p\n        hunks:\n          - {hunk}\n"
    )


def the_offset(hunk: str, build_keys: str = "") -> int:
    profile = load_profile_text(HEADER, hunk_document(hunk, build_keys))
    assert profile.patches is not None
    return profile.patches.builds[0].patches[0].hunks[0].offset


@pytest.mark.parametrize(
    ("location", "build_keys", "offset"),
    [
        ("offset: 0x1A2B3C", "", 0x1A2B3C),
        ("offset: 1715004", "", 0x1A2B3C),
        ('offset: "0x1A2B3C"', "", 0x1A2B3C),
        ('offset: "1715004"', "", 0x1A2B3C),
        ("offset: 0", "", 0),
        # Without image_base an address is an offset.
        ("address: 0x1A2B40", "", 0x1A2B40),
        ("address: 0x71001A2B40", "    image_base: 0x7100000000\n", 0x1A2B40),
        ('address: "0x71001A2B40"', '    image_base: "0x7100000000"\n', 0x1A2B40),
        ("address: 0x7100000000", "    image_base: 0x7100000000\n", 0),
        ("address: 4096", "    image_base: 4096\n", 0),
        # The last four bytes of a 4 GiB image are still inside it.
        ("offset: 0xFFFFFFFC", "", 0xFFFFFFFC),
        ("address: 0x71FFFFFFFC", "    image_base: 0x7100000000\n", 0xFFFFFFFC),
    ],
)
def test_hunk_positions(location: str, build_keys: str, offset: int) -> None:
    hunk = f'{{{location}, expected_bytes: "00 00 00 00", patch_bytes: "01 01 01 01"}}'
    assert the_offset(hunk, build_keys) == offset


def test_text_that_yaml_keeps_as_text_needs_no_quotes() -> None:
    # Nothing is guessed, but nothing is demanded either: these scalars reach the loader
    # as strings because they do not look like numbers to YAML.
    profile = load_profile_text(
        f"schema: 1\ntitle_id: {TITLE_ID}\n",
        f"schema: 1\ntitle_id: {TITLE_ID.lower()}\nbuilds:\n  - build_id: {BUILD_ID}\n"
        "    patches:\n      - name: p\n        hunks:\n"
        "          - {offset: 8, expected_bytes: 1F 20 03 D5, patch_bytes: E0031F2A}\n",
    )
    assert profile.title_id == TITLE_ID
    assert profile.patches is not None
    assert profile.patches.title_id == TITLE_ID
    hunk = profile.patches.builds[0].patches[0].hunks[0]
    assert hunk == Hunk(offset=8, expected=b"\x1f\x20\x03\xd5", patch=b"\xe0\x03\x1f\x2a")


def test_title_id_is_stored_uppercase() -> None:
    profile = load_profile_text(
        f'schema: 1\ntitle_id: "{TITLE_ID.lower()}"\n',
        f'schema: 1\ntitle_id: "{TITLE_ID.lower()}"\nbuilds: []\n',
    )
    assert TITLE_ID.lower() != TITLE_ID
    assert profile.title_id == TITLE_ID
    assert profile.patches is not None
    assert profile.patches.title_id == TITLE_ID


@pytest.mark.parametrize(
    "name", ["a", "9", "A.b_c-9", "x" * 64, "9lives", "bea-" + TITLE_ID, "a..b"]
)
def test_patchset_names_that_are_folder_names(name: str) -> None:
    profile = load_profile_text(HEADER + f'binary: {{patchset_name: "{name}"}}\n')
    assert profile.binary.patchset_name == name


def test_yaml_anchors_and_merge_keys_work() -> None:
    profile = load_profile_text(
        HEADER
        + """\
textures:
  rules:
    - &ui
      match: "ui/**"
      max_width: 1920
    - <<: *ui
      match: "hud/**"
"""
    )
    # The merged rule overrides "match" and inherits "max_width": no duplicate key there.
    assert profile.textures.rules == (
        TextureRule(match=("ui/**",), max_width=1920),
        TextureRule(match=("hud/**",), max_width=1920),
    )


def test_overriding_a_merged_key_is_never_mistaken_for_a_duplicate() -> None:
    # "x" merges "w" and overrides one of its keys, and is itself merged by a mapping
    # that YAML builds before "x": the keys of "x" must be judged as they were written.
    profile = load_profile_text(
        HEADER
        + """\
configs:
  - match: a.json
    format: json
    edits:
      - path: [a]
        value:
          w: &w {k: 1, j: 2}
          deep:
            x: &x {<<: *w, k: 3}
      - path: [b]
        value: {<<: *x, extra: true}
"""
    )
    first, second = profile.configs[0].edits
    assert isinstance(first, JsonEdit)
    assert isinstance(second, JsonEdit)
    assert first.value == {"w": {"k": 1, "j": 2}, "deep": {"x": {"k": 3, "j": 2}}}
    assert second.value == {"k": 3, "j": 2, "extra": True}


def test_byte_order_mark_and_windows_line_endings_are_fine() -> None:
    text = "\N{BYTE ORDER MARK}" + (HEADER + "description: >\n  one\n  two\n").replace("\n", "\r\n")
    profile = load_profile_text(text)
    assert profile.title_id == TITLE_ID
    assert profile.description == "one two\n"


def test_template_is_a_valid_profile_that_shows_every_section() -> None:
    assert validate_profile_dir(TEMPLATE) == []
    profile = load_profile(TEMPLATE)
    placeholder = "0100" + "0" * 12
    assert profile.title_id == placeholder
    assert profile.source_dir == TEMPLATE
    assert profile.name
    assert profile.description
    assert profile.textures.rules
    assert any(rule.skip for rule in profile.textures.rules)
    assert any(rule.exclude for rule in profile.textures.rules)
    assert profile.media.rules
    assert {rule.format for rule in profile.configs} == set(ConfigFormat)
    assert profile.binary.patchset_name
    patches = profile.patches
    assert patches is not None
    assert patches.title_id == placeholder
    all_patches = [patch for build in patches.builds for patch in build.patches]
    assert {patch.enabled for patch in all_patches} == {True, False}
    assert all(patch.hunks for patch in all_patches)


def test_template_and_guide_name_no_game() -> None:
    placeholder = "0100" + "0" * 12
    title_ids = re.compile(r"0100[0-9A-Fa-f]{12}")
    for path in (
        TEMPLATE / "profile.yaml",
        TEMPLATE / "patches.yaml",
        REPO / "profiles" / "README.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert set(title_ids.findall(text)) <= {placeholder}, path.name


# ----------------------------------------------------------------------------------
# Problems in profile.yaml: one violation, one problem, at the right key path
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path", "fragment"),
    [
        pytest.param(f'title_id: "{TITLE_ID}"\n', "schema", "required", id="schema-missing"),
        pytest.param(f'schema: 2\ntitle_id: "{TITLE_ID}"\n', "schema", "must be 1", id="schema-2"),
        pytest.param(
            f'schema: "1"\ntitle_id: "{TITLE_ID}"\n', "schema", "the string '1'", id="schema-str"
        ),
        pytest.param(
            f'schema: true\ntitle_id: "{TITLE_ID}"\n', "schema", "boolean", id="schema-bool"
        ),
        pytest.param(
            f'schema: 1.0\ntitle_id: "{TITLE_ID}"\n', "schema", "the number 1.0", id="schema-float"
        ),
        pytest.param("schema: 1\n", "title_id", "required", id="title-missing"),
        pytest.param(
            f"schema: 1\ntitle_id: {OCTAL_LOOKING_ID}\n",
            "title_id",
            f"the integer {int(OCTAL_LOOKING_ID, 8)}: put it in quotes",
            id="title-unquoted-octal",
        ),
        pytest.param(
            f"schema: 1\ntitle_id: 0x{TITLE_ID}\n",
            "title_id",
            f"the integer {int(TITLE_ID, 16)}: put it in quotes",
            id="title-unquoted-hex",
        ),
        pytest.param(
            f'schema: 1\ntitle_id: "{TITLE_ID[:-1]}"\n', "title_id", "16 hex digits", id="title-15"
        ),
        pytest.param(
            f'schema: 1\ntitle_id: "{TITLE_ID}0"\n', "title_id", "16 hex digits", id="title-17"
        ),
        pytest.param(
            f'schema: 1\ntitle_id: "{"G" * 16}"\n', "title_id", "16 hex digits", id="title-not-hex"
        ),
        pytest.param(
            f'schema: 1\ntitle_id: "0x{TITLE_ID}"\n', "title_id", "16 hex digits", id="title-0x"
        ),
        pytest.param(
            f'schema: 1\ntitle_id: " {TITLE_ID}"\n', "title_id", "16 hex digits", id="title-blank"
        ),
        pytest.param(f"schema: 1\ntitle_id: [{TITLE_ID}]\n", "title_id", "a list", id="title-list"),
        pytest.param("schema: 1\ntitle_id:\n", "title_id", "null", id="title-null"),
    ],
)
def test_schema_and_title_id_problems(text: str, path: str, fragment: str) -> None:
    label, where, message = the_problem(text)
    assert (label, where) == ("<profile>", path)
    assert fragment in message


def ini_edit(edit: str) -> str:
    return f"configs: [{{match: a.ini, format: ini, edits: [{edit}]}}]\n"


def json_edit(edit: str) -> str:
    return f"configs: [{{match: a.json, format: json, edits: [{edit}]}}]\n"


def text_edit(edit: str) -> str:
    return f"configs: [{{match: a.cfg, format: text, edits: [{edit}]}}]\n"


def text_rule(keys: str) -> str:
    return f"configs: [{{match: a.cfg, format: text, {keys}, edits: [{{find: a, replace: b}}]}}]\n"


EDIT = "configs[0].edits[0]"
TEXTURE_RULE_KEYS = (
    "match, exclude, skip, max_width, max_height, scale, strategy, filter, power_of_two, alpha"
)
MEDIA_RULE_KEYS = (
    "match, exclude, skip, max_width, max_height, max_fps, crf, video_bitrate, audio_bitrate"
)

PROFILE_CASES = [
    # ------------------------------------------------------------------ top level
    ("texture: {}\n", "texture", "did you mean 'textures'?"),
    ("Name: x\n", "Name", "did you mean 'name'?"),
    (
        "zzz: 1\n",
        "zzz",
        "unknown key; allowed keys: schema, title_id, name, description, target, textures, "
        "media, configs, binary",
    ),
    ("builds: []\n", "builds", "unknown key"),
    ("name: 2048\n", "name", "the integer 2048: put it in quotes"),
    ("name: 1.10\n", "name", "the number 1.1: put it in quotes"),
    ("name: yes\n", "name", "the boolean true: put it in quotes"),
    ("name: 2024-01-31\n", "name", "the date 2024-01-31: put it in quotes"),
    ("name:\n", "name", "null"),
    ("description: [a, b]\n", "description", "got a list"),
    ("description: {a: b}\n", "description", "got a mapping"),
    # --------------------------------------------------------------------- target
    ("target: 1280\n", "target", "must be a mapping (max_width, max_height)"),
    ("target: [1280, 720]\n", "target", "got a list"),
    ("target:\n", "target", "null"),
    ("target: {max_width: 0}\n", "target.max_width", "an integer >= 1"),
    ("target: {max_height: -720}\n", "target.max_height", "an integer >= 1"),
    ("target: {max_height: 720.0}\n", "target.max_height", "the number 720.0"),
    ("target: {max_width: true}\n", "target.max_width", "the boolean true"),
    ('target: {max_width: "1280"}\n', "target.max_width", "the string '1280'"),
    ("target: {max_width: }\n", "target.max_width", "null"),
    ("target: {depth: 1}\n", "target.depth", "allowed keys: max_width, max_height"),
    ("target: {max_widht: 1}\n", "target.max_widht", "did you mean 'max_width'?"),
    # ------------------------------------------------------------------- textures
    ("textures: []\n", "textures", "must be a mapping (enabled, defaults, rules)"),
    ("textures: {enabled: 1}\n", "textures.enabled", "true or false"),
    ('textures: {enabled: "true"}\n', "textures.enabled", "true or false"),
    ("textures: {enabled: }\n", "textures.enabled", "null"),
    ("textures: {defaults: [auto]}\n", "textures.defaults", "must be a mapping"),
    ("textures: {defaults: {scale: 0}}\n", "textures.defaults.scale", "greater than 0"),
    ("textures: {defaults: {scale: 1.5}}\n", "textures.defaults.scale", "at most 1"),
    ("textures: {defaults: {scale: -0.5}}\n", "textures.defaults.scale", "greater than 0"),
    ("textures: {defaults: {scale: true}}\n", "textures.defaults.scale", "the boolean true"),
    ("textures: {defaults: {scale: .nan}}\n", "textures.defaults.scale", "nan"),
    ('textures: {defaults: {scale: "0.5"}}\n', "textures.defaults.scale", "the string '0.5'"),
    (
        "textures: {defaults: {strategy: mipdrop}}\n",
        "textures.defaults.strategy",
        "must be one of auto, mip_drop, resample (did you mean 'mip_drop'?)",
    ),
    ("textures: {defaults: {strategy: Auto}}\n", "textures.defaults.strategy", "'auto'?"),
    (
        "textures: {defaults: {filter: 3}}\n",
        "textures.defaults.filter",
        "nearest, box, bilinear, hamming, bicubic, lanczos",
    ),
    (
        "textures: {defaults: {power_of_two: keep}}\n",
        "textures.defaults.power_of_two",
        "preserve, ignore",
    ),
    (
        "textures: {defaults: {alpha: premultiplied}}\n",
        "textures.defaults.alpha",
        "straight, weighted",
    ),
    (
        "textures: {defaults: {max_width: 64}}\n",
        "textures.defaults.max_width",
        "allowed keys: scale, strategy, filter, power_of_two, alpha",
    ),
    ("textures: {rules: {match: a}}\n", "textures.rules", "must be a list"),
    ('textures: {rules: ["ui/**"]}\n', "textures.rules[0]", "must be a mapping"),
    ("textures: {rules: [{skip: true}]}\n", "textures.rules[0].match", "required"),
    ("textures: {rules: [{match: []}]}\n", "textures.rules[0].match", "at least one glob"),
    ('textures: {rules: [{match: ""}]}\n', "textures.rules[0].match", "''"),
    ('textures: {rules: [{match: "/"}]}\n', "textures.rules[0].match", "'/'"),
    ("textures: {rules: [{match: 5}]}\n", "textures.rules[0].match", "a glob or a list of globs"),
    ("textures: {rules: [{match: }]}\n", "textures.rules[0].match", "null"),
    (
        'textures: {rules: [{match: a}, {match: ["ok/*.png", "tex_[z-a].png"]}]}\n',
        "textures.rules[1].match[1]",
        "'tex_[z-a].png'",
    ),
    ('textures: {rules: [{match: [a, ""]}]}\n', "textures.rules[0].match[1]", "''"),
    (
        'textures: {rules: [{match: a, exclude: "x[z-a]"}]}\n',
        "textures.rules[0].exclude",
        "'x[z-a]'",
    ),
    (
        "textures: {rules: [{match: a, exclude: [b, 5]}]}\n",
        "textures.rules[0].exclude[1]",
        "the integer 5",
    ),
    (
        "textures: {rules: [{match: a}, {match: b}, {match: c, max_width: 0}]}\n",
        "textures.rules[2].max_width",
        "an integer >= 1",
    ),
    (
        "textures: {rules: [{match: a, max_height: 1.5}]}\n",
        "textures.rules[0].max_height",
        "the number 1.5",
    ),
    ("textures: {rules: [{match: a, scale: 2}]}\n", "textures.rules[0].scale", "at most 1"),
    (
        "textures: {rules: [{match: a, strategy: drop}]}\n",
        "textures.rules[0].strategy",
        "auto, mip_drop, resample",
    ),
    (
        "textures: {rules: [{match: a, filter: Lanczos}]}\n",
        "textures.rules[0].filter",
        "'lanczos'?",
    ),
    (
        "textures: {rules: [{match: a, power_of_two: true}]}\n",
        "textures.rules[0].power_of_two",
        "the boolean true",
    ),
    ("textures: {rules: [{match: a, alpha: 1}]}\n", "textures.rules[0].alpha", "the integer 1"),
    (
        "textures: {rules: [{match: a, skip: true, scale: 0.5}]}\n",
        "textures.rules[0].scale",
        "skip: true",
    ),
    ('textures: {rules: [{match: a, skip: "yes"}]}\n', "textures.rules[0].skip", "true or false"),
    (
        "textures: {rules: [{match: a, max_fps: 30}]}\n",
        "textures.rules[0].max_fps",
        f"allowed keys: {TEXTURE_RULE_KEYS}",
    ),
    # ---------------------------------------------------------------------- media
    ("media: [a]\n", "media", "must be a mapping (enabled, rules)"),
    ("media: {enabled: }\n", "media.enabled", "null"),
    ("media: {rules: a}\n", "media.rules", "must be a list"),
    ("media: {rules: [{max_fps: 30}]}\n", "media.rules[0].match", "required"),
    ('media: {rules: [{match: "[b-a]"}]}\n', "media.rules[0].match", "'[b-a]'"),
    ("media: {rules: [{match: a, max_width: 0}]}\n", "media.rules[0].max_width", ">= 1"),
    ("media: {rules: [{match: a, max_height: 0.5}]}\n", "media.rules[0].max_height", ">= 1"),
    ("media: {rules: [{match: a, max_fps: 0}]}\n", "media.rules[0].max_fps", "greater than 0"),
    ("media: {rules: [{match: a, max_fps: -30}]}\n", "media.rules[0].max_fps", "greater than 0"),
    ("media: {rules: [{match: a, max_fps: .inf}]}\n", "media.rules[0].max_fps", "inf"),
    ('media: {rules: [{match: a, max_fps: "30"}]}\n', "media.rules[0].max_fps", "'30'"),
    ("media: {rules: [{match: a, max_fps: true}]}\n", "media.rules[0].max_fps", "boolean"),
    ("media: {rules: [{match: a, crf: 64}]}\n", "media.rules[0].crf", "from 0 to 63"),
    ("media: {rules: [{match: a, crf: -1}]}\n", "media.rules[0].crf", "from 0 to 63"),
    ("media: {rules: [{match: a, crf: 23.0}]}\n", "media.rules[0].crf", "the number 23.0"),
    ("media: {rules: [{match: a, crf: false}]}\n", "media.rules[0].crf", "the boolean false"),
    (
        'media: {rules: [{match: a, crf: 23, video_bitrate: "2500k"}]}\n',
        "media.rules[0]",
        "mutually exclusive",
    ),
    ("media: {rules: [{match: a, skip: true, crf: 23}]}\n", "media.rules[0].crf", "skip: true"),
    (
        "media: {rules: [{match: a, scale: 0.5}]}\n",
        "media.rules[0].scale",
        f"allowed keys: {MEDIA_RULE_KEYS}",
    ),
    # -------------------------------------------------------------------- configs
    ("configs: {match: a}\n", "configs", "must be a list"),
    (
        "configs: [a.ini]\n",
        "configs[0]",
        "must be a mapping (match, exclude, format, encoding, required, edits)",
    ),
    (
        "configs: [{format: text, edits: [{find: a, replace: b}]}]\n",
        "configs[0].match",
        "required",
    ),
    ("configs: [{match: a, edits: [{find: a, replace: b}]}]\n", "configs[0].format", "required"),
    ("configs: [{match: a, format: text}]\n", "configs[0].edits", "required"),
    ("configs: [{match: a, format: yaml, edits: [{}]}]\n", "configs[0].format", "ini, json, text"),
    ("configs: [{match: a, format: INI, edits: [{}]}]\n", "configs[0].format", "'ini'?"),
    ("configs: [{match: a, format: text, edits: []}]\n", "configs[0].edits", "must not be empty"),
    (
        "configs: [{match: a, format: text, edits: {find: a, replace: b}}]\n",
        "configs[0].edits",
        "must be a list",
    ),
    (
        "configs: [{match: a, format: ini, edits: [5]}]\n",
        EDIT,
        "must be a mapping (section, key, value, if_missing)",
    ),
    (text_rule('encoding: "utf-9"'), "configs[0].encoding", "utf-9"),
    (text_rule("encoding: hex"), "configs[0].encoding", "not a text encoding"),
    (text_rule('encoding: ""'), "configs[0].encoding", "not a text encoding"),
    (text_rule("encoding: 8"), "configs[0].encoding", "the integer 8: put it in quotes"),
    (text_rule('required: "no"'), "configs[0].required", "true or false"),
    (text_rule("required: 0"), "configs[0].required", "true or false"),
    (text_rule('exclude: ["ok", "x[9-0]"]'), "configs[0].exclude[1]", "'x[9-0]'"),
    (text_rule("mode: fast"), "configs[0].mode", "unknown key"),
    # ------------------------------------------------------------- format: ini
    (ini_edit("{value: v}"), f"{EDIT}.key", "required"),
    (ini_edit("{key: k}"), f"{EDIT}.value", "required"),
    (ini_edit("{key: 5, value: v}"), f"{EDIT}.key", "the integer 5: put it in quotes"),
    (ini_edit('{key: "", value: v}'), f"{EDIT}.key", "must not be empty"),
    (ini_edit('{key: "  ", value: v}'), f"{EDIT}.key", "must not be empty"),
    (ini_edit('{key: "a\\nb", value: v}'), f"{EDIT}.key", "single line"),
    (ini_edit('{section: "", key: k, value: v}'), f"{EDIT}.section", "use null"),
    (ini_edit("{section: 5, key: k, value: v}"), f"{EDIT}.section", "put it in quotes"),
    (ini_edit("{section: [a], key: k, value: v}"), f"{EDIT}.section", "a list"),
    (ini_edit('{section: "a\\nb", key: k, value: v}'), f"{EDIT}.section", "single line"),
    (ini_edit('{key: k, value: "70\\n"}'), f"{EDIT}.value", "single line"),
    (
        ini_edit("{key: k, value: v, if_missing: create}"),
        f"{EDIT}.if_missing",
        "error, add, ignore",
    ),
    (ini_edit("{key: k, value: v, if_missing: true}"), f"{EDIT}.if_missing", "boolean"),
    (
        ini_edit("{key: k, value: v, find: x}"),
        f"{EDIT}.find",
        "allowed keys: section, key, value, if_missing",
    ),
    # ------------------------------------------------------------ format: json
    (json_edit("{value: 1}"), f"{EDIT}.path", "required"),
    (json_edit("{path: [], value: 1}"), f"{EDIT}.path", "must not be empty"),
    (json_edit("{path: a, value: 1}"), f"{EDIT}.path", "must be a list"),
    (json_edit("{path: [a, 1.5], value: 1}"), f"{EDIT}.path[1]", "the number 1.5"),
    (json_edit("{path: [true], value: 1}"), f"{EDIT}.path[0]", "the boolean true"),
    (json_edit("{path: [a, -1], value: 1}"), f"{EDIT}.path[1]", "the integer -1"),
    (json_edit("{path: [a, [b]], value: 1}"), f"{EDIT}.path[1]", "a list"),
    (json_edit("{path: [a, ~], value: 1}"), f"{EDIT}.path[1]", "null"),
    (json_edit("{path: [a]}"), f"{EDIT}.value", "required"),
    (json_edit("{path: [a], value: 2024-01-31}"), f"{EDIT}.value", "not a JSON value"),
    (json_edit("{path: [a], value: .inf}"), f"{EDIT}.value", "inf"),
    (json_edit("{path: [a], value: {b: [1, .nan]}}"), f"{EDIT}.value.b[1]", "nan"),
    (json_edit("{path: [a], value: {b: [{c: 2024-01-31}]}}"), f"{EDIT}.value.b[0].c", "date"),
    (json_edit("{path: [a], value: {1: x}}"), f"{EDIT}.value", "keys are strings"),
    (json_edit("{path: [a], value: &v [1, *v]}"), f"{EDIT}.value[1]", "contains itself"),
    (json_edit("{path: [a], value: &v {k: {again: *v}}}"), f"{EDIT}.value.k.again", "itself"),
    (json_edit("{path: [a], value: 1, if_missing: maybe}"), f"{EDIT}.if_missing", "error, add"),
    (
        json_edit("{path: [a], value: 1, key: k}"),
        f"{EDIT}.key",
        "allowed keys: path, value, if_missing",
    ),
    # ------------------------------------------------------------ format: text
    (text_edit("{replace: b}"), f"{EDIT}.find", "required"),
    (text_edit("{find: a}"), f"{EDIT}.replace", "required"),
    (text_edit("{find: 5, replace: b}"), f"{EDIT}.find", "put it in quotes"),
    (text_edit("{find: a, replace: [b]}"), f"{EDIT}.replace", "a list"),
    (text_edit('{find: "(a", replace: b}'), f"{EDIT}.find", "regular expression"),
    (text_edit("{find: '[z-a]', replace: b}"), f"{EDIT}.find", "regular expression"),
    (text_edit("{find: 'a{99999999999999}', replace: b}"), f"{EDIT}.find", "regular expression"),
    (text_edit(r"{find: '(a)', replace: '\2'}"), f"{EDIT}.replace", "not a valid replacement"),
    (text_edit(r"{find: a, replace: '\q'}"), f"{EDIT}.replace", "not a valid replacement"),
    (text_edit(r"{find: a, replace: 'tail\'}"), f"{EDIT}.replace", "not a valid replacement"),
    (
        text_edit(r"{find: '(?P<n>a)', replace: '\g<nope>'}"),
        f"{EDIT}.replace",
        "not a valid replacement",
    ),
    (text_edit("{find: a, replace: b, count: 0}"), f"{EDIT}.count", "an integer >= 1"),
    (text_edit("{find: a, replace: b, count: 1.0}"), f"{EDIT}.count", "the number 1.0"),
    (text_edit("{find: a, replace: b, count: true}"), f"{EDIT}.count", "boolean"),
    (
        text_edit("{find: a, replace: b, section: s}"),
        f"{EDIT}.section",
        "allowed keys: find, replace, count",
    ),
    # --------------------------------------------------------------------- binary
    ("binary: ips\n", "binary", "must be a mapping (patchset_name, mode)"),
    (
        'binary: {patchset_name: ".hidden"}\n',
        "binary.patchset_name",
        "start with a letter or digit",
    ),
    ('binary: {patchset_name: "-x"}\n', "binary.patchset_name", "start with a letter or digit"),
    ('binary: {patchset_name: "a/b"}\n', "binary.patchset_name", "'a/b'"),
    ('binary: {patchset_name: "two words"}\n', "binary.patchset_name", "'two words'"),
    ('binary: {patchset_name: ""}\n', "binary.patchset_name", "1 to 64"),
    (f'binary: {{patchset_name: "{"a" * 65}"}}\n', "binary.patchset_name", "1 to 64"),
    (
        'binary: {patchset_name: "caf\N{LATIN SMALL LETTER E WITH ACUTE}"}\n',
        "binary.patchset_name",
        "letters, digits",
    ),
    ('binary: {patchset_name: "name\\n"}\n', "binary.patchset_name", "letters, digits"),
    ("binary: {patchset_name: 5}\n", "binary.patchset_name", "the integer 5: put it in quotes"),
    ("binary: {mode: IPS}\n", "binary.mode", "did you mean 'ips'?"),
    ("binary: {mode: patch}\n", "binary.mode", "must be one of ips, replace"),
    ("binary: {name: x}\n", "binary.name", "allowed keys: patchset_name, mode"),
]


@pytest.mark.parametrize(("body", "path", "fragment"), PROFILE_CASES)
def test_profile_problems(body: str, path: str, fragment: str) -> None:
    label, where, message = the_problem(HEADER + body)
    assert (label, where) == ("<profile>", path)
    assert fragment in message


@pytest.mark.parametrize(
    "value",
    [
        '"fast"',
        '"2500 k"',
        '"k"',
        '"-5k"',
        '"+5k"',
        '"1.5.2M"',
        '"2500kb"',
        '"5G"',
        '"1,5M"',
        '".5M"',
        '"5.M"',
        '""',
        '"0"',
        '"0k"',
        '"0.0M"',
        "0",
        "-64000",
        "2.5",
        "true",
        "[128]",
        "{k: 1}",
        "",
    ],
)
@pytest.mark.parametrize("key", ["video_bitrate", "audio_bitrate"])
def test_bitrate_problems(key: str, value: str) -> None:
    label, where, message = the_problem(
        HEADER + f"media: {{rules: [{{match: a, {key}: {value}}}]}}\n"
    )
    assert (label, where) == ("<profile>", f"media.rules[0].{key}")
    assert "bitrate" in message


@pytest.mark.parametrize(
    "value", ["0.7", "on", "No", "", "~", "[1]", "{a: 1}", "2024-01-31", ".inf", "!!binary aGk="]
)
def test_ini_values_that_are_not_text_or_integers_ask_for_quotes(value: str) -> None:
    # 0.70 would silently become 0.7 and "on" would become true: only the author knows
    # the text the file needs.
    label, where, message = the_problem(HEADER + ini_edit(f"{{key: k, value: {value}}}"))
    assert (label, where) == ("<profile>", f"{EDIT}.value")
    assert "quotes" in message


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("70", "70"),
        ("-1", "-1"),
        ("0x10", "16"),
        ('"0x10"', "0x10"),
        ('"0.70"', "0.70"),
        ('"on"', "on"),
    ],
)
def test_ini_values(written: str, stored: str) -> None:
    profile = load_profile_text(HEADER + ini_edit(f"{{key: k, value: {written}}}"))
    (edit,) = profile.configs[0].edits
    assert edit == IniEdit(section=None, key="k", value=stored)


def test_skip_excludes_every_tuning_key() -> None:
    tuning = {
        "max_width": "64",
        "max_height": "64",
        "scale": "0.5",
        "strategy": "resample",
        "filter": "box",
        "power_of_two": "ignore",
        "alpha": "weighted",
    }
    keys = ", ".join(f"{key}: {value}" for key, value in tuning.items())
    rule = f'{{match: "a/**", exclude: "a/b/**", skip: true, {keys}}}'
    problems = problems_of(HEADER + f"textures: {{rules: [{rule}]}}\n")
    assert located(problems) == [("<profile>", f"textures.rules[0].{key}") for key in tuning]
    # The same keys are fine when the rule does not skip, and so is a bare skip rule.
    load_profile_text(HEADER + f"textures: {{rules: [{rule.replace('true', 'false')}]}}\n")
    load_profile_text(
        HEADER + 'textures: {rules: [{match: "a/**", exclude: "a/b/**", skip: true}]}\n'
    )

    media = {
        "max_width": "64",
        "max_height": "64",
        "max_fps": "30",
        "crf": "20",
        "audio_bitrate": "1",
    }
    keys = ", ".join(f"{key}: {value}" for key, value in media.items())
    problems = problems_of(HEADER + f"media: {{rules: [{{match: a, skip: true, {keys}}}]}}\n")
    assert located(problems) == [("<profile>", f"media.rules[0].{key}") for key in media]
    problems = problems_of(
        HEADER + 'media: {rules: [{match: a, skip: true, video_bitrate: "1M"}]}\n'
    )
    assert located(problems) == [("<profile>", "media.rules[0].video_bitrate")]


def test_edits_of_a_rule_without_a_usable_format_are_only_checked_as_a_list() -> None:
    problems = problems_of(
        HEADER + "configs: [{match: a, format: xml, edits: [{anything: goes}, 5]}]\n"
    )
    assert located(problems) == [
        ("<profile>", "configs[0].format"),
        ("<profile>", "configs[0].edits[1]"),
    ]


def test_ambiguous_regex_is_a_problem_when_python_makes_it_an_error() -> None:
    # Python warns about "[[" (FutureWarning). Under -W error the warning is raised: it
    # must come out as a finding about the profile, not as a crash.
    body = text_edit("{find: '[[]loader-warning-as-error', replace: x}")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        label, where, message = the_problem(HEADER + body)
    assert (label, where) == ("<profile>", f"{EDIT}.find")
    assert "regular expression" in message


def test_ambiguous_regex_is_otherwise_what_python_accepts() -> None:
    body = text_edit("{find: '[[]loader-warning-only', replace: x}")
    with pytest.warns(FutureWarning):
        profile = load_profile_text(HEADER + body)
    assert profile.configs[0].edits == (TextEdit(find="[[]loader-warning-only", replace="x"),)


def test_keys_must_be_strings() -> None:
    assert problems_of(HEADER + "1: x\n") == [
        "<profile>: keys are strings, got the integer 1; allowed keys: schema, title_id, name, "
        "description, target, textures, media, configs, binary"
    ]
    label, where, message = the_problem(HEADER + "target: {true: 1}\n")
    assert (label, where) == ("<profile>", "target")
    assert "keys are strings, got the boolean true" in message


# ----------------------------------------------------------------------------------
# Problems in patches.yaml
# ----------------------------------------------------------------------------------


def builds_document(builds: str) -> str:
    return HEADER + "builds:\n" + builds


def patch_document(patch: str) -> str:
    """Return a patches.yaml with one build holding the given patch (a flow mapping)."""
    return builds_document(f'  - build_id: "{BUILD_ID}"\n    patches:\n      - {patch}\n')


BUILD = "builds[0]"
PATCH = "builds[0].patches[0]"
HUNK = "builds[0].patches[0].hunks[0]"
ONE_HUNK = '[{offset: 1, expected_bytes: "00", patch_bytes: "01"}]'
WITH_BASE = "    image_base: 0x7100000000\n"

PATCHES_CASES = [
    # ------------------------------------------------------------------ top level
    (f'title_id: "{TITLE_ID}"\nbuilds: []\n', "schema", "required"),
    (f'schema: 3\ntitle_id: "{TITLE_ID}"\nbuilds: []\n', "schema", "must be 1"),
    ("schema: 1\nbuilds: []\n", "title_id", "required"),
    (
        f"schema: 1\ntitle_id: {OCTAL_LOOKING_ID}\nbuilds: []\n",
        "title_id",
        "put it in quotes",
    ),
    (f'schema: 1\ntitle_id: "{TITLE_ID[:8]}"\nbuilds: []\n', "title_id", "16 hex digits"),
    (HEADER, "builds", "required"),
    (HEADER + "builds: {}\n", "builds", "must be a list"),
    (HEADER + "builds:\n", "builds", "null"),
    (HEADER + "builds: []\npatches: []\n", "patches", "allowed keys: schema, title_id, builds"),
    (HEADER + "builds: []\ntextures: {}\n", "textures", "unknown key"),
    # ---------------------------------------------------------------------- build
    (
        builds_document("  - 5\n"),
        BUILD,
        "must be a mapping (build_id, module, label, image_base, patches)",
    ),
    (builds_document("  - patches: []\n"), f"{BUILD}.build_id", "required"),
    (
        builds_document("  - build_id: 1234567812345678\n    patches: []\n"),
        f"{BUILD}.build_id",
        "the integer 1234567812345678: put it in quotes",
    ),
    (builds_document('  - build_id: "ABC"\n    patches: []\n'), f"{BUILD}.build_id", "'ABC'"),
    (builds_document('  - build_id: "XYZW"\n    patches: []\n'), f"{BUILD}.build_id", "'XYZW'"),
    (builds_document('  - build_id: ""\n    patches: []\n'), f"{BUILD}.build_id", "''"),
    (builds_document('  - build_id: "AB CD"\n    patches: []\n'), f"{BUILD}.build_id", "'AB CD'"),
    (
        builds_document(f'  - build_id: "{"AB" * 33}"\n    patches: []\n'),
        f"{BUILD}.build_id",
        "at most 64",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    module: Main\n    patches: []\n'),
        f"{BUILD}.module",
        "lowercase letters and digits",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    module: "sub-sdk"\n    patches: []\n'),
        f"{BUILD}.module",
        "'sub-sdk'",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    module: "main.npdm"\n    patches: []\n'),
        f"{BUILD}.module",
        "lowercase letters and digits",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    module: ""\n    patches: []\n'),
        f"{BUILD}.module",
        "lowercase letters and digits",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    module: 5\n    patches: []\n'),
        f"{BUILD}.module",
        "put it in quotes",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    label: 1.0\n    patches: []\n'),
        f"{BUILD}.label",
        "the number 1.0: put it in quotes",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    image_base: -1\n    patches: []\n'),
        f"{BUILD}.image_base",
        "negative",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    image_base: 1.5\n    patches: []\n'),
        f"{BUILD}.image_base",
        "the number 1.5",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    image_base: true\n    patches: []\n'),
        f"{BUILD}.image_base",
        "the boolean true",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    image_base: "0x"\n    patches: []\n'),
        f"{BUILD}.image_base",
        "the string '0x'",
    ),
    (builds_document(f'  - build_id: "{BUILD_ID}"\n'), f"{BUILD}.patches", "required"),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    patches: {{}}\n'),
        f"{BUILD}.patches",
        "must be a list",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    patches: [5]\n'),
        PATCH,
        "must be a mapping (name, category, description, enabled, hunks)",
    ),
    (
        builds_document(f'  - build_id: "{BUILD_ID}"\n    version: 1\n    patches: []\n'),
        f"{BUILD}.version",
        "allowed keys: build_id, module, label, image_base, patches",
    ),
    # ---------------------------------------------------------------------- patch
    (patch_document(f"{{hunks: {ONE_HUNK}}}"), f"{PATCH}.name", "required"),
    (patch_document(f'{{name: "two words", hunks: {ONE_HUNK}}}'), f"{PATCH}.name", "'two words'"),
    (patch_document(f'{{name: "a/b", hunks: {ONE_HUNK}}}'), f"{PATCH}.name", "letters, digits"),
    (patch_document(f'{{name: "", hunks: {ONE_HUNK}}}'), f"{PATCH}.name", "letters, digits"),
    (patch_document(f"{{name: 5, hunks: {ONE_HUNK}}}"), f"{PATCH}.name", "put it in quotes"),
    (
        patch_document(f"{{name: p, description: 5, hunks: {ONE_HUNK}}}"),
        f"{PATCH}.description",
        "put it in quotes",
    ),
    (
        patch_document(f'{{name: p, enabled: "no", hunks: {ONE_HUNK}}}'),
        f"{PATCH}.enabled",
        "true or false",
    ),
    (patch_document("{name: p}"), f"{PATCH}.hunks", "required"),
    (patch_document("{name: p, hunks: []}"), f"{PATCH}.hunks", "must not be empty"),
    (patch_document("{name: p, hunks: {offset: 1}}"), f"{PATCH}.hunks", "must be a list"),
    (
        patch_document("{name: p, hunks: [5]}"),
        HUNK,
        "must be a mapping (offset, address, patch_bytes, expected_sha256, window_offset, window_length, expected_bytes)",
    ),
    (
        patch_document(f"{{name: p, hunks: {ONE_HUNK}, kind: x}}"),
        f"{PATCH}.kind",
        "allowed keys: name, category, description, enabled, hunks",
    ),
    # ----------------------------------------------------------------------- hunk
    (hunk_document('{expected_bytes: "00", patch_bytes: "01"}'), HUNK, "one of offset / address"),
    (
        hunk_document('{offset: 1, address: 2, expected_bytes: "00", patch_bytes: "01"}'),
        HUNK,
        "only one",
    ),
    (
        hunk_document('{offset: -1, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "-0x1",
    ),
    (
        hunk_document('{offset: 1.5, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "the number 1.5",
    ),
    (
        hunk_document('{offset: abc, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "the string 'abc'",
    ),
    (
        hunk_document('{offset: true, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "the boolean true",
    ),
    (
        hunk_document('{offset: , expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "null",
    ),
    # One byte too far: 0xFFFFFFFD + 4 = 2**32 + 1.
    (
        hunk_document(
            '{offset: 0xFFFFFFFD, expected_bytes: "00 00 00 00", patch_bytes: "01 01 01 01"}'
        ),
        f"{HUNK}.offset",
        "0x100000000",
    ),
    (
        hunk_document('{offset: 0x100000000, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.offset",
        "0x100000000",
    ),
    # 0x70FFFFFFFF - 0x7100000000 = -1: an address below the image base.
    (
        hunk_document(
            '{address: 0x70FFFFFFFF, expected_bytes: "00", patch_bytes: "01"}', WITH_BASE
        ),
        f"{HUNK}.address",
        "-0x1",
    ),
    # 0x7200000000 - 0x7100000000 = 2**32: past the end of any module image.
    (
        hunk_document(
            '{address: 0x7200000000, expected_bytes: "00", patch_bytes: "01"}', WITH_BASE
        ),
        f"{HUNK}.address",
        "image_base 0x7100000000",
    ),
    # Without image_base the address is the offset, and this one is far beyond 4 GiB.
    (
        hunk_document('{address: 0x71001A2B40, expected_bytes: "00", patch_bytes: "01"}'),
        f"{HUNK}.address",
        "0x71001A2B40",
    ),
    (
        hunk_document('{address: "zz", expected_bytes: "00", patch_bytes: "01"}', WITH_BASE),
        f"{HUNK}.address",
        "the string 'zz'",
    ),
    (
        hunk_document('{offset: 1, patch_bytes: "01"}'),
        HUNK,
        "one of expected_sha256 / expected_bytes is required",
    ),
    (hunk_document('{offset: 1, expected_bytes: "00"}'), f"{HUNK}.patch_bytes", "required"),
    (
        hunk_document('{offset: 1, expected_bytes: 12345678, patch_bytes: "01 02 03 04"}'),
        f"{HUNK}.expected_bytes",
        "the integer 12345678: put it in quotes",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00 00 00 00", patch_bytes: 00000000}'),
        f"{HUNK}.patch_bytes",
        "the integer 0: put it in quotes",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00 00", patch_bytes: 10:20}'),
        f"{HUNK}.patch_bytes",
        "the integer 620: put it in quotes",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "1F 2 03", patch_bytes: "00 00 00"}'),
        f"{HUNK}.expected_bytes",
        "not a byte string",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "XY", patch_bytes: "00"}'),
        f"{HUNK}.expected_bytes",
        "not a byte string",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "", patch_bytes: "00"}'),
        f"{HUNK}.expected_bytes",
        "not a byte string",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00", patch_bytes: "0"}'),
        f"{HUNK}.patch_bytes",
        "not a byte string",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: [0, 0], patch_bytes: "01 01"}'),
        f"{HUNK}.expected_bytes",
        "a list",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00 00", patch_bytes: "01"}'),
        f"{HUNK}.patch_bytes",
        "same length",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00", patch_bytes: "01 02"}'),
        f"{HUNK}.patch_bytes",
        "holds 2 bytes but expected_bytes holds 1",
    ),
    (
        hunk_document('{offset: 1, expected_bytes: "00", patch_bytes: "01", bytes: "02"}'),
        f"{HUNK}.bytes",
        "allowed keys: offset, address, patch_bytes, expected_sha256, window_offset, window_length, expected_bytes",
    ),
]


@pytest.mark.parametrize(("patches", "path", "fragment"), PATCHES_CASES)
def test_patches_problems(patches: str, path: str, fragment: str) -> None:
    label, where, message = the_problem(HEADER, patches)
    assert (label, where) == ("<patches>", path)
    assert fragment in message


def test_patches_title_id_must_be_the_profiles() -> None:
    label, where, message = the_problem(
        HEADER, f'schema: 1\ntitle_id: "{OTHER_TITLE_ID}"\nbuilds: []\n'
    )
    assert (label, where) == ("<patches>", "title_id")
    assert OTHER_TITLE_ID in message
    assert TITLE_ID in message
    assert "<profile>" in message
    # Case is spelling, not identity.
    load_profile_text(HEADER, f'schema: 1\ntitle_id: "{TITLE_ID.lower()}"\nbuilds: []\n')


def test_duplicate_builds_are_found_through_case_and_zero_padding() -> None:
    padded = BUILD_ID.lower() + "00" * 12
    assert len(padded) == 64
    document = builds_document(
        f'  - build_id: "{BUILD_ID}"\n    patches: []\n'
        f'  - build_id: "{OTHER_BUILD_ID}"\n    patches: []\n'
        f'  - build_id: "{padded}"\n    module: main\n    patches: []\n'
    )
    label, where, message = the_problem(HEADER, document)
    assert (label, where) == ("<patches>", "builds[2].build_id")
    assert "builds[0]" in message

    # The same build ID in another module is another executable, and another ID that only
    # shares a prefix is another build.
    profile = load_profile_text(
        HEADER,
        builds_document(
            f'  - build_id: "{BUILD_ID}"\n    patches: []\n'
            f'  - build_id: "{BUILD_ID}"\n    module: sdk\n    patches: []\n'
            f'  - build_id: "{BUILD_ID[:-2]}"\n    patches: []\n'
            f'  - build_id: "00{BUILD_ID[:-2]}"\n    patches: []\n'
        ),
    )
    assert profile.patches is not None
    assert [(build.module, build.build_id) for build in profile.patches.builds] == [
        ("main", BUILD_ID),
        ("sdk", BUILD_ID),
        ("main", BUILD_ID[:-2]),
        ("main", "00" + BUILD_ID[:-2]),
    ]


def test_build_ids_are_stored_in_canonical_form() -> None:
    profile = load_profile_text(
        HEADER,
        builds_document(
            '  - build_id: "ab00cd00"\n    patches: []\n'
            '  - build_id: "0000"\n    module: sdk\n    patches: []\n'
            f'  - build_id: "{"EF" * 32}"\n    module: rtld\n    patches: []\n'
        ),
    )
    assert profile.patches is not None
    # Upper case, trailing zero bytes removed (interior ones kept), never empty.
    assert [build.build_id for build in profile.patches.builds] == ["AB00CD", "00", "EF" * 32]


def test_patch_names_are_unique_within_a_build_only() -> None:
    hunk = '{offset: 1, expected_bytes: "00", patch_bytes: "01"}'
    duplicated = builds_document(
        f'  - build_id: "{BUILD_ID}"\n    patches:\n'
        f"      - {{name: a, hunks: [{hunk}]}}\n"
        f"      - {{name: b, hunks: [{hunk}]}}\n"
        f"      - {{name: a, hunks: [{hunk}]}}\n"
    )
    label, where, message = the_problem(HEADER, duplicated)
    assert (label, where) == ("<patches>", "builds[0].patches[2].name")
    assert "patches[0]" in message

    profile = load_profile_text(
        HEADER,
        builds_document(
            f'  - build_id: "{BUILD_ID}"\n    patches:\n      - {{name: a, hunks: [{hunk}]}}\n'
            f"      - {{name: A, hunks: [{hunk}]}}\n"
            f'  - build_id: "{OTHER_BUILD_ID}"\n    patches:\n      - {{name: a, hunks: [{hunk}]}}\n'
        ),
    )
    assert profile.patches is not None
    names = [[patch.name for patch in build.patches] for build in profile.patches.builds]
    assert names == [["a", "A"], ["a"]]


def hunks_document(hunks: list[str]) -> str:
    listed = "".join(f"          - {hunk}\n" for hunk in hunks)
    return builds_document(
        f'  - build_id: "{BUILD_ID}"\n    patches:\n      - name: p\n        hunks:\n{listed}'
    )


def test_hunks_of_a_patch_must_not_overlap() -> None:
    four = 'expected_bytes: "00 00 00 00", patch_bytes: "01 01 01 01"'
    two = 'expected_bytes: "00 00", patch_bytes: "01 01"'
    # 0x100..0x103 and 0x103..0x104 share the byte at 0x103.
    label, where, message = the_problem(
        HEADER, hunks_document([f"{{offset: 0x100, {four}}}", f"{{offset: 0x103, {two}}}"])
    )
    assert (label, where) == ("<patches>", "builds[0].patches[0].hunks[1]")
    assert "hunks[0]" in message
    assert "0x103" in message

    # Order in the file does not matter: the hunk that starts later is the one reported.
    label, where, message = the_problem(
        HEADER, hunks_document([f"{{offset: 0x103, {two}}}", f"{{offset: 0x100, {four}}}"])
    )
    assert where == "builds[0].patches[0].hunks[0]"
    assert "hunks[1]" in message

    # Touching is not overlapping.
    profile = load_profile_text(
        HEADER, hunks_document([f"{{offset: 0x104, {two}}}", f"{{offset: 0x100, {four}}}"])
    )
    assert profile.patches is not None
    assert [hunk.offset for hunk in profile.patches.builds[0].patches[0].hunks] == [0x104, 0x100]


def test_a_long_hunk_overlaps_every_hunk_inside_it() -> None:
    sixteen = " ".join(["00"] * 16)
    two = 'expected_bytes: "00 00", patch_bytes: "01 01"'
    problems = problems_of(
        HEADER,
        hunks_document(
            [
                f"{{offset: 0x104, {two}}}",
                f'{{offset: 0x100, expected_bytes: "{sixteen}", patch_bytes: "{sixteen}"}}',
                f"{{offset: 0x108, {two}}}",
                f"{{offset: 0x110, {two}}}",
            ]
        ),
    )
    # 0x100..0x10F swallows the hunks at 0x104 and 0x108; the one at 0x110 is clear of it.
    assert located(problems) == [
        ("<patches>", "builds[0].patches[0].hunks[0]"),
        ("<patches>", "builds[0].patches[0].hunks[2]"),
    ]
    assert all("hunks[1]" in problem for problem in problems)


def test_hunks_of_different_patches_may_cover_the_same_bytes() -> None:
    hunk = '{offset: 0x100, expected_bytes: "00 00", patch_bytes: "01 01"}'
    profile = load_profile_text(
        HEADER,
        builds_document(
            f'  - build_id: "{BUILD_ID}"\n    patches:\n'
            f"      - {{name: thirty, hunks: [{hunk}]}}\n"
            f"      - {{name: sixty, enabled: false, hunks: [{hunk}]}}\n"
        ),
    )
    assert profile.patches is not None
    assert len(profile.patches.builds[0].patches) == 2


def test_byte_strings_hold_at_most_256_bytes() -> None:
    assert MAX_HUNK_BYTES == 256
    full = " ".join(["AB"] * 256)
    profile = load_profile_text(
        HEADER, hunk_document(f'{{offset: 0, expected_bytes: "{full}", patch_bytes: "{full}"}}')
    )
    assert profile.patches is not None
    assert profile.patches.builds[0].patches[0].hunks[0].patch == b"\xab" * 256

    too_long = " ".join(["AB"] * 257)
    problems = problems_of(
        HEADER,
        hunk_document(f'{{offset: 0, expected_bytes: "{too_long}", patch_bytes: "{too_long}"}}'),
    )
    assert located(problems) == [
        ("<patches>", f"{HUNK}.expected_bytes"),
        ("<patches>", f"{HUNK}.patch_bytes"),
    ]
    assert all("257" in problem and "256" in problem for problem in problems)


def many_hunks(count: int) -> str:
    """Return a build whose single patch has ``count`` 256-byte hunks, 4 KiB apart."""
    full = "AB" * 256
    return hunks_document(
        [
            f'{{offset: {index * 0x1000}, expected_bytes: "{full}", patch_bytes: "{full}"}}'
            for index in range(count)
        ]
    )


def test_patch_bytes_of_a_build_total_at_most_16_kib() -> None:
    assert MAX_PATCH_BYTES_PER_BUILD == 16 * 1024
    # 64 hunks of 256 bytes are exactly 16384 bytes.
    profile = load_profile_text(HEADER, many_hunks(64))
    assert profile.patches is not None
    assert sum(len(h.patch) for h in profile.patches.builds[0].patches[0].hunks) == 16384

    label, where, message = the_problem(HEADER, many_hunks(65))
    assert (label, where) == ("<patches>", "builds[0].patches")
    assert "16640" in message
    assert "16384" in message


def test_the_total_counts_every_patch_of_the_build_even_disabled_ones() -> None:
    full = "AB" * 256

    def patch(name: str, first: int, count: int, enabled: str) -> str:
        hunks = ", ".join(
            f'{{offset: {(first + index) * 0x1000}, expected_bytes: "{full}", patch_bytes: "{full}"}}'
            for index in range(count)
        )
        # Quoted: unquoted, YAML would read the names "on" and "off" as booleans.
        return f'      - {{name: "{name}", enabled: {enabled}, hunks: [{hunks}]}}\n'

    document = builds_document(
        f'  - build_id: "{BUILD_ID}"\n    patches:\n'
        + patch("on", 0, 33, "true")
        + patch("off", 33, 32, "false")
        + f'  - build_id: "{OTHER_BUILD_ID}"\n    patches:\n'
        + patch("other", 0, 64, "true")
    )
    # 33 + 32 hunks of 256 bytes in the first build; the second build is at its limit.
    label, where, message = the_problem(HEADER, document)
    assert (label, where) == ("<patches>", "builds[0].patches")
    assert str(65 * 256) in message


def test_an_unusable_image_base_does_not_cascade_into_its_hunks() -> None:
    document = hunk_document(
        '{address: 0x71001A2B40, expected_bytes: "00", patch_bytes: "01"}',
        "    image_base: nowhere\n",
    )
    label, where, _message = the_problem(HEADER, document)
    assert (label, where) == ("<patches>", "builds[0].image_base")


# ----------------------------------------------------------------------------------
# YAML itself
# ----------------------------------------------------------------------------------


def test_duplicate_keys_are_refused_with_their_position() -> None:
    # PyYAML would keep the last "max_width" without a word.
    text = HEADER + "target:\n  max_width: 1280\n  max_height: 720\n  max_width: 640\n"
    assert yaml.safe_load(text)["target"]["max_width"] == 640
    label, where, message = the_problem(text)
    assert (label, where) == ("<profile>", "line 6, column 3")
    assert "duplicate key 'max_width'" in message
    assert "line 4" in message


@pytest.mark.parametrize(
    ("body", "where", "key"),
    [
        ("name: a\nname: b\n", "line 4, column 1", "'name'"),
        ("target: {max_width: 1, max_width: 2}\n", "line 3, column 24", "'max_width'"),
        (
            "textures:\n  rules:\n    - match: a\n      skip: true\n      match: b\n",
            "line 7, column 7",
            "'match'",
        ),
        # An anchored mapping is checked as well, even when it is only ever merged.
        ("configs:\n  - &c {match: a, match: b}\n  - <<: *c\n", "line 4, column 19", "'match'"),
        ("schema: 1\n", "line 3, column 1", "'schema'"),
    ],
)
def test_duplicate_keys_anywhere(body: str, where: str, key: str) -> None:
    label, found, message = the_problem(HEADER + body)
    assert (label, found) == ("<profile>", where)
    assert f"duplicate key {key}" in message


def test_duplicate_keys_in_patches() -> None:
    document = hunk_document('{offset: 1, expected_bytes: "00", patch_bytes: "01", offset: 2}')
    label, where, message = the_problem(HEADER, document)
    # Locate the second "offset" in the text itself (lines and columns count from 1).
    lines = document.splitlines()
    (line,) = [number for number, text in enumerate(lines, 1) if "offset: 2" in text]
    column = lines[line - 1].index("offset: 2") + 1
    assert (label, where) == ("<patches>", f"line {line}, column {column}")
    assert "duplicate key 'offset'" in message


@pytest.mark.parametrize("text", ["", "\n\n", "# only a comment\n", "---\n", "~\n", "null\n"])
def test_empty_documents(text: str) -> None:
    problems = problems_of(text)
    assert len(problems) == 1
    assert problems[0].startswith("<profile>: the document is empty")
    problems = problems_of(HEADER, text)
    assert len(problems) == 1
    assert problems[0].startswith("<patches>: the document is empty")


@pytest.mark.parametrize(
    ("text", "got"),
    [
        ("- schema: 1\n", "a list"),
        ("just text\n", "the string 'just text'"),
        ("42\n", "the integer 42"),
    ],
)
def test_top_level_must_be_a_mapping(text: str, got: str) -> None:
    for problems, label in (
        (problems_of(text), "<profile>"),
        (problems_of(HEADER, text), "<patches>"),
    ):
        assert len(problems) == 1
        assert problems[0].startswith(f"{label}: the top level must be a mapping")
        assert problems[0].endswith(f"got {got}")


def test_syntax_errors_carry_line_and_column() -> None:
    # A tab cannot indent YAML: the error is the tab itself, first character of line 4.
    label, where, message = the_problem(HEADER + "target:\n\tmax_width: 1\n")
    assert (label, where) == ("<profile>", "line 4, column 1")
    assert message.startswith("YAML error: ")

    label, where, message = the_problem(HEADER, HEADER + "builds: [1, 2\nmore: x\n")
    assert label == "<patches>"
    assert where.startswith("line 4, column ")
    assert message.startswith("YAML error: ")


def test_a_second_document_is_an_error() -> None:
    label, where, message = the_problem(HEADER + "---\n" + HEADER)
    assert (label, where) == ("<profile>", "line 3, column 1")
    assert "document" in message


def test_tags_never_build_objects(tmp_path: Path) -> None:
    canary = tmp_path / "canary.txt"
    text = HEADER + f'name: !!python/object/apply:builtins.open ["{canary.as_posix()}", "w"]\n'
    label, where, message = the_problem(text)
    assert (label, where) == ("<profile>", "line 3, column 7")
    assert message.startswith("YAML error: ")
    assert not canary.exists()

    label, where, _message = the_problem(HEADER + "name: !custom x\n")
    assert (label, where) == ("<profile>", "line 3, column 7")


def test_scalars_yaml_cannot_convert_are_yaml_errors_not_crashes() -> None:
    # "2024-13-45" looks like a date to YAML, but month 13 does not exist.
    text = HEADER + ini_edit("{key: Day, value: 2024-13-45}")
    line = text.splitlines()[2]
    label, where, message = the_problem(text)
    assert (label, where) == ("<profile>", f"line 3, column {line.index('2024') + 1}")
    assert "quotes" in message

    digits = "9" * 5000  # more digits than Python converts
    label, where, message = the_problem(HEADER + f"target: {{max_width: {digits}}}\n")
    assert (label, where) == ("<profile>", "line 3, column 21")
    assert message.startswith("YAML error: ")

    label, where, message = the_problem(HEADER + "name: !!bool maybe\n")
    assert (label, where) == ("<profile>", "line 3, column 7")


def test_integers_too_long_to_print_are_reported_not_printed() -> None:
    huge = "0x" + "F" * 5000  # YAML reads it; Python refuses to print 6000 decimal digits
    for body, path in [
        (f"name: {huge}\n", "name"),
        (f"target: {{max_width: {huge}}}\n", "target"),  # a valid, absurd width: no problem
        (ini_edit(f"{{key: k, value: {huge}}}"), f"{EDIT}.value"),
        (
            f"media: {{rules: [{{match: a, audio_bitrate: {huge}}}]}}\n",
            "media.rules[0].audio_bitrate",
        ),
        (f"media: {{rules: [{{match: a, max_fps: {huge}}}]}}\n", "media.rules[0].max_fps"),
        # Not as a key: YAML limits a plain key to 1024 characters (a syntax error).
    ]:
        try:
            profile = load_profile_text(HEADER + body)
        except ProfileError as error:
            assert len(error.problems) == 1
            assert "20000-bit integer" in error.problems[0]
            assert error.problems[0].startswith(f"<profile>: {path}".rstrip(": ") + ": ")
        else:
            assert path == "target"
            assert profile.target.max_width == 16**5000 - 1


def test_characters_yaml_forbids_are_located() -> None:
    label, where, message = the_problem(HEADER + 'name: "a\x07b"\n')
    assert (label, where) == ("<profile>", "line 3, column 9")
    assert "U+0007" in message


def test_documents_nested_too_deeply_are_refused() -> None:
    depth = 5000
    problems = problems_of(HEADER + "name: " + "[" * depth + "]" * depth + "\n")
    assert problems == ["<profile>: YAML error: the document is nested too deeply to be a profile"]


# ----------------------------------------------------------------------------------
# Every problem at once
# ----------------------------------------------------------------------------------

MANY_PROBLEMS = (
    HEADER
    + """\
nme: "typo"
target: {max_width: 0}
textures:
  defaults: {filter: sharp}
  rules:
    - match: "ui/**"
      skip: true
      scale: 0.5
media:
  rules:
    - match: "*.mp4"
      crf: 99
configs:
  - match: "a.ini"
    format: ini
    edits:
      - {key: K, value: 1.5}
binary: {mode: zip}
"""
)

MANY_PATCH_PROBLEMS = f"""\
schema: 1
title_id: "{OTHER_TITLE_ID}"
builds:
  - build_id: "{BUILD_ID}"
    module: MAIN
    patches:
      - name: p
        hunks:
          - {{offset: 0, expected_bytes: "00", patch_bytes: "0102"}}
"""


def test_all_problems_of_both_files_are_reported_together() -> None:
    with pytest.raises(ProfileError) as caught:
        load_profile_text(MANY_PROBLEMS, MANY_PATCH_PROBLEMS)
    error = caught.value
    assert located(list(error.problems)) == [
        ("<profile>", "nme"),
        ("<profile>", "target.max_width"),
        ("<profile>", "textures.defaults.filter"),
        ("<profile>", "textures.rules[0].scale"),
        ("<profile>", "media.rules[0].crf"),
        ("<profile>", "configs[0].edits[0].value"),
        ("<profile>", "binary.mode"),
        ("<patches>", "title_id"),
        ("<patches>", "builds[0].module"),
        ("<patches>", "builds[0].patches[0].hunks[0].patch_bytes"),
    ]


def test_a_broken_file_does_not_hide_the_problems_of_the_other() -> None:
    problems = problems_of("schema: [1\n", MANY_PATCH_PROBLEMS)
    # profile.yaml is not YAML at all, so its title ID is unknown: patches.yaml is still
    # validated, minus the comparison of the two IDs.
    assert [problem.split(": ", 1)[0] for problem in problems] == ["<profile>"] + ["<patches>"] * 2
    assert located(problems)[1:] == [
        ("<patches>", "builds[0].module"),
        ("<patches>", "builds[0].patches[0].hunks[0].patch_bytes"),
    ]

    problems = problems_of(MANY_PROBLEMS, "builds: [\n")
    assert len(problems) == 8
    assert [problem.split(": ", 1)[0] for problem in problems] == ["<profile>"] * 7 + ["<patches>"]


def test_error_is_a_bea_error_with_a_headline_and_one_problem_per_line() -> None:
    with pytest.raises(ProfileError) as caught:
        load_profile_text(
            HEADER + "target: {max_width: 0, max_height: 0}\n",
            "- not a mapping\n",
            source="mine.yaml",
            patches_source="mine-patches.yaml",
        )
    error = caught.value
    assert isinstance(error, BeaError)
    assert isinstance(error.problems, tuple)
    assert [problem.split(": ", 1)[0] for problem in error.problems] == [
        "mine.yaml",
        "mine.yaml",
        "mine-patches.yaml",
    ]
    lines = str(error).splitlines()
    assert len(lines) == 4
    assert "mine.yaml" in lines[0]
    assert "3 problems" in lines[0]
    assert [line.strip() for line in lines[1:]] == list(error.problems)

    with pytest.raises(ProfileError) as caught:
        load_profile_text("schema: 1\n")
    assert "1 problem)" in str(caught.value).splitlines()[0]


# ----------------------------------------------------------------------------------
# Properties over whole documents
# ----------------------------------------------------------------------------------

#: Keys docs/PROFILES.md marks as required, wherever they appear in the full documents.
REQUIRED_KEYS = {
    "schema",
    "title_id",
    "match",
    "format",
    "edits",
    "key",
    "value",
    "path",
    "find",
    "replace",
    "builds",
    "build_id",
    "patches",
    "hunks",
    "patch_bytes",
}

type Steps = tuple[str | int, ...]


def walk(node: object, steps: Steps = ()) -> Iterator[Steps]:
    """Yield the location of every value below ``node`` (mapping keys and list indexes)."""
    children: list[tuple[str | int, object]] = []
    if isinstance(node, dict):
        children = list(node.items())
    elif isinstance(node, list):
        children = list(enumerate(node))
    for step, child in children:
        yield (*steps, step)
        yield from walk(child, (*steps, step))


def dotted(steps: Steps) -> str:
    """Write a location the way the loader does: ``a.b[0].c``."""
    text = ""
    for step in steps:
        if isinstance(step, int):
            text += f"[{step}]"
        else:
            text += f".{step}" if text else step
    return text


def edited(document: Any, steps: Steps, *, value: object = None, delete: bool = False) -> str:
    """Return the YAML of ``document`` with the value at ``steps`` replaced or removed."""
    clone = copy.deepcopy(document)
    parent = clone
    for step in steps[:-1]:
        parent = parent[step]
    if delete:
        del parent[steps[-1]]
    else:
        parent[steps[-1]] = value
    return yaml.safe_dump(clone, sort_keys=False)


def is_required(steps: Steps) -> bool:
    key = steps[-1]
    if "value" in steps[:-1]:
        return False  # inside a free-form JSON value
    if key == "name":
        return "patches" in steps  # a patch needs a name, a profile does not
    return key in REQUIRED_KEYS


def mapping_keys(document: object) -> list[Steps]:
    return [steps for steps in walk(document) if isinstance(steps[-1], str)]


PROFILE_DATA = yaml.safe_load(FULL_PROFILE)
PATCHES_DATA = yaml.safe_load(FULL_PATCHES)


@pytest.mark.parametrize("steps", mapping_keys(PROFILE_DATA), ids=dotted)
def test_removing_a_key_of_the_profile_fails_exactly_when_it_is_required(steps: Steps) -> None:
    text = edited(PROFILE_DATA, steps, delete=True)
    if is_required(steps):
        label, where, message = the_problem(text)
        assert (label, where) == ("<profile>", dotted(steps))
        assert "required key is missing" in message
    else:
        load_profile_text(text)


@pytest.mark.parametrize("steps", mapping_keys(PATCHES_DATA), ids=dotted)
def test_removing_a_key_of_the_patches_fails_exactly_when_it_is_required(steps: Steps) -> None:
    text = edited(PATCHES_DATA, steps, delete=True)
    key = steps[-1]
    if is_required(steps):
        label, where, message = the_problem(FULL_PROFILE, text)
        assert (label, where) == ("<patches>", dotted(steps))
        assert "required key is missing" in message
    elif key == "expected_bytes":
        # The hunks of the document are verified by literal bytes: one of the two forms
        # of verification is then missing.
        label, where, message = the_problem(FULL_PROFILE, text)
        assert (label, where) == ("<patches>", dotted(steps[:-1]))
        assert "one of expected_sha256 / expected_bytes is required" in message
    elif key in ("offset", "address"):
        # Each hunk of the document has exactly one of the two.
        label, where, message = the_problem(FULL_PROFILE, text)
        assert (label, where) == ("<patches>", dotted(steps[:-1]))
        assert "one of offset / address is required" in message
    elif key == "image_base":
        # Without it the base is 0 and the address of the second hunk is no longer inside
        # a module image.
        label, where, _message = the_problem(FULL_PROFILE, text)
        assert (label, where) == ("<patches>", "builds[0].patches[0].hunks[1].address")
    else:
        load_profile_text(FULL_PROFILE, text)


HOSTILE_VALUES = [
    None,
    True,
    0,
    -1,
    1.5,
    float("nan"),
    2**70,
    "",
    "two\nlines",
    [],
    ["x"],
    {},
    {"k": 1},
    datetime.date(2024, 1, 31),
    b"\x00\x01",
]


def test_no_value_anywhere_makes_the_loader_crash() -> None:
    """Whatever replaces a value, the outcome is a profile or a ``ProfileError``."""
    outcomes = {"valid": 0, "invalid": 0}
    for document, as_patches in ((PROFILE_DATA, False), (PATCHES_DATA, True)):
        label = "<patches>: " if as_patches else "<profile>: "
        for steps in walk(document):
            for value in HOSTILE_VALUES:
                text = edited(document, steps, value=value)
                try:
                    if as_patches:
                        load_profile_text(FULL_PROFILE, text)
                    else:
                        load_profile_text(text)
                except ProfileError as error:
                    outcomes["invalid"] += 1
                    assert error.problems, (dotted(steps), value)
                    assert all(problem.startswith(label) for problem in error.problems)
                else:
                    outcomes["valid"] += 1
    # Both outcomes happen: free-form JSON values and informative strings accept a lot.
    assert outcomes["valid"] > 50
    assert outcomes["invalid"] > 500


# ----------------------------------------------------------------------------------
# Directories
# ----------------------------------------------------------------------------------


def write_profile(directory: Path, profile: str = HEADER, patches: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.yaml").write_bytes(profile.encode("utf-8"))
    if patches is not None:
        (directory / "patches.yaml").write_bytes(patches.encode("utf-8"))
    return directory


def directory_problems(directory: Path) -> list[str]:
    with pytest.raises(ProfileError) as caught:
        load_profile(directory)
    return list(caught.value.problems)


def test_load_profile_reads_both_files_and_remembers_the_directory(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / TITLE_ID, FULL_PROFILE, FULL_PATCHES)
    profile = load_profile(directory)
    assert profile.source_dir == directory
    assert dataclasses_equal_ignoring_source(profile, load_profile_text(FULL_PROFILE, FULL_PATCHES))
    assert profile.patches is not None
    assert len(profile.patches.builds) == 2
    assert profile.describe()["source_dir"] == str(directory)


def dataclasses_equal_ignoring_source(left: Profile, right: Profile) -> bool:
    import dataclasses

    return dataclasses.replace(left, source_dir=None) == dataclasses.replace(right, source_dir=None)


def test_patches_file_is_optional(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / "anything")
    profile = load_profile(directory)
    assert profile.patches is None
    assert profile == Profile(title_id=TITLE_ID, source_dir=directory)


def test_problems_of_a_directory_name_the_file(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / "broken", MANY_PROBLEMS, MANY_PATCH_PROBLEMS)
    with pytest.raises(ProfileError) as caught:
        load_profile(directory)
    error = caught.value
    assert [problem.split(": ", 1)[0] for problem in error.problems] == (
        ["profile.yaml"] * 7 + ["patches.yaml"] * 3
    )
    assert error.problems[1].startswith("profile.yaml: target.max_width: ")
    assert error.problems[-1].startswith(
        "patches.yaml: builds[0].patches[0].hunks[0].patch_bytes: "
    )
    assert "profile.yaml" in error.problems[7]  # "... but profile.yaml says ..."
    headline = str(error).splitlines()[0]
    assert str(directory) in headline
    assert "10 problems" in headline


def test_directory_named_like_a_title_id_must_be_the_title_id(tmp_path: Path) -> None:
    wrong = write_profile(tmp_path / OTHER_TITLE_ID)
    problems = directory_problems(wrong)
    assert len(problems) == 1
    label, where, message = problems[0].split(": ", 2)
    assert (label, where) == ("profile.yaml", "title_id")
    assert TITLE_ID in message
    assert OTHER_TITLE_ID in message

    # Case does not matter, and names that are not title IDs are not checked at all.
    for name in (TITLE_ID, TITLE_ID.lower() + "-copy", "_template", "my profile", TITLE_ID[:-1]):
        directory = write_profile(tmp_path / "ok" / name)
        assert load_profile(directory).title_id == TITLE_ID
    lower = write_profile(tmp_path / "lower" / TITLE_ID.lower())
    assert load_profile(lower).title_id == TITLE_ID


def test_directory_name_check_uses_the_real_name_of_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_profile(tmp_path / OTHER_TITLE_ID)
    monkeypatch.chdir(tmp_path / OTHER_TITLE_ID)
    problems = directory_problems(Path("."))
    assert located(problems) == [("profile.yaml", "title_id")]

    monkeypatch.chdir(tmp_path)
    write_profile(tmp_path / TITLE_ID)
    profile = load_profile(Path(TITLE_ID))
    assert profile.source_dir == Path(TITLE_ID)


def test_directory_name_is_not_checked_when_the_title_id_is_unusable(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / OTHER_TITLE_ID, 'schema: 1\ntitle_id: "nope"\n')
    assert located(directory_problems(directory)) == [("profile.yaml", "title_id")]


def test_missing_directory_or_profile_file(tmp_path: Path) -> None:
    for directory in (tmp_path / "missing", tmp_path):
        problems = directory_problems(directory)
        assert len(problems) == 1
        assert problems[0].startswith("profile.yaml: file not found")
        assert str(directory) in problems[0]

    # A patches.yaml on its own is still validated.
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "patches.yaml").write_bytes(MANY_PATCH_PROBLEMS.encode("utf-8"))
    problems = directory_problems(lonely)
    assert problems[0].startswith("profile.yaml: file not found")
    assert located(problems[1:]) == [
        ("patches.yaml", "builds[0].module"),
        ("patches.yaml", "builds[0].patches[0].hunks[0].patch_bytes"),
    ]

    # A file where a directory is expected.
    problems = directory_problems(write_profile(tmp_path / "real") / "profile.yaml")
    assert len(problems) == 1
    assert problems[0].startswith("profile.yaml: file not found")


def test_a_file_that_cannot_be_read_is_a_problem(tmp_path: Path) -> None:
    # A directory in place of the file cannot be opened on any platform, whoever asks.
    directory = write_profile(tmp_path / "p")
    (directory / "patches.yaml").mkdir()
    problems = directory_problems(directory)
    assert len(problems) == 1
    assert problems[0].startswith("patches.yaml: cannot be read")

    other = tmp_path / "q"
    (other / "profile.yaml").mkdir(parents=True)
    problems = directory_problems(other)
    assert len(problems) == 1
    assert problems[0].startswith("profile.yaml: cannot be read")


def test_files_must_be_utf8(tmp_path: Path) -> None:
    data = HEADER.encode("utf-8") + b'name: "caf\xe9"\n'
    directory = tmp_path / "latin1"
    directory.mkdir()
    (directory / "profile.yaml").write_bytes(data)
    problems = directory_problems(directory)
    assert len(problems) == 1
    assert problems[0].startswith("profile.yaml: is not UTF-8 text")
    assert f"offset {data.index(0xE9)}" in problems[0]
    assert "0xE9" in problems[0]

    utf16 = tmp_path / "utf16"
    utf16.mkdir()
    (utf16 / "profile.yaml").write_bytes(HEADER.encode("utf-8"))
    (utf16 / "patches.yaml").write_bytes((HEADER + "builds: []\n").encode("utf-16"))
    problems = directory_problems(utf16)
    assert len(problems) == 1
    assert problems[0].startswith("patches.yaml: is not UTF-8 text")
    assert "offset 0)" in problems[0]


def test_utf8_byte_order_mark_and_crlf_files_load(tmp_path: Path) -> None:
    directory = tmp_path / "bom"
    directory.mkdir()
    text = (
        HEADER + 'name: "N\N{LATIN SMALL LETTER E WITH ACUTE}"\ntarget:\n  max_width: 640\n'
    ).replace("\n", "\r\n")
    (directory / "profile.yaml").write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))
    profile = load_profile(directory)
    assert profile.name == "N\N{LATIN SMALL LETTER E WITH ACUTE}"
    assert profile.target.max_width == 640

    # With a mark in front, a position on the first line is still counted from column 1.
    (directory / "profile.yaml").write_bytes(codecs.BOM_UTF8 + b"\tschema: 1\n")
    assert directory_problems(directory)[0].startswith("profile.yaml: line 1, column 1: ")


def test_files_larger_than_the_limit_are_refused_unread(tmp_path: Path) -> None:
    assert MAX_PROFILE_FILE_BYTES == 256 * 1024
    head = HEADER.encode("utf-8") + b"# "
    directory = tmp_path / "big"
    directory.mkdir()

    at_limit = head + b"x" * (MAX_PROFILE_FILE_BYTES - len(head) - 1) + b"\n"
    assert len(at_limit) == MAX_PROFILE_FILE_BYTES
    (directory / "profile.yaml").write_bytes(at_limit)
    assert load_profile(directory).title_id == TITLE_ID

    # One byte more: refused, although the content is a perfectly valid profile.
    (directory / "profile.yaml").write_bytes(
        head + b"x" * (MAX_PROFILE_FILE_BYTES - len(head)) + b"\n"
    )
    problems = directory_problems(directory)
    assert len(problems) == 1
    assert problems[0].startswith("profile.yaml: is larger than 262144 bytes")

    (directory / "profile.yaml").write_bytes(HEADER.encode("utf-8"))
    (directory / "patches.yaml").write_bytes(b"# " + b"x" * MAX_PROFILE_FILE_BYTES + b"\n")
    problems = directory_problems(directory)
    assert len(problems) == 1
    assert problems[0].startswith("patches.yaml: is larger than 262144 bytes")


def test_find_profile_dir(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    on_disk = write_profile(profiles / TITLE_ID.lower())
    (profiles / OTHER_TITLE_ID).mkdir()
    write_profile(profiles / "_template")

    assert find_profile_dir(profiles, TITLE_ID) == on_disk
    assert find_profile_dir(profiles, TITLE_ID.lower()) == on_disk
    found = find_profile_dir(profiles, TITLE_ID)
    assert found is not None
    assert found.name == TITLE_ID.lower()
    assert load_profile(found).title_id == TITLE_ID
    # A directory without profile.yaml is not a profile; a template is not a game.
    assert find_profile_dir(profiles, OTHER_TITLE_ID) is None
    assert find_profile_dir(profiles, "_template") is None
    assert find_profile_dir(profiles, "") is None
    assert find_profile_dir(profiles, "..") is None
    assert find_profile_dir(tmp_path / "missing", TITLE_ID) is None
    assert find_profile_dir(on_disk / "profile.yaml", TITLE_ID) is None

    write_profile(profiles / OTHER_TITLE_ID)
    assert find_profile_dir(profiles, OTHER_TITLE_ID) == profiles / OTHER_TITLE_ID


def test_list_profile_dirs(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    # Lower case sorts after upper case: the order is by name, not by ID value.
    names = [
        f"{(0x0100 << 48) | 0xBEA0BEA0C000:016X}",
        f"{(0x0100 << 48) | 0xBEA0BEA0A000:016x}",
        f"{(0x0100 << 48) | 0xBEA0BEA0E000:016X}",
    ]
    for name in names:
        write_profile(profiles / name)
    (profiles / f"{(0x0100 << 48) | 0xBEA0BEA0D000:016X}").mkdir()  # no profile.yaml
    write_profile(profiles / "_template")
    write_profile(profiles / "notes")
    write_profile(profiles / names[0][:-1])  # 15 digits
    write_profile(profiles / (names[0] + "0"))  # 17 digits
    write_profile(profiles / ("G" + names[0][1:]))  # not hexadecimal
    (profiles / ("F" * 16)).write_bytes(b"a file that is named like an ID\n")
    (profiles / "README.md").write_bytes(b"guide\n")

    found = list_profile_dirs(profiles)
    assert found == [profiles / names[0], profiles / names[2], profiles / names[1]]
    assert [path.name for path in found] == sorted(names)
    assert all(isinstance(path, Path) for path in found)

    assert list_profile_dirs(tmp_path / "missing") == []
    assert list_profile_dirs(profiles / "README.md") == []
    assert list_profile_dirs(profiles / "_template") == []


def test_validate_profile_dir_returns_problems_instead_of_raising(tmp_path: Path) -> None:
    valid = write_profile(tmp_path / TITLE_ID, FULL_PROFILE, FULL_PATCHES)
    assert validate_profile_dir(valid) == []

    broken = write_profile(tmp_path / "broken", MANY_PROBLEMS, MANY_PATCH_PROBLEMS)
    problems = validate_profile_dir(broken)
    assert problems == directory_problems(broken)
    assert len(problems) == 10

    missing = validate_profile_dir(tmp_path / "missing")
    assert len(missing) == 1
    assert missing[0].startswith("profile.yaml: file not found")

    misnamed = validate_profile_dir(write_profile(tmp_path / OTHER_TITLE_ID))
    assert located(misnamed) == [("profile.yaml", "title_id")]


def test_validate_profile_dir_turns_any_other_failure_into_one_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(directory: Path) -> Profile:
        raise PermissionError(13, "the disk said no")

    monkeypatch.setattr(loader, "load_profile", failing)
    problems = validate_profile_dir(tmp_path / "anything")
    assert len(problems) == 1
    assert "the disk said no" in problems[0]
    assert "PermissionError" in problems[0]
    assert str(tmp_path / "anything") in problems[0]

    def interrupted(directory: Path) -> Profile:
        raise KeyboardInterrupt

    # Not a failure of the profile: the user wants out.
    monkeypatch.setattr(loader, "load_profile", interrupted)
    with pytest.raises(KeyboardInterrupt):
        validate_profile_dir(tmp_path / "anything")


# ----------------------------------------------------------------------------------
# Verification by hash window (the form repository profiles use)
# ----------------------------------------------------------------------------------

SHA = "ab" * 32


def test_hash_window_hunk_with_defaults() -> None:
    profile = load_profile_text(
        HEADER,
        hunk_document(
            f'{{offset: 0x100, patch_bytes: "1F 20 03 D5", expected_sha256: "{SHA.upper()}"}}'
        ),
    )
    assert profile.patches is not None
    (hunk,) = profile.patches.builds[0].patches[0].hunks
    # The window starts at the patch and is 32 bytes long by default; the hash is stored
    # in lower case and no expected bytes exist.
    assert hunk == Hunk(
        offset=0x100,
        patch=b"\x1f\x20\x03\xd5",
        expected_sha256=SHA,
        window_offset=0x100,
        window_length=32,
    )
    assert hunk.expected is None and hunk.end == 0x104


def test_hash_window_defaults_to_the_patch_length_when_longer() -> None:
    long_patch = " ".join(["11"] * 40)
    profile = load_profile_text(
        HEADER,
        hunk_document(f'{{offset: 8, patch_bytes: "{long_patch}", expected_sha256: "{SHA}"}}'),
    )
    assert profile.patches is not None
    assert profile.patches.builds[0].patches[0].hunks[0].window_length == 40


def test_explicit_window() -> None:
    profile = load_profile_text(
        HEADER,
        hunk_document(
            f'{{offset: 0x110, patch_bytes: "00 00", expected_sha256: "{SHA}", '
            "window_offset: 0x100, window_length: 64}"
        ),
    )
    assert profile.patches is not None
    hunk = profile.patches.builds[0].patches[0].hunks[0]
    assert (hunk.window_offset, hunk.window_length) == (0x100, 64)


@pytest.mark.parametrize(
    ("hunk", "where", "fragment"),
    [
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA}", expected_bytes: "00"}}',
            HUNK,
            "give only one",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA[:-2]}"}}',
            f"{HUNK}.expected_sha256",
            "64 hexadecimal digits",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{"zz" * 32}"}}',
            f"{HUNK}.expected_sha256",
            "64 hexadecimal digits",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA}", window_length: 15}}',
            f"{HUNK}.window_length",
            "16",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA}", window_length: 257}}',
            f"{HUNK}.window_length",
            "256",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA}", window_length: true}}',
            f"{HUNK}.window_length",
            "boolean",
        ),
        # The window must contain the patched bytes, on both sides.
        (
            f'{{offset: 0x10, patch_bytes: "00", expected_sha256: "{SHA}", window_offset: 0x11}}',
            f"{HUNK}.window_offset",
            "must contain",
        ),
        (
            f'{{offset: 0x30, patch_bytes: "00 00", expected_sha256: "{SHA}", window_offset: 0x10, window_length: 33}}',
            f"{HUNK}.window_offset",
            "must contain",
        ),
        (
            f'{{offset: 0, patch_bytes: "00", expected_sha256: "{SHA}", window_offset: -1}}',
            f"{HUNK}.window_offset",
            "outside",
        ),
        (
            '{offset: 0, patch_bytes: "00", expected_bytes: "00", window_offset: 0}',
            f"{HUNK}.window_offset",
            "goes with expected_sha256",
        ),
        (
            '{offset: 0, patch_bytes: "00", expected_bytes: "00", window_length: 32}',
            f"{HUNK}.window_length",
            "goes with expected_sha256",
        ),
        (
            '{offset: 0, patch_bytes: "00", expected_sha256: 12}',
            f"{HUNK}.expected_sha256",
            "must be a string",
        ),
    ],
)
def test_hash_window_problems(hunk: str, where: str, fragment: str) -> None:
    label, found, message = the_problem(HEADER, hunk_document(hunk))
    assert (label, found) == ("<patches>", where)
    assert fragment in message


def test_window_that_exactly_covers_the_patch_is_valid() -> None:
    patch = " ".join(["00"] * 16)
    profile = load_profile_text(
        HEADER,
        hunk_document(
            f'{{offset: 0x40, patch_bytes: "{patch}", expected_sha256: "{SHA}", '
            "window_offset: 0x40, window_length: 16}"
        ),
    )
    assert profile.patches is not None


# ----------------------------------------------------------------------------------
# Patch categories
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(PATCH_CATEGORIES))
def test_every_documented_category_is_accepted(category: str) -> None:
    document = patch_document(f"{{name: p, category: {category}, hunks: {ONE_HUNK}}}")
    profile = load_profile_text(HEADER, document)
    assert profile.patches is not None
    assert profile.patches.builds[0].patches[0].category == category


def test_category_is_optional_for_the_engine() -> None:
    profile = load_profile_text(HEADER, patch_document(f"{{name: p, hunks: {ONE_HUNK}}}"))
    assert profile.patches is not None
    assert profile.patches.builds[0].patches[0].category is None


@pytest.mark.parametrize("category", ["cheat", "signature", "Framerate", "frame_rate", "", "drm"])
def test_unknown_categories_are_refused(category: str) -> None:
    document = patch_document(f'{{name: p, category: "{category}", hunks: {ONE_HUNK}}}')
    label, where, message = the_problem(HEADER, document)
    assert (label, where) == ("<patches>", f"{PATCH}.category")
    assert "must be one of" in message and "framerate" in message


def test_category_suggestion_for_a_near_miss() -> None:
    document = patch_document(f"{{name: p, category: framerat, hunks: {ONE_HUNK}}}")
    _label, _where, message = the_problem(HEADER, document)
    assert "framerate" in message
