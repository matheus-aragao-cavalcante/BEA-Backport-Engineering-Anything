"""The texture transformer: make loose ``.png``, ``.tga`` and ``.dds`` files smaller.

Each RomFS texture is compared with its limits (the profile's target box, a rule's own
box and scale; see :meth:`bea.profile.schema.TextureSettings.resolve`). A file within
its limits is left alone. A larger one is made smaller in one of two ways:

``mip_drop`` (DDS only)
    The largest mip levels are removed. Nothing is decoded, so it works for every pixel
    format, including the ones BEA cannot encode, and it loses nothing: the kept levels
    are byte-for-byte the original ones.
``resample``
    The image is decoded, resized with the profile's filter and encoded again in the
    same format (for DDS: the same pixel format, with a rebuilt mip chain). Only for the
    pixel formats BEA can encode.

Which one applies follows the profile's ``strategy`` (``auto`` prefers ``mip_drop``).
What cannot be done is reported as ``unsupported`` with the reason, never skipped
silently.

Decisions are taken by :func:`decide`, a pure function of the file's header and the
resolved parameters, so they can be tested without any I/O.
"""

from __future__ import annotations

import io
import math
import os
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image

from bea.errors import BeaError, FormatError
from bea.formats import dds
from bea.formats.imageinfo import ImageInfo, probe
from bea.fsutil import fs_path
from bea.model import ROMFS_PREFIX
from bea.profile.schema import (
    AlphaMode,
    ResampleFilter,
    Strategy,
    TextureParams,
    halvings_needed,
    scale_factor,
    target_size,
)
from bea.transformers.base import (
    Action,
    PlanContext,
    PlanItem,
    Skip,
    SkipKind,
    Transformer,
    TransformResult,
)

__all__ = ["MAX_PIXELS", "TEXTURE_EXTENSIONS", "Decision", "TextureTransformer", "decide"]

TEXTURE_EXTENSIONS = frozenset({".png", ".tga", ".dds"})
#: Largest ``width * height`` BEA decodes (the point where Pillow refuses outright).
MAX_PIXELS = 2 * 89_478_485

_RESAMPLING: Mapping[ResampleFilter, Image.Resampling] = {
    member: Image.Resampling[member.name] for member in ResampleFilter
}
_BLOCK_CODECS = frozenset({"DXT1", "DXT5", "BC5"})


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do with one texture."""

    #: ``"none"`` (within limits), ``"action"`` or ``"skip"``.
    kind: str
    params: dict[str, Any] | None = None
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    skip_kind: SkipKind | None = None
    reason: str | None = None


def _skip(kind: SkipKind, reason: str, details: dict[str, Any]) -> Decision:
    return Decision("skip", details=details, skip_kind=kind, reason=reason)


def _mip_count_for(width: int, height: int, source_mips: int) -> int:
    if source_mips <= 1:
        return 1
    return min(source_mips, math.floor(math.log2(max(width, height))) + 1)


def decide(
    info: ImageInfo, params: TextureParams, *, dds_header: dds.DdsHeader | None = None
) -> Decision:
    """Decide what to do with a texture described by ``info`` (and its DDS header)."""
    width, height = info.width, info.height
    details: dict[str, Any] = {
        "source": {
            "width": width,
            "height": height,
            "format": info.pixel_format,
            "mips": info.mip_count,
        }
    }
    factor = scale_factor(width, height, params.max_width, params.max_height, params.scale)
    if factor >= 1.0:
        return Decision("none", details=details)
    new_width, new_height = target_size(
        width,
        height,
        params.max_width,
        params.max_height,
        scale=params.scale,
        power_of_two=params.power_of_two,
    )

    if info.container in ("png", "tga"):
        if params.strategy is Strategy.MIP_DROP:
            return _skip(
                SkipKind.UNSUPPORTED,
                f"a {info.container.upper()} file has no mip chain to drop; "
                "use strategy auto or resample",
                details,
            )
        if info.container == "png" and info.bit_depth == 16:
            return _skip(
                SkipKind.UNSUPPORTED,
                "16-bit PNG: resampling would reduce it to 8 bits per channel",
                details,
            )
        if width * height > MAX_PIXELS:
            return _skip(SkipKind.UNSUPPORTED, f"{width}x{height} is too large to decode", details)
        details["planned"] = {"width": new_width, "height": new_height, "strategy": "resample"}
        return Decision(
            "action",
            params={
                "container": info.container,
                "op": "resample",
                "width": new_width,
                "height": new_height,
                "filter": params.filter.value,
                "alpha": params.alpha.value,
            },
            summary=f"{width}x{height} -> {new_width}x{new_height} (resample {params.filter.value})",
            details=details,
        )

    if info.container != "dds" or dds_header is None:
        return _skip(SkipKind.UNSUPPORTED, f"{info.container} textures are not processed", details)
    header = dds_header
    fmt = header.format
    if header.is_volume:
        return _skip(SkipKind.UNSUPPORTED, "volume textures are not processed", details)
    if not fmt.size_known:
        return _skip(
            SkipKind.UNSUPPORTED, f"the pixel format {fmt.name} is not understood", details
        )

    levels_needed = halvings_needed(width, height, factor)
    can_resample = fmt.codec is not None and header.layers == 1 and width * height <= MAX_PIXELS

    def mip_drop(levels: int) -> Decision:
        level_width, level_height, _depth = header.level_dimensions(levels)
        planned: dict[str, Any] = {
            "width": level_width,
            "height": level_height,
            "strategy": "mip_drop",
        }
        if levels < levels_needed:
            planned["note"] = f"only {levels} of {levels_needed} levels can be dropped"
        details["planned"] = planned
        return Decision(
            "action",
            params={"container": "dds", "op": "mip_drop", "levels": levels},
            summary=(
                f"{width}x{height} {fmt.name}, {header.mip_count} mips -> "
                f"{level_width}x{level_height} (mip_drop {levels})"
            ),
            details=details,
        )

    def resample() -> Decision:
        target_width, target_height = new_width, new_height
        if fmt.compressed and width % 4 == 0 and height % 4 == 0:
            # Keep whole 4x4 blocks: encoders pad partial blocks, which shifts content.
            target_width = max(4, target_width - target_width % 4)
            target_height = max(4, target_height - target_height % 4)
        mips = _mip_count_for(target_width, target_height, header.mip_count)
        details["planned"] = {
            "width": target_width,
            "height": target_height,
            "strategy": "resample",
            "mips": mips,
        }
        return Decision(
            "action",
            params={
                "container": "dds",
                "op": "resample",
                "width": target_width,
                "height": target_height,
                "mips": mips,
                "filter": params.filter.value,
                "alpha": params.alpha.value,
            },
            summary=(
                f"{width}x{height} {fmt.name} -> {target_width}x{target_height} "
                f"(resample {params.filter.value})"
            ),
            details=details,
        )

    def why_not_resample() -> str:
        if fmt.codec is None:
            return f"no encoder for {fmt.name} in this version"
        if header.layers != 1:
            return "cube maps and texture arrays are not resampled"
        return f"{width}x{height} is too large to decode"

    if params.strategy is Strategy.MIP_DROP:
        if header.mip_count == 1:
            return _skip(SkipKind.UNSUPPORTED, "the file has no mip chain to drop", details)
        return mip_drop(min(levels_needed, header.mip_count - 1))
    if params.strategy is Strategy.RESAMPLE:
        if can_resample:
            return resample()
        return _skip(
            SkipKind.UNSUPPORTED,
            f"{why_not_resample()}; strategy mip_drop works when the file has a mip chain",
            details,
        )
    # AUTO: lossless when possible.
    if header.mip_count - 1 >= levels_needed:
        return mip_drop(levels_needed)
    if can_resample:
        return resample()
    if header.mip_count > 1:
        return mip_drop(header.mip_count - 1)
    return _skip(
        SkipKind.UNSUPPORTED,
        f"{why_not_resample()} and the file has no mip chain to drop",
        details,
    )


class TextureTransformer(Transformer):
    """Downsizes loose textures as the profile's texture rules declare."""

    name: ClassVar[str] = "texture"
    version: ClassVar[int] = 1

    def supports(self, path: str) -> bool:
        return (
            path.startswith(ROMFS_PREFIX)
            and os.path.splitext(path)[1].lower() in TEXTURE_EXTENSIONS
        )

    def plan(self, context: PlanContext) -> Iterable[PlanItem]:
        textures = context.profile.textures
        if not textures.enabled:
            return
        for entry in context.game.romfs_files:
            if entry.ext not in TEXTURE_EXTENSIONS:
                continue
            params = textures.resolve(entry.path, context.profile.target)
            if params is None:
                continue
            item = self._plan_file(context, entry.logical, entry.size, params)
            if item is not None:
                yield item

    def _plan_file(
        self, context: PlanContext, logical: str, size: int, params: TextureParams
    ) -> PlanItem | None:
        path = context.game.resolve(logical)
        try:
            info = probe(path)
            header = None
            if info is not None and info.container == "dds":
                header = dds.read_header(path)
                header.trailing_bytes(size)  # refuses a truncated file
        except (BeaError, OSError) as exc:
            return Skip(self.name, logical, SkipKind.INVALID, str(exc))
        if info is None:
            return Skip(self.name, logical, SkipKind.INVALID, "not a recognized image file")
        if info.container not in ("png", "tga", "dds"):
            return Skip(
                self.name,
                logical,
                SkipKind.UNSUPPORTED,
                f"the file is a {info.container.upper()} image; only PNG, TGA and DDS are processed",
            )
        decision = decide(info, params, dds_header=header)
        if decision.kind == "none":
            return None
        if decision.kind == "skip":
            assert decision.skip_kind is not None and decision.reason is not None
            return Skip(self.name, logical, decision.skip_kind, decision.reason)
        assert decision.params is not None
        return Action(
            transformer=self.name,
            source=logical,
            output=logical,
            params=decision.params,
            summary=decision.summary,
            details=decision.details,
        )

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        container = config["container"]
        if container == "dds":
            data = Path(fs_path(src)).read_bytes()
            if config["op"] == "mip_drop":
                output = dds.drop_mips(data, int(config["levels"]))
                header = dds.parse_header(output)
                Path(dst).write_bytes(output)
                return TransformResult(
                    details={
                        "width": header.width,
                        "height": header.height,
                        "strategy": "mip_drop",
                        "format": header.format.name,
                    }
                )
            return _resample_dds(data, dst, config)
        return _resample_picture(src, dst, config)


# -------------------------------------------------------------------------- resampling


def _open_image(path: Path) -> Image.Image:
    try:
        with warnings.catch_warnings():
            # Sizes were bounded by MAX_PIXELS at plan time.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            image = Image.open(fs_path(path))
            image.load()
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise FormatError(f"{path.name} cannot be decoded: {exc}") from exc
    return image


def _resize(image: Image.Image, width: int, height: int, config: Mapping[str, Any]) -> Image.Image:
    method = _RESAMPLING[ResampleFilter(config["filter"])]
    if image.mode not in ("RGBA", "LA"):
        return image.resize((width, height), method)
    if AlphaMode(config["alpha"]) is AlphaMode.WEIGHTED:
        source_mode = image.mode
        premultiplied = image.convert("RGBA").convert("RGBa")
        resized = premultiplied.resize((width, height), method).convert("RGBA")
        return resized if source_mode == "RGBA" else resized.convert("LA")
    # Straight: every channel on its own. Pillow weights colour by alpha when it resizes
    # an RGBA or LA image, which would destroy data packed under zero alpha (masks,
    # height maps), so the channels are resized separately and put back together.
    channels = [channel.resize((width, height), method) for channel in image.split()]
    return Image.merge(image.mode, channels)


def _flatten_palette(image: Image.Image, notes: list[str]) -> Image.Image:
    if image.mode == "P":
        notes.append("palette image written as truecolour")
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    if image.mode == "PA":
        notes.append("palette image written as truecolour")
        return image.convert("RGBA")
    if image.mode == "1":
        return image.convert("L")
    if image.mode not in ("L", "LA", "RGB", "RGBA"):
        return image.convert("RGBA" if "A" in image.mode else "RGB")
    return image


def _resample_picture(src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
    width, height = int(config["width"]), int(config["height"])
    notes: list[str] = []
    source = _open_image(src)
    image = _flatten_palette(source, notes)
    resized = _resize(image, width, height, config)
    buffer = io.BytesIO()
    if config["container"] == "png":
        options: dict[str, Any] = {}
        if "icc_profile" in source.info:
            options["icc_profile"] = source.info["icc_profile"]
        resized.save(buffer, format="PNG", **options)
    else:
        options = {}
        if source.info.get("compression") == "tga_rle":
            options["compression"] = "tga_rle"
        if "orientation" in source.info:
            options["orientation"] = source.info["orientation"]
        resized.save(buffer, format="TGA", **options)
    Path(dst).write_bytes(buffer.getvalue())
    return TransformResult(
        notes=notes,
        details={"width": width, "height": height, "strategy": "resample", "format": resized.mode},
    )


def _resample_dds(data: bytes, dst: Path, config: Mapping[str, Any]) -> TransformResult:
    header = dds.parse_header(data)
    fmt = header.format
    width, height, mips = int(config["width"]), int(config["height"]), int(config["mips"])
    start = header.level_offset(0, 0)
    payload = data[start : start + header.level_size(0)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        top = dds.decode_surface(fmt, payload, header.width, header.height)
    resized = _resize(top, width, height, config)
    levels = []
    for level in range(mips):
        level_width, level_height = max(1, width >> level), max(1, height >> level)
        # Every level is computed from the level-0 result, not from the previous level.
        image = resized if level == 0 else _resize(resized, level_width, level_height, config)
        levels.append(dds.encode_surface(image, fmt))
    output = dds.resized_header(data, header, width=width, height=height, depth=1, mip_count=mips)
    Path(dst).write_bytes(output + b"".join(levels))
    notes = []
    if fmt.codec in _BLOCK_CODECS:
        notes.append(f"re-encoded with the built-in {fmt.codec} encoder (basic quality)")
    return TransformResult(
        notes=notes,
        details={
            "width": width,
            "height": height,
            "strategy": "resample",
            "format": fmt.name,
            "mips": mips,
        },
    )
