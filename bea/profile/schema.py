"""The profile data model: everything BEA knows about one game, as plain data.

A profile is the *only* place where game-specific knowledge lives. It is written by hand
as two YAML files (``profile.yaml`` and the optional ``patches.yaml``) in a directory
named after the title ID; :mod:`bea.profile.loader` turns them into the frozen dataclasses
below, validating strictly. ``docs/PROFILES.md`` is the normative description of the
YAML form.

Nothing here does I/O. The classes carry a little behaviour only where it defines the
meaning of the data (rule resolution, target-size arithmetic).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any

from bea.globmatch import PathMatcher
from bea.hexutil import build_ids_equal

__all__ = [
    "DEFAULT_TARGET_HEIGHT",
    "DEFAULT_TARGET_WIDTH",
    "DEFAULT_WINDOW_LENGTH",
    "MAX_HUNK_BYTES",
    "MAX_PATCH_BYTES_PER_BUILD",
    "MAX_WINDOW_LENGTH",
    "MIN_WINDOW_LENGTH",
    "PATCH_CATEGORIES",
    "SCHEMA_VERSION",
    "AlphaMode",
    "BinarySettings",
    "BuildPatches",
    "ConfigEdit",
    "ConfigFormat",
    "ConfigRule",
    "ExefsMode",
    "Hunk",
    "IfMissing",
    "IniEdit",
    "JsonEdit",
    "MediaParams",
    "MediaRule",
    "MediaSettings",
    "Patch",
    "PatchSet",
    "PowerOfTwo",
    "Profile",
    "ResampleFilter",
    "Strategy",
    "Target",
    "TextEdit",
    "TextureDefaults",
    "TextureParams",
    "TextureRule",
    "TextureSettings",
    "generic_profile",
    "halvings_needed",
    "is_power_of_two",
    "scale_factor",
    "target_size",
]

#: Version of the YAML schema understood by this module.
SCHEMA_VERSION = 1

DEFAULT_TARGET_WIDTH = 1280
DEFAULT_TARGET_HEIGHT = 720

#: Largest ``expected_bytes`` / ``patch_bytes`` string accepted in one hunk. Profiles
#: describe small, surgical edits; they are not a vehicle for shipping code or data.
MAX_HUNK_BYTES = 256
#: Largest total of ``patch_bytes`` accepted for one build (one executable).
MAX_PATCH_BYTES_PER_BUILD = 16 * 1024

#: Verification windows (``expected_sha256``): long enough that the hash cannot be
#: inverted to recover the bytes it covers, short enough to stay inside one function.
MIN_WINDOW_LENGTH = 16
DEFAULT_WINDOW_LENGTH = 32
MAX_WINDOW_LENGTH = MAX_HUNK_BYTES

#: What a patch may be for. BEA adapts how a game performs on hardware it already
#: targets; anything else (protections, licence or online checks, platform gates,
#: cheats) is out of scope and has no category.
PATCH_CATEGORIES = frozenset(
    {
        "framerate",
        "resolution",
        "dynamic_resolution",
        "render_scale",
        "lod",
        "shadow_quality",
        "draw_distance",
        "texture_budget",
        "memory_budget",
        "post_fx",
        "perf_fix",
    }
)


class Strategy(StrEnum):
    """How a texture is made smaller."""

    #: Drop mip levels when the file has enough of them, otherwise resample.
    AUTO = "auto"
    #: Only ever drop top mip levels (lossless, keeps the pixel format).
    MIP_DROP = "mip_drop"
    #: Always decode, resize and re-encode.
    RESAMPLE = "resample"


class ResampleFilter(StrEnum):
    """Resampling kernel used when a texture is resized."""

    NEAREST = "nearest"
    BOX = "box"
    BILINEAR = "bilinear"
    HAMMING = "hamming"
    BICUBIC = "bicubic"
    LANCZOS = "lanczos"


class PowerOfTwo(StrEnum):
    """What to do with textures whose width and height are both powers of two."""

    #: Keep them powers of two by halving until they fit.
    PRESERVE = "preserve"
    #: Treat them like any other texture (proportional fit, any size).
    IGNORE = "ignore"


class AlphaMode(StrEnum):
    """How the alpha channel takes part in resampling."""

    #: Filter every channel independently (never destroys data packed in RGB or A).
    STRAIGHT = "straight"
    #: Weight colour by alpha while filtering (avoids halos on cut-out sprites).
    WEIGHTED = "weighted"


class ConfigFormat(StrEnum):
    """Syntax of a configuration file edited by a :class:`ConfigRule`."""

    INI = "ini"
    JSON = "json"
    TEXT = "text"


class IfMissing(StrEnum):
    """What to do when the thing an edit targets does not exist in the file."""

    ERROR = "error"
    ADD = "add"
    IGNORE = "ignore"


class ExefsMode(StrEnum):
    """How executable patches are delivered."""

    #: One ``<build id>.ips`` per patched module (small, shareable, the default).
    IPS = "ips"
    #: A fully patched copy of the module (contains the whole executable: never share it).
    REPLACE = "replace"


# --------------------------------------------------------------------------- targets


@dataclass(frozen=True, slots=True)
class Target:
    """The default limit box: nothing BEA produces should be larger than this."""

    max_width: int = DEFAULT_TARGET_WIDTH
    max_height: int = DEFAULT_TARGET_HEIGHT


def is_power_of_two(value: int) -> bool:
    """Tell whether ``value`` is a positive power of two."""
    return value > 0 and value & (value - 1) == 0


def scale_factor(
    width: int, height: int, max_width: int, max_height: int, scale: float | None = None
) -> float:
    """Return the uniform factor (``<= 1``) that brings ``width x height`` within the limits.

    The limits are the box ``max_width x max_height`` and, when given, the uniform
    ``scale``. A result of ``1.0`` means the image already complies.
    """
    factor = min(1.0, max_width / width, max_height / height)
    if scale is not None:
        factor = min(factor, scale)
    return factor


def _exact(value: float | Fraction) -> Fraction:
    """Return ``value`` as the rational number it stands for.

    Profiles spell scales as decimals (``0.57``) and factors are ratios of sizes
    (``64 / 257``); the binary float nearest to such a number is a hair off, and
    ``100 * 0.57`` would land just under 57. Two distinct fractions whose denominators are
    at most 2**24 lie much further apart than a float's rounding error, so the closest
    such fraction is the number that was meant.
    """
    if isinstance(value, Fraction):
        return value
    return Fraction(value).limit_denominator(1 << 24)


def _exact_factor(
    width: int, height: int, max_width: int, max_height: int, scale: float | Fraction | None
) -> Fraction:
    factor = min(Fraction(1), Fraction(max_width, width), Fraction(max_height, height))
    if scale is not None:
        factor = min(factor, _exact(scale))
    return factor


def _halvings(width: int, height: int, factor: Fraction) -> int:
    count = 0
    while True:
        level_width = max(1, width >> count)
        level_height = max(1, height >> count)
        # A side that reached one pixel cannot shrink any further: it no longer counts.
        width_done = level_width == 1 or level_width <= width * factor
        height_done = level_height == 1 or level_height <= height * factor
        if width_done and height_done:
            return count
        count += 1


def halvings_needed(width: int, height: int, factor: float | Fraction) -> int:
    """Return how many times the dimensions must be halved to shrink by at least ``factor``.

    Halving follows the mip-chain rule (``max(1, dim >> k)``), so the result is exactly
    the number of top mip levels to drop. The comparison uses exact arithmetic.
    """
    exact = _exact(factor)
    if exact >= 1:
        return 0
    return _halvings(width, height, exact)


def target_size(
    width: int,
    height: int,
    max_width: int,
    max_height: int,
    *,
    scale: float | None = None,
    power_of_two: PowerOfTwo = PowerOfTwo.PRESERVE,
) -> tuple[int, int]:
    """Return the size a ``width x height`` texture is resampled to.

    * Never larger than the source and never larger than the limits. The scale is a limit
      too: with ``scale: 0.5`` a 1001-pixel side may become at most 500 pixels, which is
      also what dropping one mip level gives.
    * Aspect ratio is kept: each side is the nearest pixel to ``side * factor``, unless
      that would exceed the side's own limit.
    * With :attr:`PowerOfTwo.PRESERVE`, a source whose two dimensions are powers of two
      is halved until it complies, so it stays a power of two.

    All comparisons use exact arithmetic (see :func:`_exact`).
    """
    factor = _exact_factor(width, height, max_width, max_height, scale)
    if factor >= 1:
        return width, height
    if power_of_two is PowerOfTwo.PRESERVE and is_power_of_two(width) and is_power_of_two(height):
        count = _halvings(width, height, factor)
        return max(1, width >> count), max(1, height >> count)
    exact_scale = None if scale is None else _exact(scale)

    def side(source: int, box: int) -> int:
        limit = min(Fraction(source), Fraction(box))
        if exact_scale is not None:
            limit = min(limit, source * exact_scale)
        nearest = math.floor(source * factor + Fraction(1, 2))
        return max(1, min(nearest, math.floor(limit)))

    return side(width, max_width), side(height, max_height)


# -------------------------------------------------------------------------- textures


@dataclass(frozen=True, slots=True)
class TextureParams:
    """Fully resolved texture settings for one file (what a rule lookup returns)."""

    max_width: int
    max_height: int
    scale: float | None
    strategy: Strategy
    filter: ResampleFilter
    power_of_two: PowerOfTwo
    alpha: AlphaMode

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in cache keys and reports)."""
        return {
            "max_width": self.max_width,
            "max_height": self.max_height,
            "scale": self.scale,
            "strategy": self.strategy.value,
            "filter": self.filter.value,
            "power_of_two": self.power_of_two.value,
            "alpha": self.alpha.value,
        }


@dataclass(frozen=True, slots=True)
class TextureDefaults:
    """Values a texture rule inherits when it does not set them."""

    scale: float | None = None
    strategy: Strategy = Strategy.AUTO
    filter: ResampleFilter = ResampleFilter.LANCZOS
    power_of_two: PowerOfTwo = PowerOfTwo.PRESERVE
    alpha: AlphaMode = AlphaMode.STRAIGHT


@dataclass(frozen=True, slots=True)
class TextureRule:
    """One entry of ``textures.rules``. ``None`` means "inherit"."""

    match: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    skip: bool = False
    max_width: int | None = None
    max_height: int | None = None
    scale: float | None = None
    strategy: Strategy | None = None
    filter: ResampleFilter | None = None
    power_of_two: PowerOfTwo | None = None
    alpha: AlphaMode | None = None
    _matcher: PathMatcher = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_matcher", PathMatcher(self.match, self.exclude))

    def matches(self, path: str) -> bool:
        """Tell whether this rule applies to the RomFS-relative ``path``."""
        return self._matcher.matches(path)


@dataclass(frozen=True, slots=True)
class TextureSettings:
    """The ``textures`` section."""

    enabled: bool = True
    defaults: TextureDefaults = field(default_factory=TextureDefaults)
    rules: tuple[TextureRule, ...] = ()

    def resolve(self, path: str, target: Target) -> TextureParams | None:
        """Return the settings for ``path``, or ``None`` when the file must be left alone.

        The first rule that matches wins; a file matched by no rule uses the defaults
        and the profile's target box.
        """
        if not self.enabled:
            return None
        defaults = self.defaults
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if rule.skip:
                return None
            return TextureParams(
                max_width=rule.max_width if rule.max_width is not None else target.max_width,
                max_height=rule.max_height if rule.max_height is not None else target.max_height,
                scale=rule.scale if rule.scale is not None else defaults.scale,
                strategy=rule.strategy or defaults.strategy,
                filter=rule.filter or defaults.filter,
                power_of_two=rule.power_of_two or defaults.power_of_two,
                alpha=rule.alpha or defaults.alpha,
            )
        return TextureParams(
            max_width=target.max_width,
            max_height=target.max_height,
            scale=defaults.scale,
            strategy=defaults.strategy,
            filter=defaults.filter,
            power_of_two=defaults.power_of_two,
            alpha=defaults.alpha,
        )


# ----------------------------------------------------------------------------- media


@dataclass(frozen=True, slots=True)
class MediaParams:
    """Fully resolved media settings for one file."""

    max_width: int
    max_height: int
    max_fps: float | None
    crf: int | None
    video_bitrate: str | None
    audio_bitrate: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in cache keys and reports)."""
        return {
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_fps": self.max_fps,
            "crf": self.crf,
            "video_bitrate": self.video_bitrate,
            "audio_bitrate": self.audio_bitrate,
        }


@dataclass(frozen=True, slots=True)
class MediaRule:
    """One entry of ``media.rules``. ``None`` means "inherit" (box) or "leave as is"."""

    match: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    skip: bool = False
    max_width: int | None = None
    max_height: int | None = None
    max_fps: float | None = None
    crf: int | None = None
    video_bitrate: str | None = None
    audio_bitrate: str | None = None
    _matcher: PathMatcher = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_matcher", PathMatcher(self.match, self.exclude))

    def matches(self, path: str) -> bool:
        """Tell whether this rule applies to the RomFS-relative ``path``."""
        return self._matcher.matches(path)


@dataclass(frozen=True, slots=True)
class MediaSettings:
    """The ``media`` section. Only files matched by a rule are ever transcoded."""

    enabled: bool = True
    rules: tuple[MediaRule, ...] = ()

    def resolve(self, path: str, target: Target) -> MediaParams | None:
        """Return the settings for ``path``, or ``None`` when no rule selects it."""
        if not self.enabled:
            return None
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if rule.skip:
                return None
            return MediaParams(
                max_width=rule.max_width if rule.max_width is not None else target.max_width,
                max_height=rule.max_height if rule.max_height is not None else target.max_height,
                max_fps=rule.max_fps,
                crf=rule.crf,
                video_bitrate=rule.video_bitrate,
                audio_bitrate=rule.audio_bitrate,
            )
        return None


# --------------------------------------------------------------------------- configs


@dataclass(frozen=True, slots=True)
class IniEdit:
    """Set ``key`` to ``value`` inside ``[section]`` (``section=None``: before any header)."""

    section: str | None
    key: str
    value: str
    if_missing: IfMissing = IfMissing.ERROR


@dataclass(frozen=True, slots=True)
class JsonEdit:
    """Set the value at ``path`` (object keys and array indexes) of a JSON document."""

    path: tuple[str | int, ...]
    value: Any
    if_missing: IfMissing = IfMissing.ERROR


@dataclass(frozen=True, slots=True)
class TextEdit:
    """Replace regular-expression matches in a text file.

    ``count`` is the exact number of matches the profile expects; any other number is an
    error, which protects against applying an edit to a file it was not written for.
    """

    find: str
    replace: str
    count: int = 1


type ConfigEdit = IniEdit | JsonEdit | TextEdit


@dataclass(frozen=True, slots=True)
class ConfigRule:
    """One entry of ``configs``: a set of edits applied to the file(s) selected by ``match``."""

    match: tuple[str, ...]
    format: ConfigFormat
    edits: tuple[ConfigEdit, ...]
    exclude: tuple[str, ...] = ()
    #: Text encoding; ``None`` detects a BOM and otherwise assumes UTF-8.
    encoding: str | None = None
    #: When true (the default) it is an error for the rule to select no file.
    required: bool = True
    _matcher: PathMatcher = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_matcher", PathMatcher(self.match, self.exclude))

    def matches(self, path: str) -> bool:
        """Tell whether this rule applies to the RomFS-relative ``path``."""
        return self._matcher.matches(path)


# ---------------------------------------------------------------------------- binary


@dataclass(frozen=True, slots=True)
class Hunk:
    """One in-place byte replacement inside a module's memory image.

    ``offset`` is relative to the start of the module (the address shown by a
    disassembler minus the image base). Before ``patch`` is written, the module must be
    shown to be the one the hunk was written for, in one of two ways:

    * ``expected_sha256``: the SHA-256 (lower-case hex) of the ``window_length`` bytes at
      ``window_offset``, a window that contains the patched range. The profile then holds
      no byte of the game; this is the only form accepted in the repository.
    * ``expected``: the bytes that must be at ``offset`` (same length as ``patch``), for
      private profiles.
    """

    offset: int
    patch: bytes
    expected: bytes | None = None
    expected_sha256: str | None = None
    window_offset: int = 0
    window_length: int = 0

    @property
    def end(self) -> int:
        """Offset just past the patched range."""
        return self.offset + len(self.patch)


@dataclass(frozen=True, slots=True)
class Patch:
    """A named, atomic group of hunks: either all of them apply or none does."""

    name: str
    hunks: tuple[Hunk, ...]
    description: str = ""
    enabled: bool = True
    #: One of :data:`PATCH_CATEGORIES`; required for profiles in the repository.
    category: str | None = None


@dataclass(frozen=True, slots=True)
class BuildPatches:
    """The patches written for one specific build of one module."""

    #: Canonical build ID (uppercase hex, trailing zero bytes removed).
    build_id: str
    patches: tuple[Patch, ...]
    #: ExeFS file the build ID belongs to.
    module: str = "main"
    #: Free-form label, typically the game version this build shipped in.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class PatchSet:
    """The content of ``patches.yaml``."""

    title_id: str
    builds: tuple[BuildPatches, ...] = ()

    def for_module(self, module: str, build_id: str) -> BuildPatches | None:
        """Return the patches for ``module`` at ``build_id``, or ``None``."""
        for build in self.builds:
            if build.module == module and build_ids_equal(build.build_id, build_id):
                return build
        return None


@dataclass(frozen=True, slots=True)
class BinarySettings:
    """The ``binary`` section of ``profile.yaml``."""

    #: Directory name used under ``exefs_patches/``; ``None`` derives one from the title ID.
    patchset_name: str | None = None
    mode: ExefsMode = ExefsMode.IPS


# --------------------------------------------------------------------------- profile


@dataclass(frozen=True, slots=True)
class Profile:
    """Everything declared for one game."""

    #: 16 uppercase hex digits; ``None`` only for the built-in generic profile.
    title_id: str | None
    name: str | None = None
    description: str | None = None
    target: Target = field(default_factory=Target)
    textures: TextureSettings = field(default_factory=TextureSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    configs: tuple[ConfigRule, ...] = ()
    binary: BinarySettings = field(default_factory=BinarySettings)
    patches: PatchSet | None = None
    #: Directory the profile was loaded from, if any.
    source_dir: Path | None = None

    @property
    def is_generic(self) -> bool:
        """Tell whether this is the built-in profile used when a game has none."""
        return self.title_id is None

    def describe(self) -> Mapping[str, Any]:
        """Return a small JSON-serializable summary for reports."""
        return {
            "title_id": self.title_id,
            "name": self.name,
            "generic": self.is_generic,
            "source_dir": str(self.source_dir) if self.source_dir is not None else None,
            "target": {"max_width": self.target.max_width, "max_height": self.target.max_height},
        }


def generic_profile(
    max_width: int = DEFAULT_TARGET_WIDTH, max_height: int = DEFAULT_TARGET_HEIGHT
) -> Profile:
    """Return the built-in profile used for a game that has none.

    It only downsizes textures to the target box: no media transcoding, no config edits
    and no binary patches, because those need knowledge of the specific game.
    """
    return Profile(title_id=None, target=Target(max_width=max_width, max_height=max_height))
