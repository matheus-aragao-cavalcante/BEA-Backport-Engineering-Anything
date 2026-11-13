"""Tests for bea.profile.schema: limit arithmetic, rule resolution and the plain data model.

Expectations come from docs/PROFILES.md (the normative description), never from the
implementation: the tables were worked out by hand, and the properties are checked with
exact rational arithmetic and a mip chain built level by level, so that the
floating-point shortcuts of the implementation are compared with something independent.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest

from bea.profile import schema
from bea.profile.schema import (
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
    generic_profile,
    halvings_needed,
    is_power_of_two,
    scale_factor,
    target_size,
)

HD = (1280, 720)
#: A box so large that it never binds: the way to exercise ``scale`` on its own.
NO_BOX = (4096, 4096)
TARGET = Target(max_width=1280, max_height=720)


def synthetic_title_id() -> str:
    # Built at run time so that no title-ID-shaped literal appears in this file.
    return f"{(0x0100 << 48) | 0xBEA0BEA0B000:016X}"


def synthetic_build_id(seed: int = 1) -> str:
    # Twenty significant bytes, as most linkers emit; synthesized, never a real ID.
    return bytes(range(seed, seed + 20)).hex().upper()


# ----------------------------------------------------------------------------------
# Constants and enumerations (normative values of docs/PROFILES.md)
# ----------------------------------------------------------------------------------


def test_documented_constants() -> None:
    assert schema.SCHEMA_VERSION == 1
    assert (schema.DEFAULT_TARGET_WIDTH, schema.DEFAULT_TARGET_HEIGHT) == (1280, 720)
    # "a byte string holds at most 256 bytes; the patch_bytes of one build total at most 16 KiB"
    assert schema.MAX_HUNK_BYTES == 256
    assert schema.MAX_PATCH_BYTES_PER_BUILD == 16384


@pytest.mark.parametrize(
    ("enumeration", "spellings"),
    [
        (Strategy, {"auto", "mip_drop", "resample"}),
        (ResampleFilter, {"nearest", "box", "bilinear", "hamming", "bicubic", "lanczos"}),
        (PowerOfTwo, {"preserve", "ignore"}),
        (AlphaMode, {"straight", "weighted"}),
        (ConfigFormat, {"ini", "json", "text"}),
        (IfMissing, {"error", "add", "ignore"}),
        (ExefsMode, {"ips", "replace"}),
    ],
)
def test_enumerations_use_the_documented_spellings(
    enumeration: type[Strategy], spellings: set[str]
) -> None:
    assert {member.value for member in enumeration} == spellings
    # The YAML loader builds members from the documented text.
    assert all(enumeration(text).value == text for text in spellings)


def test_every_public_name_is_exported() -> None:
    for name in schema.__all__:
        assert hasattr(schema, name), name
    defined = {
        name
        for name, value in vars(schema).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == schema.__name__
    }
    assert defined <= set(schema.__all__)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, True),
        (2, True),
        (4, True),
        (1024, True),
        (1 << 20, True),
        (0, False),
        (-2, False),
        (-1, False),
        (3, False),
        (6, False),
        (1023, False),
        (1025, False),
        (720, False),
    ],
)
def test_is_power_of_two(value: int, expected: bool) -> None:
    assert is_power_of_two(value) is expected


# ----------------------------------------------------------------------------------
# scale_factor: hand-computed table
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "box", "scale", "expected"),
    [
        # The box alone: the tighter of the two ratios wins.
        (2560, 1440, HD, None, 0.5),
        (3840, 2160, HD, None, 1 / 3),
        (2048, 2048, HD, None, 0.3515625),  # 720/2048 beats 1280/2048
        (4096, 2048, HD, None, 0.3125),  # 1280/4096 beats 720/2048
        (2000, 1000, HD, None, 0.64),  # 1280/2000 beats 720/1000
        (720, 1280, HD, None, 0.5625),  # portrait: only the height binds
        (1, 4096, HD, None, 0.17578125),
        # Already compliant: exactly 1.0, never more (nothing is ever enlarged).
        (1280, 720, HD, None, 1.0),
        (640, 360, HD, None, 1.0),
        (1, 1, HD, None, 1.0),
        (1280, 1, HD, None, 1.0),
        # A scale without a binding box is the factor itself.
        (1024, 512, NO_BOX, 0.5, 0.5),
        (640, 360, HD, 0.5, 0.5),
        (100, 100, HD, 0.3, 0.3),
        # Box and scale together: the smaller one applies.
        (2048, 1024, HD, 0.9, 0.625),
        (2048, 1024, HD, 0.25, 0.25),
        (2048, 1024, HD, 0.625, 0.625),
        # A scale of 1 (or a meaningless larger one) changes nothing.
        (2560, 1440, HD, 1.0, 0.5),
        (640, 360, HD, 1.0, 1.0),
        (640, 360, HD, 2.0, 1.0),
    ],
)
def test_scale_factor(
    width: int, height: int, box: tuple[int, int], scale: float | None, expected: float
) -> None:
    assert scale_factor(width, height, box[0], box[1], scale) == pytest.approx(expected, rel=1e-12)


def test_scale_factor_is_exactly_one_for_compliant_images() -> None:
    # Callers compare with 1.0 to decide that a file needs no work: no rounding noise allowed.
    for width, height in [(1280, 720), (1279, 719), (3, 7), (1, 720), (1000, 3)]:
        assert scale_factor(width, height, 1280, 720) == 1.0
        assert scale_factor(width, height, 1280, 720, 1.0) == 1.0


# ----------------------------------------------------------------------------------
# halvings_needed: hand-computed table
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "factor", "expected"),
    [
        (2048, 2048, 0.3515625, 2),  # 2048 -> 1024 -> 512 <= 720
        (4096, 2048, 0.3125, 2),  # 4096 -> 2048 -> 1024 <= 1280
        (2560, 1440, 0.5, 1),  # exactly one level: 1280x720
        (3840, 2160, 1 / 3, 2),  # 1920x1080 is still too large, 960x540 fits
        (2000, 1000, 0.64, 1),
        (1000, 1000, 0.5, 1),  # an exact fit is a fit
        (1000, 1000, 0.49, 2),  # 500 > 490
        (1024, 1024, 0.9, 1),  # halving is the smallest possible step
        (5, 5, 0.5, 1),  # mip levels round down: 5 -> 2 <= 2.5
        (7, 3, 0.5, 1),  # 7 -> 3 <= 3.5 and 3 -> 1 <= 1.5
        (1, 4096, 0.17578125, 3),  # the 1-pixel side cannot shrink: only the height counts
        (4096, 1, 0.3125, 2),
        (2, 4096, 0.17578125, 3),
        (8192, 16, 0.15625, 3),
        (1, 1, 0.5, 0),  # nothing left to halve
        (3, 1, 0.1, 1),  # 3 -> 1, then stuck
        # Nothing to do.
        (2048, 2048, 1.0, 0),
        (2048, 2048, 1.5, 0),
    ],
)
def test_halvings_needed(width: int, height: int, factor: float, expected: int) -> None:
    assert halvings_needed(width, height, factor) == expected


# ----------------------------------------------------------------------------------
# target_size: hand-computed table, both power-of-two modes
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "box", "scale", "preserve", "ignore"),
    [
        # Plain downscales into the default box.
        ((2560, 1440), HD, None, (1280, 720), (1280, 720)),
        ((3840, 2160), HD, None, (1280, 720), (1280, 720)),
        ((2000, 1000), HD, None, (1280, 640), (1280, 640)),
        ((1920, 1200), HD, None, (1152, 720), (1152, 720)),
        ((720, 1280), HD, None, (405, 720), (405, 720)),
        ((4000, 3000), HD, None, (960, 720), (960, 720)),
        ((1023, 1023), HD, None, (720, 720), (720, 720)),
        # 768 * 1280/1366 = 719.65: the free dimension goes to the nearest pixel.
        ((1366, 768), HD, None, (1280, 720), (1280, 720)),
        # The documented example: "2048x2048 in a 1280x720 box becomes 512x512;
        # with `ignore` it becomes 720x720".
        ((2048, 2048), HD, None, (512, 512), (720, 720)),
        ((4096, 2048), HD, None, (1024, 512), (1280, 640)),
        ((4096, 4096), (1365, 1365), None, (1024, 1024), (1365, 1365)),
        ((2048, 1024), (1024, 512), None, (1024, 512), (1024, 512)),
        # 1xN and Nx1: a side of one pixel stays one pixel.
        ((1, 4096), HD, None, (1, 512), (1, 720)),
        ((4096, 1), HD, None, (1024, 1), (1280, 1)),
        ((1, 1000), HD, None, (1, 720), (1, 720)),  # 1000 is not a power of two
        ((2, 4096), HD, None, (1, 512), (1, 720)),
        ((8192, 16), HD, None, (1024, 2), (1280, 3)),  # 16 * 0.15625 = 2.5 rounds half up
        ((16, 8192), HD, None, (1, 512), (1, 720)),
        # Already compliant: returned untouched, whatever the mode.
        ((1280, 720), HD, None, (1280, 720), (1280, 720)),
        ((640, 360), HD, None, (640, 360), (640, 360)),
        ((1024, 512), HD, None, (1024, 512), (1024, 512)),
        ((1, 1), HD, None, (1, 1), (1, 1)),
        ((1280, 1), HD, None, (1280, 1), (1280, 1)),
        ((640, 360), HD, 1.0, (640, 360), (640, 360)),
        # A scale without a binding box.
        ((1024, 512), NO_BOX, 0.5, (512, 256), (512, 256)),
        ((256, 256), NO_BOX, 0.75, (128, 128), (192, 192)),
        ((1000, 600), NO_BOX, 0.5, (500, 300), (500, 300)),
        ((640, 360), HD, 0.5, (320, 180), (320, 180)),
        ((1500, 1000), HD, 0.3, (450, 300), (450, 300)),
        ((100, 100), NO_BOX, 0.57, (57, 57), (57, 57)),  # 100 * 0.57 is 56.99999999999999
        # A scale is a limit too: 500.5 pixels allowed means 500, exactly what dropping
        # one mip level of the same texture gives.
        ((1001, 601), NO_BOX, 0.5, (500, 300), (500, 300)),
        ((1025, 513), NO_BOX, 0.5, (512, 256), (512, 256)),
        ((7, 7), NO_BOX, 0.5, (3, 3), (3, 3)),
        ((5, 5), NO_BOX, 0.5, (2, 2), (2, 2)),
        ((3, 3), NO_BOX, 0.5, (1, 1), (1, 1)),
        ((10, 10), NO_BOX, 0.25, (2, 2), (2, 2)),
        ((999, 999), NO_BOX, 0.1, (99, 99), (99, 99)),
        ((1024, 1024), NO_BOX, 0.9, (512, 512), (921, 921)),
        # Box and scale together.
        ((2048, 1024), HD, 0.9, (1024, 512), (1280, 640)),  # the box is tighter
        ((2048, 1024), HD, 0.25, (512, 256), (512, 256)),  # the scale is tighter
        ((2000, 1000), HD, 0.7, (1280, 640), (1280, 640)),
        ((2560, 1440), HD, 1.0, (1280, 720), (1280, 720)),
    ],
)
def test_target_size(
    source: tuple[int, int],
    box: tuple[int, int],
    scale: float | None,
    preserve: tuple[int, int],
    ignore: tuple[int, int],
) -> None:
    assert target_size(*source, *box, scale=scale) == preserve  # PRESERVE is the default
    assert target_size(*source, *box, scale=scale, power_of_two=PowerOfTwo.PRESERVE) == preserve
    assert target_size(*source, *box, scale=scale, power_of_two=PowerOfTwo.IGNORE) == ignore


# ----------------------------------------------------------------------------------
# Properties, checked with exact arithmetic over a grid of awkward sizes
# ----------------------------------------------------------------------------------

SIDES = [1, 2, 3, 5, 7, 16, 100, 255, 256, 257, 720, 1000, 1024, 1080, 1366, 2048, 4096, 4097]
BOXES = [(1280, 720), (1920, 1080), (1, 1), (64, 64), (333, 77), (4096, 4096)]
#: Scales as the decimals a profile author would write (``None``: no scale).
SCALES = [
    None,
    Fraction(1, 2),
    Fraction(1, 4),
    Fraction(3, 4),
    Fraction(3, 10),
    Fraction(57, 100),
    Fraction(9, 10),
    Fraction(1),
]

type Case = tuple[int, int, tuple[int, int], Fraction | None]


def grid() -> Iterator[Case]:
    for width in SIDES:
        for height in SIDES:
            for box in BOXES:
                for scale in SCALES:
                    yield width, height, box, scale


def exact_factor(width: int, height: int, box: tuple[int, int], scale: Fraction | None) -> Fraction:
    """The uniform factor of docs/PROFILES.md, as an exact rational."""
    factor = min(Fraction(1), Fraction(box[0], width), Fraction(box[1], height))
    return factor if scale is None else min(factor, scale)


def largest_allowed(
    width: int, height: int, box: tuple[int, int], scale: Fraction | None
) -> tuple[int, int]:
    """The largest whole size the limits allow per side (a side cannot go below one pixel)."""
    limit_width = min(Fraction(width), Fraction(box[0]))
    limit_height = min(Fraction(height), Fraction(box[1]))
    if scale is not None:
        limit_width = min(limit_width, width * scale)
        limit_height = min(limit_height, height * scale)
    return max(1, math.floor(limit_width)), max(1, math.floor(limit_height))


def mip_chain(width: int, height: int) -> list[tuple[int, int]]:
    """Sizes of a complete mip chain, built level by level the way a texture stores it."""
    levels = [(width, height)]
    while levels[-1] != (1, 1):
        previous_width, previous_height = levels[-1]
        levels.append((max(1, previous_width // 2), max(1, previous_height // 2)))
    return levels


def mips_to_drop(width: int, height: int, factor: Fraction) -> int:
    """Index of the first mip level that shrank by at least ``factor`` (exact arithmetic)."""
    for index, (level_width, level_height) in enumerate(mip_chain(width, height)):
        # A side that reached one pixel cannot shrink any more: it no longer counts.
        width_done = level_width <= width * factor or level_width == 1
        height_done = level_height <= height * factor or level_height == 1
        if width_done and height_done:
            return index
    raise AssertionError("the 1x1 level always qualifies")


def sized(case: Case, mode: PowerOfTwo) -> tuple[int, int]:
    width, height, box, scale = case
    return target_size(
        width,
        height,
        box[0],
        box[1],
        scale=None if scale is None else float(scale),
        power_of_two=mode,
    )


def test_mip_chain_helper_matches_a_chain_written_out_by_hand() -> None:
    assert mip_chain(8, 2) == [(8, 2), (4, 1), (2, 1), (1, 1)]
    assert mip_chain(5, 3) == [(5, 3), (2, 1), (1, 1)]
    assert mip_chain(1, 1) == [(1, 1)]


def test_result_is_never_larger_than_the_source() -> None:
    for case in grid():
        for mode in PowerOfTwo:
            new_width, new_height = sized(case, mode)
            assert 1 <= new_width <= case[0], (case, mode)
            assert 1 <= new_height <= case[1], (case, mode)


def test_result_never_exceeds_the_limits() -> None:
    # "The limits of a file are the box max_width x max_height and, if set, the uniform
    # scale. The result [...] never exceeds the limits."
    for case in grid():
        allowed_width, allowed_height = largest_allowed(*case)
        for mode in PowerOfTwo:
            new_width, new_height = sized(case, mode)
            assert new_width <= allowed_width, (case, mode, new_width)
            assert new_height <= allowed_height, (case, mode, new_height)


def test_compliant_sources_are_returned_untouched() -> None:
    for case in grid():
        width, height, box, scale = case
        if exact_factor(width, height, box, scale) == 1:
            for mode in PowerOfTwo:
                assert sized(case, mode) == (width, height), (case, mode)


def test_aspect_ratio_is_kept_within_one_pixel() -> None:
    # There must be one uniform factor f for which both sides are less than a pixel
    # away from the exact size (width * f, height * f).
    for case in grid():
        width, height = case[0], case[1]
        for mode in PowerOfTwo:
            new_width, new_height = sized(case, mode)
            lowest = max(Fraction(new_width - 1, width), Fraction(new_height - 1, height))
            highest = min(Fraction(new_width + 1, width), Fraction(new_height + 1, height))
            assert lowest < highest, (case, mode, new_width, new_height)


def test_power_of_two_sources_stay_power_of_two_under_preserve() -> None:
    for case in grid():
        width, height = case[0], case[1]
        if is_power_of_two(width) and is_power_of_two(height):
            new_width, new_height = sized(case, PowerOfTwo.PRESERVE)
            assert is_power_of_two(new_width) and is_power_of_two(new_height), case


def test_preserve_only_matters_for_power_of_two_sources() -> None:
    for case in grid():
        width, height = case[0], case[1]
        preserved = sized(case, PowerOfTwo.PRESERVE)
        ignored = sized(case, PowerOfTwo.IGNORE)
        if is_power_of_two(width) and is_power_of_two(height):
            # Halving can only undershoot what a proportional fit would give.
            assert preserved[0] <= ignored[0] and preserved[1] <= ignored[1], case
        else:
            assert preserved == ignored, case


def test_a_proportional_fit_uses_the_box_fully() -> None:
    # Without a scale, the side that binds lands exactly on its limit.
    for width, height, box, scale in grid():
        if scale is None and exact_factor(width, height, box, None) < 1:
            new_width, new_height = target_size(
                width, height, box[0], box[1], power_of_two=PowerOfTwo.IGNORE
            )
            assert new_width == box[0] or new_height == box[1], (width, height, box)


def test_fitting_the_box_twice_changes_nothing() -> None:
    for width, height, box, scale in grid():
        if scale is None:
            for mode in PowerOfTwo:
                once = target_size(width, height, box[0], box[1], power_of_two=mode)
                assert target_size(*once, box[0], box[1], power_of_two=mode) == once
                assert scale_factor(*once, box[0], box[1]) == 1.0


def test_halvings_needed_is_the_number_of_mip_levels_to_drop() -> None:
    for width, height, box, scale in grid():
        exact = exact_factor(width, height, box, scale)
        factor = scale_factor(
            width, height, box[0], box[1], None if scale is None else float(scale)
        )
        expected = 0 if exact == 1 else mips_to_drop(width, height, exact)
        assert halvings_needed(width, height, factor) == expected, (width, height, box, scale)


def test_preserve_lands_on_a_level_of_the_mip_chain() -> None:
    # Which is what makes `mip_drop` and `resample` interchangeable for such textures.
    for case in grid():
        width, height, box, scale = case
        if is_power_of_two(width) and is_power_of_two(height):
            dropped = mips_to_drop(width, height, exact_factor(width, height, box, scale))
            assert sized(case, PowerOfTwo.PRESERVE) == mip_chain(width, height)[dropped], case


def test_halving_scales_agree_with_dropping_mip_levels() -> None:
    # scale 1/2^k applied by resampling gives the size of mip level k, for any texture.
    for width in SIDES:
        for height in SIDES:
            for levels, scale in ((1, 0.5), (2, 0.25), (3, 0.125)):
                expected = mip_chain(width, height)[min(levels, len(mip_chain(width, height)) - 1)]
                for mode in PowerOfTwo:
                    assert (
                        target_size(width, height, 1 << 20, 1 << 20, scale=scale, power_of_two=mode)
                        == expected
                    ), (width, height, scale, mode)


# ----------------------------------------------------------------------------------
# Texture rules
# ----------------------------------------------------------------------------------

DEFAULT_PARAMS = TextureParams(
    max_width=1280,
    max_height=720,
    scale=None,
    strategy=Strategy.AUTO,
    filter=ResampleFilter.LANCZOS,
    power_of_two=PowerOfTwo.PRESERVE,
    alpha=AlphaMode.STRAIGHT,
)


def test_texture_defaults_are_the_documented_ones() -> None:
    assert TextureDefaults() == TextureDefaults(
        scale=None,
        strategy=Strategy.AUTO,
        filter=ResampleFilter.LANCZOS,
        power_of_two=PowerOfTwo.PRESERVE,
        alpha=AlphaMode.STRAIGHT,
    )
    assert Target() == Target(max_width=1280, max_height=720)
    assert TextureSettings() == TextureSettings(enabled=True, defaults=TextureDefaults(), rules=())


def test_texture_without_rules_uses_target_and_defaults() -> None:
    assert TextureSettings().resolve("ui/logo.png", TARGET) == DEFAULT_PARAMS
    custom = TextureSettings(
        defaults=TextureDefaults(
            scale=0.5,
            strategy=Strategy.MIP_DROP,
            filter=ResampleFilter.BOX,
            power_of_two=PowerOfTwo.IGNORE,
            alpha=AlphaMode.WEIGHTED,
        )
    )
    assert custom.resolve("any/file.dds", Target(1920, 1080)) == TextureParams(
        max_width=1920,
        max_height=1080,
        scale=0.5,
        strategy=Strategy.MIP_DROP,
        filter=ResampleFilter.BOX,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )


def test_texture_first_matching_rule_wins() -> None:
    wide = TextureRule(match=("ui/**",), max_width=1920, max_height=1080)
    half = TextureRule(match=("**/*.png",), scale=0.5)
    settings = TextureSettings(rules=(wide, half))
    # Both rules match: only the first one is used, nothing of the second leaks in.
    assert settings.resolve("ui/menu/ok.png", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, max_width=1920, max_height=1080
    )
    assert settings.resolve("fx/spark.png", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, scale=0.5
    )
    assert settings.resolve("audio/readme.txt", TARGET) == DEFAULT_PARAMS

    reordered = TextureSettings(rules=(half, wide))
    assert reordered.resolve("ui/menu/ok.png", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, scale=0.5
    )
    assert reordered.resolve("ui/menu/ok.dds", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, max_width=1920, max_height=1080
    )


def test_texture_skip_rule_leaves_the_file_alone() -> None:
    settings = TextureSettings(
        rules=(
            TextureRule(match=("**/*_lut.*", "fonts/**"), skip=True),
            TextureRule(match=("**",), scale=0.5),
        )
    )
    assert settings.resolve("fx/grade_lut.png", TARGET) is None
    assert settings.resolve("fonts/main/glyphs.dds", TARGET) is None
    assert settings.resolve("fx/spark.png", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, scale=0.5
    )
    # A skip rule that comes later never undoes an earlier match.
    late_skip = TextureSettings(
        rules=(
            TextureRule(match=("fonts/**",), scale=0.5),
            TextureRule(match=("**",), skip=True),
        )
    )
    assert late_skip.resolve("fonts/a.png", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, scale=0.5
    )
    # "To restrict processing to an explicit list, end the rules with `match: "**"` / `skip: true`."
    assert late_skip.resolve("ui/a.png", TARGET) is None


def test_texture_excluded_files_fall_through_to_later_rules() -> None:
    characters = TextureRule(
        match=("characters/**/*.dds",),
        exclude=("characters/**/*_n.dds",),
        scale=0.5,
        strategy=Strategy.MIP_DROP,
    )
    normals = TextureRule(match=("**/*_n.dds",), filter=ResampleFilter.BILINEAR)
    settings = TextureSettings(rules=(characters, normals))
    assert settings.resolve("characters/hero/body.dds", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, scale=0.5, strategy=Strategy.MIP_DROP
    )
    # Excluded from the first rule, picked up by the second.
    assert settings.resolve("characters/hero/body_n.dds", TARGET) == dataclasses.replace(
        DEFAULT_PARAMS, filter=ResampleFilter.BILINEAR
    )
    # Excluded and matched by nothing else: target and defaults, not "left alone".
    alone = TextureSettings(rules=(characters,))
    assert alone.resolve("characters/hero/body_n.dds", TARGET) == DEFAULT_PARAMS
    # An exclusion wins even over a skip of the same rule.
    skipping = TextureSettings(
        rules=(TextureRule(match=("ui/**",), exclude=("ui/keep/**",), skip=True),)
    )
    assert skipping.resolve("ui/a.png", TARGET) is None
    assert skipping.resolve("ui/keep/a.png", TARGET) == DEFAULT_PARAMS


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"max_width": 1920}, {"max_width": 1920}),  # the height still comes from the target
        ({"max_height": 1080}, {"max_height": 1080}),
        ({"scale": 0.25}, {"scale": 0.25}),
        ({"strategy": Strategy.RESAMPLE}, {"strategy": Strategy.RESAMPLE}),
        ({"filter": ResampleFilter.NEAREST}, {"filter": ResampleFilter.NEAREST}),
        ({"power_of_two": PowerOfTwo.PRESERVE}, {"power_of_two": PowerOfTwo.PRESERVE}),
        ({"alpha": AlphaMode.STRAIGHT}, {"alpha": AlphaMode.STRAIGHT}),
    ],
)
def test_texture_rule_inherits_everything_it_does_not_set(
    override: dict[str, object], expected: dict[str, object]
) -> None:
    defaults = TextureDefaults(
        scale=0.75,
        strategy=Strategy.MIP_DROP,
        filter=ResampleFilter.BICUBIC,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )
    inherited = TextureParams(
        max_width=1600,
        max_height=900,
        scale=0.75,
        strategy=Strategy.MIP_DROP,
        filter=ResampleFilter.BICUBIC,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )
    target = Target(max_width=1600, max_height=900)

    bare = TextureSettings(defaults=defaults, rules=(TextureRule(match=("**",)),))
    assert bare.resolve("a/b.png", target) == inherited

    rule = TextureRule(match=("**",), **override)  # type: ignore[arg-type]
    settings = TextureSettings(defaults=defaults, rules=(rule,))
    assert settings.resolve("a/b.png", target) == dataclasses.replace(inherited, **expected)  # type: ignore[arg-type]


def test_texture_rule_can_override_every_setting_at_once() -> None:
    rule = TextureRule(
        match=("ui/**",),
        max_width=640,
        max_height=480,
        scale=0.5,
        strategy=Strategy.RESAMPLE,
        filter=ResampleFilter.HAMMING,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )
    assert TextureSettings(rules=(rule,)).resolve("ui/a.png", TARGET) == TextureParams(
        max_width=640,
        max_height=480,
        scale=0.5,
        strategy=Strategy.RESAMPLE,
        filter=ResampleFilter.HAMMING,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )


def test_textures_disabled_resolve_to_nothing() -> None:
    settings = TextureSettings(enabled=False, rules=(TextureRule(match=("**",), scale=0.5),))
    assert settings.resolve("ui/a.png", TARGET) is None
    assert TextureSettings(enabled=False).resolve("ui/a.png", TARGET) is None


def test_texture_params_as_dict_is_plain_json() -> None:
    params = TextureParams(
        max_width=1920,
        max_height=1080,
        scale=0.5,
        strategy=Strategy.MIP_DROP,
        filter=ResampleFilter.BOX,
        power_of_two=PowerOfTwo.IGNORE,
        alpha=AlphaMode.WEIGHTED,
    )
    expected = {
        "max_width": 1920,
        "max_height": 1080,
        "scale": 0.5,
        "strategy": "mip_drop",
        "filter": "box",
        "power_of_two": "ignore",
        "alpha": "weighted",
    }
    assert params.as_dict() == expected
    assert json.loads(json.dumps(params.as_dict())) == expected
    # Plain strings, not enum members: a cache key must not depend on how enums print.
    assert all(type(value) in (int, float, str) for value in params.as_dict().values())
    assert DEFAULT_PARAMS.as_dict()["scale"] is None
    assert json.loads(json.dumps(DEFAULT_PARAMS.as_dict()))["scale"] is None


# ----------------------------------------------------------------------------------
# Media rules
# ----------------------------------------------------------------------------------


def test_media_only_files_matched_by_a_rule_are_selected() -> None:
    assert MediaSettings().resolve("movies/intro.mp4", TARGET) is None
    settings = MediaSettings(rules=(MediaRule(match=("movies/**/*.mp4",)),))
    assert settings.resolve("movies/intro.mp4", TARGET) == MediaParams(
        max_width=1280,
        max_height=720,
        max_fps=None,
        crf=None,
        video_bitrate=None,
        audio_bitrate=None,
    )
    assert settings.resolve("movies/intro.webm", TARGET) is None
    assert settings.resolve("other/intro.mp4", TARGET) is None


def test_media_rule_inherits_the_box_and_nothing_else() -> None:
    rule = MediaRule(match=("movies/**",), max_height=540, max_fps=30, crf=23, audio_bitrate="128k")
    assert MediaSettings(rules=(rule,)).resolve("movies/a.mp4", Target(1600, 900)) == MediaParams(
        max_width=1600,  # inherited
        max_height=540,
        max_fps=30,
        crf=23,
        video_bitrate=None,  # "leave as is"
        audio_bitrate="128k",
    )
    full = MediaRule(match=("**",), max_width=854, max_height=480, video_bitrate="2500k")
    assert MediaSettings(rules=(full,)).resolve("a.mp4", TARGET) == MediaParams(
        max_width=854,
        max_height=480,
        max_fps=None,
        crf=None,
        video_bitrate="2500k",
        audio_bitrate=None,
    )


def test_media_first_match_skip_and_exclude() -> None:
    settings = MediaSettings(
        rules=(
            MediaRule(match=("movies/credits*",), skip=True),
            MediaRule(match=("movies/**",), exclude=("movies/raw/**",), crf=30),
            MediaRule(match=("**/*.mp4",), crf=18),
        )
    )
    assert settings.resolve("movies/credits_long.mp4", TARGET) is None
    first = settings.resolve("movies/intro.mp4", TARGET)
    assert first is not None and first.crf == 30
    # Excluded from the second rule: falls through to the third.
    fallen = settings.resolve("movies/raw/take1.mp4", TARGET)
    assert fallen is not None and fallen.crf == 18
    # Excluded and matched by nothing else: not selected at all.
    assert settings.resolve("movies/raw/take1.webm", TARGET) is None


def test_media_disabled_resolves_to_nothing() -> None:
    settings = MediaSettings(enabled=False, rules=(MediaRule(match=("**",), crf=20),))
    assert settings.resolve("movies/a.mp4", TARGET) is None
    assert MediaSettings().enabled is True


def test_media_params_as_dict_is_plain_json() -> None:
    params = MediaParams(
        max_width=1280,
        max_height=720,
        max_fps=29.97,
        crf=23,
        video_bitrate="4M",
        audio_bitrate=None,
    )
    expected = {
        "max_width": 1280,
        "max_height": 720,
        "max_fps": 29.97,
        "crf": 23,
        "video_bitrate": "4M",
        "audio_bitrate": None,
    }
    assert params.as_dict() == expected
    assert json.loads(json.dumps(params.as_dict())) == expected


# ----------------------------------------------------------------------------------
# Rule matchers
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        TextureRule(match=("ui/**/*.png", "*.dds"), exclude=("ui/fonts/**", "*_lut.dds")),
        MediaRule(match=("ui/**/*.png", "*.dds"), exclude=("ui/fonts/**", "*_lut.dds")),
        ConfigRule(
            match=("ui/**/*.png", "*.dds"),
            format=ConfigFormat.TEXT,
            edits=(TextEdit(find="a", replace="b"),),
            exclude=("ui/fonts/**", "*_lut.dds"),
        ),
    ],
)
def test_rule_matchers(rule: TextureRule | MediaRule | ConfigRule) -> None:
    assert rule.matches("ui/menu/ok.png")
    assert rule.matches("UI/Menu/OK.PNG")  # paths "are matched case-insensitively"
    assert rule.matches("textures/deep/rock.dds")  # no "/": the file name at any depth
    assert not rule.matches("ui/fonts/glyphs.png")
    assert not rule.matches("fx/grade_lut.dds")
    assert not rule.matches("menu/ok.png")  # "ui/**/*.png" is anchored at the root
    assert not rule.matches("audio/theme.wav")


def test_rules_compare_and_print_by_their_declared_fields() -> None:
    first = TextureRule(match=("ui/**",), exclude=("ui/x/**",), scale=0.5)
    second = TextureRule(match=("ui/**",), exclude=("ui/x/**",), scale=0.5)
    assert first == second and hash(first) == hash(second)
    assert first != dataclasses.replace(first, scale=0.25)
    assert "_matcher" not in repr(first)
    assert "ui/**" in repr(first)
    # A rule rebuilt from another one matches by its own patterns.
    rebuilt = dataclasses.replace(first, match=("fx/**",), exclude=())
    assert rebuilt.matches("fx/a.png") and not rebuilt.matches("ui/a.png")
    assert MediaRule(match=("a",)) == MediaRule(match=("a",))
    assert ConfigRule(match=("a",), format=ConfigFormat.INI, edits=()) == ConfigRule(
        match=("a",), format=ConfigFormat.INI, edits=()
    )


def test_rules_reject_invalid_globs_when_they_are_built() -> None:
    with pytest.raises(ValueError):
        TextureRule(match=("ui/*.png", ""))
    with pytest.raises(ValueError):
        MediaRule(match=("movies/**",), exclude=("/",))
    with pytest.raises(ValueError):
        ConfigRule(match=("tex_[z-a].ini",), format=ConfigFormat.INI, edits=())


def test_rule_without_patterns_matches_nothing() -> None:
    assert not TextureRule(match=()).matches("a.png")
    assert not MediaRule(match=(), exclude=("**",)).matches("a.mp4")


def test_schema_objects_are_immutable() -> None:
    rule = TextureRule(match=("**",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.skip = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        TARGET.max_width = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_PARAMS.scale = 0.5  # type: ignore[misc]
    profile = generic_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.name = "x"  # type: ignore[misc]


# ----------------------------------------------------------------------------------
# Config and binary data
# ----------------------------------------------------------------------------------


def test_config_data_defaults_are_the_documented_ones() -> None:
    assert IniEdit(section="Rendering", key="Scale", value="70").if_missing is IfMissing.ERROR
    assert IniEdit(section=None, key="k", value="v").section is None
    assert JsonEdit(path=("video", "maxFps"), value=30).if_missing is IfMissing.ERROR
    assert JsonEdit(path=("list", 0), value=None).value is None
    assert TextEdit(find=r"\d+", replace="2").count == 1

    rule = ConfigRule(
        match=("config/graphics.ini",),
        format=ConfigFormat.INI,
        edits=(IniEdit(section="Rendering", key="Scale", value="70"),),
    )
    assert rule.exclude == ()
    assert rule.encoding is None
    assert rule.required is True
    # The alias covers exactly the three kinds of edit.
    assert schema.ConfigEdit.__value__ == IniEdit | JsonEdit | TextEdit


def test_binary_data_defaults_are_the_documented_ones() -> None:
    hunk = Hunk(offset=0x1A2B3C, expected=b"\x08\x87\x80\x52", patch=b"\x08\x5a\x80\x52")
    patch = Patch(name="render-720p", hunks=(hunk,))
    assert patch.enabled is True
    assert patch.description == ""
    build = BuildPatches(build_id=synthetic_build_id(), patches=(patch,))
    assert build.module == "main"
    assert build.label is None
    assert BinarySettings() == BinarySettings(patchset_name=None, mode=ExefsMode.IPS)
    assert PatchSet(title_id=synthetic_title_id()).builds == ()


def test_patch_set_finds_builds_the_way_mod_loaders_compare_ids() -> None:
    main_id = synthetic_build_id(1)
    sdk_id = synthetic_build_id(101)
    main_build = BuildPatches(build_id=main_id, patches=(), label="v1.0.0")
    sdk_build = BuildPatches(build_id=sdk_id, patches=(), module="sdk")
    patches = PatchSet(title_id=synthetic_title_id(), builds=(main_build, sdk_build))

    assert patches.for_module("main", main_id) is main_build
    # "a 40-digit ID and the same ID followed by zeros are equal"
    assert patches.for_module("main", main_id + "00" * 12) is main_build
    assert patches.for_module("main", main_id + "00") is main_build
    assert patches.for_module("main", main_id.lower()) is main_build
    assert patches.for_module("sdk", sdk_id + "0000") is sdk_build
    # The module is part of the key.
    assert patches.for_module("sdk", main_id) is None
    assert patches.for_module("main", sdk_id) is None
    assert patches.for_module("subsdk0", main_id) is None
    # Zero padding is not prefix matching.
    assert patches.for_module("main", main_id[:-2]) is None
    assert patches.for_module("main", main_id + "01") is None
    assert patches.for_module("main", "00" + main_id) is None
    assert PatchSet(title_id=synthetic_title_id()).for_module("main", main_id) is None


def test_patch_set_matches_ids_declared_with_trailing_zero_bytes() -> None:
    # The profile may spell the full 32 bytes; the module reports the canonical short form.
    short = "AB00CD"
    declared = BuildPatches(build_id=short + "00" * 29, patches=())
    patches = PatchSet(title_id=synthetic_title_id(), builds=(declared,))
    assert patches.for_module("main", short) is declared
    assert patches.for_module("main", "ab00cd00") is declared
    assert patches.for_module("main", "AB00CE") is None


def test_patch_set_returns_the_first_of_several_candidates() -> None:
    build_id = synthetic_build_id()
    first = BuildPatches(build_id=build_id, patches=(), label="first")
    second = BuildPatches(build_id=build_id + "00", patches=(), label="second")
    assert PatchSet(synthetic_title_id(), (first, second)).for_module("main", build_id) is first


# ----------------------------------------------------------------------------------
# Profile
# ----------------------------------------------------------------------------------


def test_generic_profile_only_downsizes_textures() -> None:
    profile = generic_profile()
    assert profile.is_generic
    assert profile.title_id is None
    assert profile.name is None and profile.description is None
    assert profile.target == Target(1280, 720)
    assert profile.textures.resolve("any/texture.dds", profile.target) == DEFAULT_PARAMS
    # No media transcoding, no config edits, no binary patches.
    assert profile.media.resolve("movies/intro.mp4", profile.target) is None
    assert profile.configs == ()
    assert profile.patches is None
    assert profile.binary == BinarySettings()
    assert profile.source_dir is None


def test_generic_profile_takes_another_box() -> None:
    profile = generic_profile(max_width=1920, max_height=1080)
    assert profile.target == Target(1920, 1080)
    params = profile.textures.resolve("a.png", profile.target)
    assert params is not None
    assert (params.max_width, params.max_height) == (1920, 1080)
    assert generic_profile(854).target == Target(854, 720)
    assert generic_profile() == generic_profile()


def test_profile_defaults() -> None:
    title_id = synthetic_title_id()
    profile = Profile(title_id=title_id)
    assert not profile.is_generic
    assert profile.target == Target()
    assert profile.textures == TextureSettings()
    assert profile.media == MediaSettings()
    assert profile.configs == ()
    assert profile.binary == BinarySettings()
    assert profile.patches is None
    assert profile.source_dir is None


def test_profile_describe_is_a_small_json_summary(tmp_path: Path) -> None:
    title_id = synthetic_title_id()
    profile = Profile(
        title_id=title_id,
        name="Synthetic Quest",
        target=Target(1600, 900),
        source_dir=tmp_path / title_id,
    )
    summary = profile.describe()
    assert summary == {
        "title_id": title_id,
        "name": "Synthetic Quest",
        "generic": False,
        "source_dir": str(tmp_path / title_id),
        "target": {"max_width": 1600, "max_height": 900},
    }
    assert json.loads(json.dumps(summary)) == summary

    generic = generic_profile().describe()
    assert generic == {
        "title_id": None,
        "name": None,
        "generic": True,
        "source_dir": None,
        "target": {"max_width": 1280, "max_height": 720},
    }
    assert json.loads(json.dumps(generic)) == generic
