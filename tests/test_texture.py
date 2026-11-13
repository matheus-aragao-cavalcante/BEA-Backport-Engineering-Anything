"""Tests for bea.transformers.texture: decisions, resampling and lossless mip dropping.

Every image is generated here (gradients, shapes); DDS files are built with
bea.formats.dds.build_dds, which has its own byte-level tests.
"""

from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from bea.formats import dds
from bea.formats.imageinfo import ImageInfo, probe
from bea.ingest import open_input
from bea.model import GameInput
from bea.packagers.layeredfs import LayeredFSPackager
from bea.pipeline import BuildOptions, build
from bea.profile.schema import (
    AlphaMode,
    PowerOfTwo,
    Profile,
    ResampleFilter,
    Strategy,
    Target,
    TextureParams,
    TextureRule,
    TextureSettings,
)
from bea.toolbox import Toolbox
from bea.transformers.base import Action, PlanContext, Skip, SkipKind
from bea.transformers.texture import TextureTransformer, decide

APP_TEXT = f"{(0x0100 << 48) | 0xBEA0FACE0000:016X}"


def params(
    max_width: int = 1280,
    max_height: int = 720,
    *,
    scale: float | None = None,
    strategy: Strategy = Strategy.AUTO,
    power_of_two: PowerOfTwo = PowerOfTwo.PRESERVE,
    alpha: AlphaMode = AlphaMode.STRAIGHT,
    resample_filter: ResampleFilter = ResampleFilter.LANCZOS,
) -> TextureParams:
    return TextureParams(
        max_width, max_height, scale, strategy, resample_filter, power_of_two, alpha
    )


def png_info(width: int, height: int, bit_depth: int = 8) -> ImageInfo:
    return ImageInfo("png", width, height, pixel_format="RGBA8", bit_depth=bit_depth)


def dds_bytes(width: int, height: int, name: str, mips: int = 1, *, cubemap: bool = False) -> bytes:
    fmt = dds.format_by_name(name)
    layers = 6 if cubemap else 1
    chains = []
    for layer in range(layers):
        chain = []
        for level in range(mips):
            size = fmt.level_size(max(1, width >> level), max(1, height >> level))
            chain.append(bytes([(layer * 16 + level) % 256]) * size)
        chains.append(chain)
    return dds.build_dds(width, height, fmt, chains, cubemap=cubemap)


def dds_case(
    width: int, height: int, name: str, mips: int = 1, **kw: bool
) -> tuple[ImageInfo, dds.DdsHeader]:
    header = dds.parse_header(dds_bytes(width, height, name, mips, **kw))
    info = ImageInfo(
        "dds",
        width,
        height,
        mip_count=header.mip_count,
        layers=header.layers,
        pixel_format=header.format.name,
    )
    return info, header


def gradient(width: int, height: int, mode: str = "RGBA") -> Image.Image:
    red = Image.linear_gradient("L").resize((width, height))
    green = red.rotate(90).resize((width, height))
    blue = Image.radial_gradient("L").resize((width, height))
    image = Image.merge("RGB", (red, green, blue))
    draw = ImageDraw.Draw(image)
    draw.ellipse((width // 4, height // 4, width // 2, height // 2), fill=(250, 40, 40))
    if mode == "RGBA":
        image.putalpha(Image.linear_gradient("L").rotate(45).resize((width, height)))
    return image.convert(mode) if mode not in ("RGB", "RGBA") else image


def straight_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Reference for alpha "straight": every channel resized on its own."""
    if image.mode not in ("RGBA", "LA"):
        return image.resize(size, Image.Resampling.LANCZOS)
    channels = [channel.resize(size, Image.Resampling.LANCZOS) for channel in image.split()]
    return Image.merge(image.mode, channels)


def pixels(image: Image.Image) -> list[tuple[int, ...]]:
    return [image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)]  # type: ignore[misc]


def psnr(left: Image.Image, right: Image.Image) -> float:
    difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    mse = sum((index % 256) ** 2 * count for index, count in enumerate(difference.histogram())) / (
        left.width * left.height * 3
    )
    return math.inf if mse == 0 else 10 * math.log10(255 * 255 / mse)


# ------------------------------------------------------------------------- decisions


@pytest.mark.parametrize(
    ("size", "decided", "expected"),
    [
        ((2560, 1440), params(), (1280, 720)),
        ((3840, 2160), params(), (1280, 720)),
        ((2000, 1000), params(), (1280, 640)),
        ((2048, 2048), params(), (512, 512)),
        ((2048, 2048), params(power_of_two=PowerOfTwo.IGNORE), (720, 720)),
        ((1024, 1024), params(4096, 4096, scale=0.5), (512, 512)),
    ],
)
def test_png_resample_sizes(
    size: tuple[int, int], decided: TextureParams, expected: tuple[int, int]
) -> None:
    decision = decide(png_info(*size), decided)
    assert decision.kind == "action" and decision.params is not None
    assert (decision.params["width"], decision.params["height"]) == expected
    assert decision.params["op"] == "resample" and decision.params["container"] == "png"
    assert decision.details["planned"]["width"] == expected[0]
    assert f"-> {expected[0]}x{expected[1]}" in decision.summary


def test_within_limits_is_left_alone() -> None:
    assert decide(png_info(1280, 720), params()).kind == "none"
    assert decide(png_info(640, 360), params()).kind == "none"


def test_png_refusals() -> None:
    sixteen = decide(png_info(4096, 4096, bit_depth=16), params())
    assert (sixteen.kind, sixteen.skip_kind) == ("skip", SkipKind.UNSUPPORTED)
    assert sixteen.reason is not None and "16-bit" in sixteen.reason
    no_chain = decide(png_info(4096, 4096), params(strategy=Strategy.MIP_DROP))
    assert no_chain.reason is not None and "no mip chain" in no_chain.reason
    huge = decide(png_info(20000, 20000), params())
    assert huge.reason is not None and "too large" in huge.reason


def test_dds_auto_prefers_mip_drop() -> None:
    info, header = dds_case(2048, 2048, "BC7_UNORM", 12)
    decision = decide(info, params(), dds_header=header)
    assert decision.params == {"container": "dds", "op": "mip_drop", "levels": 2}
    assert "-> 512x512 (mip_drop 2)" in decision.summary
    one = decide(info, params(1024, 1024), dds_header=header)
    assert one.params == {"container": "dds", "op": "mip_drop", "levels": 1}


def test_dds_without_mips_and_without_encoder_is_unsupported() -> None:
    info, header = dds_case(2048, 2048, "BC7_UNORM", 1)
    decision = decide(info, params(), dds_header=header)
    assert (decision.kind, decision.skip_kind) == ("skip", SkipKind.UNSUPPORTED)
    assert decision.reason is not None and "BC7_UNORM" in decision.reason


def test_dds_without_mips_with_encoder_is_resampled() -> None:
    info, header = dds_case(2048, 2048, "BC1_UNORM", 1)
    decision = decide(info, params(), dds_header=header)
    assert decision.params is not None and decision.params["op"] == "resample"
    assert (decision.params["width"], decision.params["height"], decision.params["mips"]) == (
        512,
        512,
        1,
    )


@pytest.mark.parametrize(
    ("size", "name", "expected"),
    [
        ((2000, 1000), "BC3_UNORM", (1280, 640)),
        ((1920, 1080), "BC1_UNORM", (1280, 720)),
        ((1000, 600), "BC1_UNORM", (1000, 600)),  # already within the box: decision is "none"
        ((2004, 1004), "BC1_UNORM", (1280, 640)),  # 1280 x 641, rounded down to whole blocks
        ((2001, 1001), "BC1_UNORM", (1280, 640)),  # not a multiple of 4: no snapping
        ((2004, 1004), "R8G8B8A8_UNORM", (1280, 641)),  # uncompressed: no snapping
    ],
)
def test_dds_resample_sizes(size: tuple[int, int], name: str, expected: tuple[int, int]) -> None:
    info, header = dds_case(*size, name)
    decision = decide(info, params(strategy=Strategy.RESAMPLE), dds_header=header)
    if size == expected:
        assert decision.kind == "none"
        return
    assert decision.params is not None
    assert (decision.params["width"], decision.params["height"]) == expected


def test_short_mip_chain_drops_what_it_can() -> None:
    info, header = dds_case(4096, 4096, "BC7_UNORM", 3)  # needs 3 levels dropped, has 2 to spare
    decision = decide(info, params(), dds_header=header)
    assert decision.params == {"container": "dds", "op": "mip_drop", "levels": 2}
    assert decision.details["planned"]["note"] == "only 2 of 3 levels can be dropped"


def test_explicit_mip_drop_needs_a_chain() -> None:
    info, header = dds_case(2048, 2048, "BC1_UNORM", 1)
    decision = decide(info, params(strategy=Strategy.MIP_DROP), dds_header=header)
    assert decision.skip_kind is SkipKind.UNSUPPORTED


def test_cube_maps_drop_mips_but_are_not_resampled() -> None:
    info, header = dds_case(2048, 2048, "BC1_UNORM", 3, cubemap=True)
    assert decide(info, params(), dds_header=header).params == {
        "container": "dds",
        "op": "mip_drop",
        "levels": 2,
    }
    refused = decide(info, params(strategy=Strategy.RESAMPLE), dds_header=header)
    assert refused.reason is not None and "cube maps" in refused.reason


# ------------------------------------------------------------------------- transforms


def save(image: Image.Image, path: Path, fmt: str, **options: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format=fmt, **options)
    return path


def transform(src: Path, dst: Path, decision_params: dict) -> Image.Image:
    TextureTransformer().transform(src, dst, decision_params)
    image = Image.open(dst)
    image.load()
    return image


@pytest.mark.parametrize("mode", ["RGBA", "RGB", "L", "LA"])
def test_png_resample_matches_a_reference_resize(tmp_path: Path, mode: str) -> None:
    source = gradient(512, 256, mode if mode in ("RGB", "RGBA") else "RGB").convert(mode)
    src = save(source, tmp_path / "in.png", "PNG")
    config = {
        "container": "png",
        "op": "resample",
        "width": 256,
        "height": 128,
        "filter": "lanczos",
        "alpha": "straight",
    }
    result = transform(src, tmp_path / "out.png", config)
    assert result.size == (256, 128) and result.mode == mode
    reference = straight_resize(source, (256, 128))
    assert (
        ImageChops.difference(result.convert("RGBA"), reference.convert("RGBA")).getbbox() is None
    )


def test_palette_png_becomes_truecolour(tmp_path: Path) -> None:
    opaque = gradient(256, 256, "RGB").quantize(32)
    src = save(opaque, tmp_path / "p.png", "PNG")
    config = {
        "container": "png",
        "op": "resample",
        "width": 128,
        "height": 128,
        "filter": "box",
        "alpha": "straight",
    }
    outcome = TextureTransformer().transform(src, tmp_path / "out.png", config)
    assert Image.open(tmp_path / "out.png").mode == "RGB"
    assert outcome.notes == ["palette image written as truecolour"]
    transparent = gradient(256, 256).quantize(32)
    assert "transparency" in transparent.info or transparent.mode == "P"
    src = save(transparent, tmp_path / "pt.png", "PNG", transparency=0)
    TextureTransformer().transform(src, tmp_path / "out2.png", config)
    assert Image.open(tmp_path / "out2.png").mode == "RGBA"


def test_weighted_alpha_avoids_dark_fringes(tmp_path: Path) -> None:
    sprite = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(sprite).rectangle((20, 20, 43, 43), fill=(255, 255, 255, 255))
    src = save(sprite, tmp_path / "sprite.png", "PNG")
    base = {"container": "png", "op": "resample", "width": 32, "height": 32, "filter": "bilinear"}
    straight = transform(src, tmp_path / "s.png", {**base, "alpha": "straight"})
    weighted = transform(src, tmp_path / "w.png", {**base, "alpha": "weighted"})
    edge_straight = [pixel for pixel in pixels(straight) if 0 < pixel[3] < 255]
    edge_weighted = [pixel for pixel in pixels(weighted) if 0 < pixel[3] < 255]
    assert edge_straight and edge_weighted
    # Straight filtering mixes the black of transparent pixels into the edge colour.
    assert min(pixel[0] for pixel in edge_straight) < 250
    assert min(pixel[0] for pixel in edge_weighted) >= 250


@pytest.mark.parametrize(("rle", "image_type"), [(False, 2), (True, 10)])
@pytest.mark.parametrize("orientation", [-1, 1])
def test_tga_keeps_its_encoding_and_row_order(
    tmp_path: Path, rle: bool, image_type: int, orientation: int
) -> None:
    options: dict[str, object] = {"orientation": orientation}
    if rle:
        options["compression"] = "tga_rle"
    src = save(gradient(400, 200), tmp_path / "in.tga", "TGA", **options)
    config = {
        "container": "tga",
        "op": "resample",
        "width": 200,
        "height": 100,
        "filter": "lanczos",
        "alpha": "straight",
    }
    TextureTransformer().transform(src, tmp_path / "out.tga", config)
    raw = (tmp_path / "out.tga").read_bytes()
    assert raw[2] == image_type
    assert bool(raw[17] & 0x20) is (orientation == 1)
    assert Image.open(tmp_path / "out.tga").size == (200, 100)


def test_dds_mip_drop_is_lossless_for_any_format(tmp_path: Path) -> None:
    for name, cubemap in (("BC7_UNORM", False), ("BC1_UNORM", True)):
        data = dds_bytes(256, 256, name, 9, cubemap=cubemap)
        src = tmp_path / f"{name}.dds"
        src.write_bytes(data)
        TextureTransformer().transform(
            src, tmp_path / "out.dds", {"container": "dds", "op": "mip_drop", "levels": 2}
        )
        output = (tmp_path / "out.dds").read_bytes()
        assert output == dds.drop_mips(data, 2)
        header = dds.parse_header(output)
        assert (header.width, header.height, header.mip_count) == (64, 64, 7)


@pytest.mark.parametrize("mips", [1, 9])
def test_dds_resample_uncompressed(tmp_path: Path, mips: int) -> None:
    fmt = dds.format_by_name("R8G8B8A8_UNORM")
    source = gradient(256, 256)
    chain = [
        dds.encode_surface(source.resize((256 >> level, 256 >> level)), fmt)
        for level in range(mips)
    ]
    src = tmp_path / "in.dds"
    src.write_bytes(dds.build_dds(256, 256, fmt, [chain]))
    expected_mips = 1 if mips == 1 else 8  # 128 -> levels 128 .. 1
    config = {
        "container": "dds",
        "op": "resample",
        "width": 128,
        "height": 128,
        "mips": expected_mips,
        "filter": "lanczos",
        "alpha": "straight",
    }
    TextureTransformer().transform(src, tmp_path / "out.dds", config)
    output = (tmp_path / "out.dds").read_bytes()
    header = dds.parse_header(output)
    assert (header.width, header.height, header.mip_count) == (128, 128, expected_mips)
    assert header.trailing_bytes(len(output)) == 0
    top = dds.decode_surface(
        fmt,
        output[header.level_offset(0, 0) : header.level_offset(0, 0) + header.level_size(0)],
        128,
        128,
    )
    reference = straight_resize(source, (128, 128))
    assert ImageChops.difference(top, reference).getbbox() is None


@pytest.mark.parametrize("name", ["BC1_UNORM", "BC3_UNORM", "BC5_UNORM"])
def test_dds_resample_block_compressed(tmp_path: Path, name: str) -> None:
    fmt = dds.format_by_name(name)
    # BC1 keeps only 1-bit alpha and BC5 only two channels: give each what it can hold.
    source = gradient(512, 512, "RGBA" if name == "BC3_UNORM" else "RGB")
    if name == "BC1_UNORM":
        source = source.convert("RGBA")
    chain = [
        dds.encode_surface(source.resize((512 >> level, 512 >> level)), fmt) for level in range(4)
    ]
    src = tmp_path / "in.dds"
    src.write_bytes(dds.build_dds(512, 512, fmt, [chain]))
    config = {
        "container": "dds",
        "op": "resample",
        "width": 256,
        "height": 256,
        "mips": 4,
        "filter": "lanczos",
        "alpha": "straight",
    }
    outcome = TextureTransformer().transform(src, tmp_path / "out.dds", config)
    output = (tmp_path / "out.dds").read_bytes()
    header = dds.parse_header(output)
    assert (header.width, header.mip_count, header.format.name) == (256, 4, name)
    assert header.trailing_bytes(len(output)) == 0
    offset = header.level_offset(0, 0)
    top = dds.decode_surface(fmt, output[offset : offset + header.level_size(0)], 256, 256)
    reference = straight_resize(source, (256, 256))
    if name == "BC5_UNORM":
        top, reference = (
            Image.merge("RGB", (*top.split()[:2], Image.new("L", top.size))),
            Image.merge("RGB", (*reference.split()[:2], Image.new("L", top.size))),
        )
    assert psnr(top, reference) > 28
    assert outcome.notes and "basic quality" in outcome.notes[0]


def test_transform_never_touches_the_source_and_is_deterministic(tmp_path: Path) -> None:
    src = save(gradient(300, 300), tmp_path / "in.png", "PNG")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    config = {
        "container": "png",
        "op": "resample",
        "width": 150,
        "height": 150,
        "filter": "bicubic",
        "alpha": "weighted",
    }
    TextureTransformer().transform(src, tmp_path / "a.png", config)
    TextureTransformer().transform(src, tmp_path / "b.png", config)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()


# ------------------------------------------------------------------------------ plan


def romfs_game(tmp_path: Path, files: dict[str, bytes]) -> GameInput:
    root = tmp_path / "game"
    for relative, data in files.items():
        path = root / "RomFS" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return open_input(root, title_id=APP_TEXT)


def encoded(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def test_plan_follows_the_rules(tmp_path: Path) -> None:
    files = {
        "ui/title.png": encoded(gradient(2560, 1440), "PNG"),
        "ui/icon.png": encoded(gradient(256, 256), "PNG"),
        "fx/grade_lut.png": encoded(gradient(2048, 32), "PNG"),
        "tex/rock.dds": dds_bytes(2048, 2048, "BC7_UNORM", 12),
        "tex/sky.dds": dds_bytes(2048, 2048, "BC7_UNORM", 1),
        "tex/broken.dds": dds_bytes(1024, 1024, "BC1_UNORM", 1)[:200],
        "tex/actually.dds": encoded(gradient(2048, 2048), "PNG"),
        "tex/photo.png": encoded(gradient(2048, 2048, "RGB"), "JPEG"),
        "tex/garbage.tga": b"not an image at all",
    }
    game = romfs_game(tmp_path, files)
    profile = Profile(
        title_id=APP_TEXT,
        textures=TextureSettings(
            rules=(
                TextureRule(match=("**/*_lut.*",), skip=True),
                TextureRule(match=("tex/rock.*",), max_width=1024, max_height=1024),
            )
        ),
    )
    items = list(
        TextureTransformer().plan(
            PlanContext(game=game, profile=profile, tools=Toolbox(environ={}))
        )
    )
    by_source = {item.source: item for item in items}
    assert "romfs/ui/icon.png" not in by_source and "romfs/fx/grade_lut.png" not in by_source

    title = by_source["romfs/ui/title.png"]
    assert isinstance(title, Action) and title.output == "romfs/ui/title.png"
    assert (title.params["width"], title.params["height"]) == (1280, 720)
    rock = by_source["romfs/tex/rock.dds"]
    assert isinstance(rock, Action) and rock.params == {
        "container": "dds",
        "op": "mip_drop",
        "levels": 1,
    }
    actually = by_source["romfs/tex/actually.dds"]
    assert isinstance(actually, Action) and actually.params["container"] == "png"  # signature wins

    def skipped(source: str) -> SkipKind:
        item = by_source[f"romfs/{source}"]
        assert isinstance(item, Skip)
        return item.kind

    assert skipped("tex/sky.dds") is SkipKind.UNSUPPORTED
    assert skipped("tex/broken.dds") is SkipKind.INVALID
    assert skipped("tex/photo.png") is SkipKind.UNSUPPORTED
    assert skipped("tex/garbage.tga") is SkipKind.INVALID

    disabled = Profile(title_id=APP_TEXT, textures=TextureSettings(enabled=False))
    assert (
        list(
            TextureTransformer().plan(
                PlanContext(game=game, profile=disabled, tools=Toolbox(environ={}))
            )
        )
        == []
    )


def test_through_the_pipeline(tmp_path: Path) -> None:
    files = {
        "ui/title.png": encoded(gradient(2560, 1440), "PNG"),
        "tex/rock.dds": dds_bytes(2048, 2048, "BC1_UNORM", 12),
    }
    game = romfs_game(tmp_path, files)

    def run(target: Target) -> list[str]:
        result = build(
            game,
            Profile(title_id=APP_TEXT, target=target),
            transformers=[TextureTransformer()],
            packager=LayeredFSPackager(),
            tools=Toolbox(environ={}),
            options=BuildOptions(out_dir=tmp_path / "out", work_dir=tmp_path / "work"),
        )
        assert result.ok
        return [outcome.status.value for outcome in result.outcomes]

    assert run(Target(1280, 720)) == ["done", "done"]
    romfs = tmp_path / "out" / "atmosphere" / "contents" / APP_TEXT / "romfs"
    title = probe(romfs / "ui" / "title.png")
    rock = probe(romfs / "tex" / "rock.dds")
    assert title is not None and (title.width, title.height) == (1280, 720)
    assert rock is not None and (rock.width, rock.height) == (512, 512)
    assert run(Target(1280, 720)) == ["cached", "cached"]
    # A larger box changes both plans: the DDS now drops one level, the PNG is resized less.
    assert run(Target(1920, 1080)) == ["done", "done"]
    rock = probe(romfs / "tex" / "rock.dds")
    assert rock is not None and (rock.width, rock.height) == (1024, 1024)
    stat = ImageStat.Stat(Image.open(romfs / "ui" / "title.png").convert("L"))
    assert (
        Image.open(romfs / "ui" / "title.png").size == (1920, 1080) and stat.count[0] == 1920 * 1080
    )
