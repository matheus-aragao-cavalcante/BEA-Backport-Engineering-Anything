"""Loading profiles: YAML text to the dataclasses of :mod:`bea.profile.schema`.

``docs/PROFILES.md`` is the normative description of what a profile may contain and this
module implements it. A profile is written by hand, so the real job of a loader is to
catch mistakes and to explain them:

* **Strict.** Unknown keys, wrong types, out-of-range values and duplicate entries are
  problems. A profile that loads is a profile BEA fully understood.
* **Everything in one pass.** Validation never stops at the first finding. Every problem
  of ``profile.yaml`` and ``patches.yaml`` is collected and raised as one
  :class:`~bea.errors.ProfileError`. Each entry of its ``problems`` reads
  ``"<file>: <key path>: <what is wrong>"``, with key paths such as
  ``textures.rules[2].max_width`` or ``builds[0].patches[1].hunks[0].expected_bytes``.
* **No guessing.** YAML turns unquoted text into numbers, booleans and dates. Where a
  string is required and something else arrived, the message says what YAML made of the
  value and asks for quotes; nothing is converted back.
* **Plain data only.** Documents are read with a ``SafeLoader`` subclass (it never builds
  arbitrary objects) that also refuses duplicate mapping keys, which PyYAML would
  silently collapse into the last one.

Nothing here names a game or a location: the directory to load is always a parameter.
"""

from __future__ import annotations

import codecs
import dataclasses
import datetime
import difflib
import math
import os
import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeGuard

import yaml
from yaml.constructor import ConstructorError
from yaml.reader import ReaderError

from bea.errors import ProfileError
from bea.fsutil import fs_path
from bea.globmatch import PathMatcher
from bea.hexutil import (
    TITLE_ID_DIGITS,
    build_ids_equal,
    is_title_id,
    normalize_build_id,
    parse_hex_bytes,
    parse_int,
)
from bea.profile.schema import (
    DEFAULT_TARGET_HEIGHT,
    DEFAULT_TARGET_WIDTH,
    DEFAULT_WINDOW_LENGTH,
    MAX_HUNK_BYTES,
    MAX_PATCH_BYTES_PER_BUILD,
    MAX_WINDOW_LENGTH,
    MIN_WINDOW_LENGTH,
    PATCH_CATEGORIES,
    SCHEMA_VERSION,
    AlphaMode,
    BinarySettings,
    BuildPatches,
    ConfigEdit,
    ConfigFormat,
    ConfigRule,
    ExefsMode,
    Hunk,
    IfMissing,
    IniEdit,
    JsonEdit,
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
    TextureRule,
    TextureSettings,
)

__all__ = [
    "MAX_PROFILE_FILE_BYTES",
    "PATCHES_FILENAME",
    "PROFILE_FILENAME",
    "find_profile_dir",
    "list_profile_dirs",
    "load_profile",
    "load_profile_text",
    "validate_profile_dir",
]

PROFILE_FILENAME = "profile.yaml"
PATCHES_FILENAME = "patches.yaml"
#: Largest ``profile.yaml`` / ``patches.yaml`` that is read. Profiles are small declarative
#: data (``docs/PROFILES.md``, "Data only"); anything bigger is refused before it is parsed.
MAX_PROFILE_FILE_BYTES = 256 * 1024

# The keys of every mapping, in the order of the tables of docs/PROFILES.md: that is the
# order in which the "allowed keys" are listed to the user.
_PROFILE_KEYS = (
    "schema",
    "title_id",
    "name",
    "description",
    "target",
    "textures",
    "media",
    "configs",
    "binary",
)
_TARGET_KEYS = ("max_width", "max_height")
_TEXTURES_KEYS = ("enabled", "defaults", "rules")
_TEXTURE_DEFAULTS_KEYS = ("scale", "strategy", "filter", "power_of_two", "alpha")
_TEXTURE_TUNING_KEYS = ("max_width", "max_height", *_TEXTURE_DEFAULTS_KEYS)
_TEXTURE_RULE_KEYS = ("match", "exclude", "skip", *_TEXTURE_TUNING_KEYS)
_MEDIA_KEYS = ("enabled", "rules")
_MEDIA_TUNING_KEYS = (
    "max_width",
    "max_height",
    "max_fps",
    "crf",
    "video_bitrate",
    "audio_bitrate",
)
_MEDIA_RULE_KEYS = ("match", "exclude", "skip", *_MEDIA_TUNING_KEYS)
_CONFIG_KEYS = ("match", "exclude", "format", "encoding", "required", "edits")
_EDIT_KEYS: Mapping[ConfigFormat, tuple[str, ...]] = {
    ConfigFormat.INI: ("section", "key", "value", "if_missing"),
    ConfigFormat.JSON: ("path", "value", "if_missing"),
    ConfigFormat.TEXT: ("find", "replace", "count"),
}
_BINARY_KEYS = ("patchset_name", "mode")
_PATCHES_KEYS = ("schema", "title_id", "builds")
_BUILD_KEYS = ("build_id", "module", "label", "image_base", "patches")
_PATCH_KEYS = ("name", "category", "description", "enabled", "hunks")
_HUNK_KEYS = (
    "offset",
    "address",
    "patch_bytes",
    "expected_sha256",
    "window_offset",
    "window_length",
    "expected_bytes",
)
_WINDOW_KEYS = ("window_offset", "window_length")
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")

_PATCHSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PATCH_NAME = re.compile(r"[A-Za-z0-9._-]+")
_MODULE_NAME = re.compile(r"[a-z0-9]+")
_BITRATE = re.compile(r"([0-9]+(?:\.[0-9]+)?)[kKmM]?")
_DEFAULT_MODULE = "main"
#: A hunk ends at or before this offset: a module image is addressed with 32 bits.
_IMAGE_END = 1 << 32

_MERGE_TAG = "tag:yaml.org,2002:merge"
_BYTE_ORDER_MARK = "\N{BYTE ORDER MARK}"
#: The line breaks YAML counts, so that positions computed here agree with PyYAML's marks.
_YAML_LINE_BREAK = re.compile("\r\n|[\r\n\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")
#: What YAML makes of unquoted text that was meant to be a string.
_UNQUOTED_SCALARS = (int, float, datetime.date)
_MISSING: Final = object()


# ------------------------------------------------------------------------------ YAML


class _UniqueKeyLoader(yaml.SafeLoader):
    """A ``SafeLoader`` (plain data, never arbitrary objects) with two extra refusals.

    * Duplicate mapping keys. PyYAML keeps the last one without a word, which would let a
      profile do something different from what its author reads.
    * Scalars PyYAML cannot convert. A plain ``2024-13-45`` looks like a date and the
      ``ValueError`` of the conversion would escape as a bare exception; here it becomes
      an ordinary YAML error that carries the position of the value.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._checked: set[int] = set()

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except (yaml.YAMLError, RecursionError):
            raise
        except Exception as exc:
            kind = node.tag.rsplit(":", 1)[-1]
            raise ConstructorError(
                None,
                None,
                f"cannot read this value as a YAML {kind} ({exc}); put it in quotes if it is text",
                node.start_mark,
            ) from exc

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # Expanding merge keys ("<<") rewrites node.value in place and can happen more
        # than once, so duplicates are looked for the first time only: while the mapping
        # still holds exactly the keys its author wrote.
        if id(node) not in self._checked:
            self._checked.add(id(node))
            self._refuse_duplicate_keys(node)
        super().flatten_mapping(node)

    def _refuse_duplicate_keys(self, node: yaml.MappingNode) -> None:
        first_line: dict[Hashable, int] = {}
        for key_node, _value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                continue  # an instruction to copy keys, not a key of its own
            key = self.construct_object(key_node, deep=True)
            try:
                seen = key in first_line
            except TypeError:
                continue  # unhashable: PyYAML reports it when it builds the mapping
            if seen:
                shown = repr(_clip(key)) if isinstance(key, str) else _describe(key)
                raise ConstructorError(
                    None,
                    None,
                    f"duplicate key {shown} (first defined on line {first_line[key] + 1}); "
                    "a key may appear only once in a mapping",
                    key_node.start_mark,
                )
            first_line[key] = key_node.start_mark.line


def _position(line: int, column: int) -> str:
    """Format a zero-based YAML position the way editors count (from one)."""
    return f"line {line + 1}, column {column + 1}"


# ------------------------------------------------------------------- describing values


def _is_integer(value: object) -> TypeGuard[int]:
    """Tell whether ``value`` is a YAML integer (Python counts booleans in, YAML does not)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _clip(text: str, limit: int = 60) -> str:
    """Shorten ``text`` so that one bad value cannot flood a message."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _decimal(value: int) -> str | None:
    """Return ``value`` in decimal; ``None`` when Python refuses to print that many digits."""
    try:
        return str(value)
    except ValueError:
        return None


def _hex(value: int) -> str:
    """Format an offset or address the way disassemblers show it (``0x1A2B3C``)."""
    if value.bit_length() > 256:
        return f"a {value.bit_length()}-bit number"
    return f"-0x{-value:X}" if value < 0 else f"0x{value:X}"


def _describe(value: object) -> str:
    """Name a YAML value the way its author thinks of it, for "got ..." messages."""
    if value is None:
        return "null (an empty value)"
    if isinstance(value, bool):
        return f"the boolean {'true' if value else 'false'}"
    if isinstance(value, int):
        if value.bit_length() > 256:
            return f"a {value.bit_length()}-bit integer"
        return f"the integer {value}"
    if isinstance(value, float):
        return f"the number {value!r}"
    if isinstance(value, str):
        return f"the string {_clip(value)!r}"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, datetime.date):
        return f"the date {value.isoformat()}"
    if isinstance(value, bytes):
        return "binary data"
    return f"a YAML {type(value).__name__}"


def _integer_range(minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f"an integer from {minimum} to {maximum}"
    if minimum is not None:
        return f"an integer >= {minimum}"
    return "an integer"


def _suggestion(word: str, candidates: Sequence[str]) -> str:
    """Return ``" (did you mean 'x'?)"`` when ``word`` is close to a candidate, else ``""``."""
    close = difflib.get_close_matches(word.lower(), candidates, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _or[T](value: T | None, default: T) -> T:
    """Return ``value`` unless it is ``None`` (the key is absent or invalid), then ``default``."""
    return default if value is None else value


# --------------------------------------------------------------- collecting problems


class _Report:
    """The findings about one file of a profile, in the order they were made."""

    __slots__ = ("items", "label")

    def __init__(self, label: str) -> None:
        self.label = label
        self.items: list[str] = []

    def add(self, path: str, message: str) -> None:
        """Record that the value at ``path`` is wrong (``""``: the file as a whole)."""
        where = f"{self.label}: {path}" if path else self.label
        self.items.append(f"{where}: {message}")


def _key(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _item(path: str, index: int) -> str:
    return f"{path}[{index}]"


class _Fields:
    """The keys of one YAML mapping, read through typed getters.

    A getter returns the value when the key is present and valid. Otherwise it records
    what is wrong (if anything is) and returns ``None`` or the given default, so that
    validation carries on and every problem of a document is found in one pass. Objects
    built from such placeholders are never handed out: one recorded problem is enough to
    make the load fail.
    """

    __slots__ = ("_data", "path", "report")

    def __init__(self, report: _Report, path: str, data: dict[str, object]) -> None:
        self.report = report
        self.path = path
        self._data = data

    def at(self, key: str) -> str:
        """Return the key path of ``key``."""
        return _key(self.path, key)

    def has(self, key: str) -> bool:
        """Tell whether ``key`` is written in the mapping (whatever its value)."""
        return key in self._data

    def problem(self, key: str, message: str) -> None:
        """Record that the value of ``key`` is wrong."""
        self.report.add(self.at(key), message)

    def raw(self, key: str, *, required: bool = False) -> object:
        """Return the value of ``key`` as YAML delivered it, or ``_MISSING``."""
        value = self._data.get(key, _MISSING)
        if value is _MISSING and required:
            self.problem(key, "required key is missing")
        return value

    def boolean(self, key: str, *, default: bool) -> bool:
        """Read ``true`` / ``false``. Numbers and quoted words are not booleans."""
        value = self.raw(key)
        if value is _MISSING:
            return default
        if isinstance(value, bool):
            return value
        self.problem(key, f"must be true or false, got {_describe(value)}")
        return default

    def integer(
        self,
        key: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        required: bool = False,
    ) -> int | None:
        """Read an integer in ``[minimum, maximum]``. Booleans and floats are not integers."""
        value = self.raw(key, required=required)
        if value is _MISSING:
            return None
        if (
            _is_integer(value)
            and (minimum is None or value >= minimum)
            and (maximum is None or value <= maximum)
        ):
            return value
        self.problem(key, f"must be {_integer_range(minimum, maximum)}, got {_describe(value)}")
        return None

    def number(self, key: str, *, at_most: float | None = None) -> float | None:
        """Read a finite number greater than zero (an integer is a number), optionally capped."""
        value = self.raw(key)
        if value is _MISSING:
            return None
        number = None
        if _is_integer(value) or isinstance(value, float):
            try:
                number = float(value)
            except OverflowError:
                number = math.inf
        if (
            number is None
            or not math.isfinite(number)
            or number <= 0
            or (at_most is not None and number > at_most)
        ):
            limit = "" if at_most is None else f" and at most {at_most:g}"
            self.problem(key, f"must be a number greater than 0{limit}, got {_describe(value)}")
            return None
        return number

    def string(self, key: str, *, required: bool = False) -> str | None:
        """Read a string. Whatever YAML made of unquoted text is reported, never converted."""
        value = self.raw(key, required=required)
        if value is _MISSING:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, _UNQUOTED_SCALARS):
            self.problem(
                key,
                f"must be a string, but YAML read the unquoted value as {_describe(value)}: "
                "put it in quotes",
            )
        else:
            self.problem(key, f"must be a string, got {_describe(value)}")
        return None

    def choice[E: StrEnum](self, key: str, kind: type[E], *, required: bool = False) -> E | None:
        """Read one of the values of the enumeration ``kind``."""
        value = self.raw(key, required=required)
        if value is _MISSING:
            return None
        options = [member.value for member in kind]
        if isinstance(value, str) and value in options:
            return kind(value)
        hint = _suggestion(value, options) if isinstance(value, str) else ""
        self.problem(key, f"must be one of {', '.join(options)}{hint}, got {_describe(value)}")
        return None

    def location(self, key: str) -> int | None:
        """Read an offset or address: a YAML integer, or text for :func:`bea.hexutil.parse_int`."""
        value = self.raw(key)
        if value is _MISSING:
            return None
        if _is_integer(value):
            return value
        if isinstance(value, str):
            try:
                return parse_int(value)
            except ValueError:
                pass
        self.problem(key, f"must be an integer (decimal or 0x hexadecimal), got {_describe(value)}")
        return None

    def globs(self, key: str, *, required: bool = False) -> tuple[str, ...]:
        """Read "one glob or a list of globs", keeping the patterns that compile."""
        value = self.raw(key, required=required)
        if value is _MISSING:
            return ()
        single = isinstance(value, str)
        entries = [value] if single else value
        if not isinstance(entries, list):
            self.problem(key, f"must be a glob or a list of globs, got {_describe(value)}")
            return ()
        if required and not entries:
            self.problem(key, "must list at least one glob")
        patterns = []
        for index, entry in enumerate(entries):
            where = self.at(key) if single else _item(self.at(key), index)
            if not isinstance(entry, str):
                self.report.add(where, f"a glob is a string, got {_describe(entry)}")
                continue
            try:
                # The same construction the rule dataclasses perform, one pattern at a
                # time so that the finding points at the pattern that is wrong.
                PathMatcher((entry,))
            except ValueError as exc:
                text = str(exc)
                self.report.add(
                    where, text if repr(entry) in text else f"{text} (pattern {entry!r})"
                )
                continue
            patterns.append(entry)
        return tuple(patterns)

    def sequence(
        self, key: str, *, required: bool = False, non_empty: bool = False
    ) -> list[object] | None:
        """Read a list; ``None`` when the key is absent or its value is not a list."""
        value = self.raw(key, required=required)
        if value is _MISSING:
            return None
        if not isinstance(value, list):
            self.problem(key, f"must be a list, got {_describe(value)}")
            return None
        if non_empty and not value:
            self.problem(key, "must not be empty")
        return value

    def mapping(self, key: str, allowed: Sequence[str]) -> _Fields | None:
        """Read a nested mapping; ``None`` when the key is absent or is not a mapping."""
        value = self.raw(key)
        return None if value is _MISSING else _open(self.report, self.at(key), value, allowed)

    def mappings(
        self, key: str, allowed: Sequence[str], *, required: bool = False, non_empty: bool = False
    ) -> Iterator[tuple[int, _Fields]]:
        """Yield ``(index, fields)`` for every item of the list at ``key`` that is a mapping."""
        entries = self.sequence(key, required=required, non_empty=non_empty) or ()
        for index, entry in enumerate(entries):
            fields = _open(self.report, _item(self.at(key), index), entry, allowed)
            if fields is not None:
                yield index, fields


def _open(report: _Report, path: str, value: object, allowed: Sequence[str]) -> _Fields | None:
    """Wrap the mapping found at ``path``, recording every key that is not ``allowed``."""
    listed = ", ".join(allowed)
    if not isinstance(value, dict):
        report.add(path, f"must be a mapping ({listed}), got {_describe(value)}")
        return None
    data: dict[str, object] = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            report.add(path, f"keys are strings, got {_describe(key)}; allowed keys: {listed}")
        elif key not in allowed:
            hint = _suggestion(key, allowed)
            report.add(_key(path, key), f"unknown key{hint}; allowed keys: {listed}")
        else:
            data[key] = entry
    return _Fields(report, path, data)


def _document(text: str, report: _Report, allowed: Sequence[str]) -> _Fields | None:
    """Parse one YAML document and return its top-level mapping.

    Returns ``None``, after recording why, when the text is not YAML, is empty or does
    not hold a mapping. A YAML error ends the validation of that file: there is no
    document to look at.
    """
    listed = ", ".join(allowed)
    text = text.removeprefix(_BYTE_ORDER_MARK)  # not content, and it would shift column 1
    try:
        # _UniqueKeyLoader is a SafeLoader: it builds plain data only.
        document = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        what = ": ".join(part for part in (exc.context, exc.problem) if part) or "invalid YAML"
        report.add("" if mark is None else _position(mark.line, mark.column), f"YAML error: {what}")
        return None
    except ReaderError as exc:
        position = exc.position if isinstance(exc.position, int) else 0
        breaks = list(_YAML_LINE_BREAK.finditer(text, 0, position))
        column = position - (breaks[-1].end() if breaks else 0)
        shown = f"U+{exc.character:04X}" if isinstance(exc.character, int) else "a character"
        report.add(
            _position(len(breaks), column),
            f"YAML error: {shown} is not allowed in a YAML file ({exc.reason})",
        )
        return None
    except yaml.YAMLError as exc:
        report.add("", f"YAML error: {exc}")
        return None
    except RecursionError:
        report.add("", "YAML error: the document is nested too deeply to be a profile")
        return None
    if document is None:
        report.add("", f"the document is empty: expected a mapping with the keys {listed}")
        return None
    if not isinstance(document, dict):
        report.add("", f"the top level must be a mapping ({listed}), got {_describe(document)}")
        return None
    return _open(report, "", document, allowed)


# ----------------------------------------------------------------------- profile.yaml


def _check_schema(fields: _Fields) -> None:
    version = fields.integer("schema", required=True)
    if version is not None and version != SCHEMA_VERSION:
        fields.problem(
            "schema",
            f"must be {SCHEMA_VERSION}, the only schema this version of BEA reads, "
            f"got {_describe(version)}",
        )


def _title_id(fields: _Fields) -> str | None:
    """Read ``title_id``: 16 hex digits written as a string, returned uppercase."""
    text = fields.string("title_id", required=True)
    if text is None:
        return None
    if not is_title_id(text):
        fields.problem(
            "title_id", f"must be exactly {TITLE_ID_DIGITS} hex digits, got {_describe(text)}"
        )
        return None
    return text.upper()


def _profile(fields: _Fields, source_dir: Path | None) -> Profile:
    """Read the top level of ``profile.yaml``."""
    _check_schema(fields)
    return Profile(
        title_id=_title_id(fields),
        name=fields.string("name"),
        description=fields.string("description"),
        target=_target(fields.mapping("target", _TARGET_KEYS)),
        textures=_textures(fields.mapping("textures", _TEXTURES_KEYS)),
        media=_media(fields.mapping("media", _MEDIA_KEYS)),
        configs=tuple(_configs(fields)),
        binary=_binary(fields.mapping("binary", _BINARY_KEYS)),
        source_dir=source_dir,
    )


def _target(fields: _Fields | None) -> Target:
    if fields is None:
        return Target()
    return Target(
        max_width=_or(fields.integer("max_width", minimum=1), DEFAULT_TARGET_WIDTH),
        max_height=_or(fields.integer("max_height", minimum=1), DEFAULT_TARGET_HEIGHT),
    )


def _textures(fields: _Fields | None) -> TextureSettings:
    if fields is None:
        return TextureSettings()
    return TextureSettings(
        enabled=fields.boolean("enabled", default=True),
        defaults=_texture_defaults(fields.mapping("defaults", _TEXTURE_DEFAULTS_KEYS)),
        rules=tuple(
            _texture_rule(entry) for _index, entry in fields.mappings("rules", _TEXTURE_RULE_KEYS)
        ),
    )


def _texture_defaults(fields: _Fields | None) -> TextureDefaults:
    builtin = TextureDefaults()
    if fields is None:
        return builtin
    return TextureDefaults(
        scale=fields.number("scale", at_most=1.0),
        strategy=_or(fields.choice("strategy", Strategy), builtin.strategy),
        filter=_or(fields.choice("filter", ResampleFilter), builtin.filter),
        power_of_two=_or(fields.choice("power_of_two", PowerOfTwo), builtin.power_of_two),
        alpha=_or(fields.choice("alpha", AlphaMode), builtin.alpha),
    )


def _texture_rule(fields: _Fields) -> TextureRule:
    rule = TextureRule(
        match=fields.globs("match", required=True),
        exclude=fields.globs("exclude"),
        skip=fields.boolean("skip", default=False),
        max_width=fields.integer("max_width", minimum=1),
        max_height=fields.integer("max_height", minimum=1),
        scale=fields.number("scale", at_most=1.0),
        strategy=fields.choice("strategy", Strategy),
        filter=fields.choice("filter", ResampleFilter),
        power_of_two=fields.choice("power_of_two", PowerOfTwo),
        alpha=fields.choice("alpha", AlphaMode),
    )
    if rule.skip:
        _refuse_tuning(fields, _TEXTURE_TUNING_KEYS)
    return rule


def _refuse_tuning(fields: _Fields, tuning: Sequence[str]) -> None:
    """Record every tuning key written in a rule that says ``skip: true``."""
    for key in tuning:
        if fields.has(key):
            fields.problem(key, "cannot be combined with skip: true (a skipped file is left alone)")


def _media(fields: _Fields | None) -> MediaSettings:
    if fields is None:
        return MediaSettings()
    return MediaSettings(
        enabled=fields.boolean("enabled", default=True),
        rules=tuple(
            _media_rule(entry) for _index, entry in fields.mappings("rules", _MEDIA_RULE_KEYS)
        ),
    )


def _media_rule(fields: _Fields) -> MediaRule:
    rule = MediaRule(
        match=fields.globs("match", required=True),
        exclude=fields.globs("exclude"),
        skip=fields.boolean("skip", default=False),
        max_width=fields.integer("max_width", minimum=1),
        max_height=fields.integer("max_height", minimum=1),
        max_fps=fields.number("max_fps"),
        crf=fields.integer("crf", minimum=0, maximum=63),
        video_bitrate=_bitrate(fields, "video_bitrate"),
        audio_bitrate=_bitrate(fields, "audio_bitrate"),
    )
    if fields.has("crf") and fields.has("video_bitrate"):
        fields.report.add(
            fields.path,
            "crf and video_bitrate are mutually exclusive: "
            "ask for a constant quality or for a bitrate, not both",
        )
    if rule.skip:
        _refuse_tuning(fields, _MEDIA_TUNING_KEYS)
    return rule


def _bitrate(fields: _Fields, key: str) -> str | None:
    """Read a bitrate: a positive integer (kept as decimal text) or text such as ``"128k"``."""
    value = fields.raw(key)
    if value is _MISSING:
        return None
    text = None
    if _is_integer(value):
        text = _decimal(value) if value > 0 else None
    elif isinstance(value, str):
        match = _BITRATE.fullmatch(value)
        if match is not None and float(match[1]) > 0:
            text = value
    if text is None:
        fields.problem(
            key,
            "must be a bitrate: a positive integer (bits per second) or a string such as "
            f'"2500k" or "4M", got {_describe(value)}',
        )
    return text


def _configs(fields: _Fields) -> Iterator[ConfigRule]:
    for _index, entry in fields.mappings("configs", _CONFIG_KEYS):
        rule = _config_rule(entry)
        if rule is not None:
            yield rule


def _encoding(fields: _Fields) -> str | None:
    """Read ``encoding``: the name of a codec Python can decode text with."""
    name = fields.string("encoding")
    if name is None:
        return None
    try:
        codecs.lookup(name)
        # lookup() also finds codecs that do not produce text (hex, zlib ...); encoding
        # with one is refused, and that is exactly what reading a file would hit later.
        "".encode(name)
    except (LookupError, ValueError) as exc:
        fields.problem(
            "encoding",
            f"is not a text encoding Python knows ({exc}): "
            "use a codec name such as utf-8, utf-16 or cp1252",
        )
        return None
    return name


def _ini_line(
    fields: _Fields, key: str, text: str | None, *, if_empty: str | None = None
) -> str | None:
    """Check text that becomes part of one line of an INI file.

    ``if_empty`` is the complaint to record for blank text; ``None`` when blank is fine.
    """
    if text is None:
        return None
    if text and text.splitlines() != [text]:
        fields.problem(
            key,
            "must be a single line, because INI files are edited line by line "
            "(a YAML block scalar ends with a line break: use a quoted string)",
        )
        return None
    if if_empty is not None and not text.strip():
        fields.problem(key, if_empty)
        return None
    return text


def _ini_value(fields: _Fields) -> str | None:
    """Read the ``value`` of an INI edit: a string as it is, an integer as decimal text."""
    value = fields.raw("value", required=True)
    if value is _MISSING:
        return None
    if isinstance(value, str):
        return _ini_line(fields, "value", value)
    text = _decimal(value) if _is_integer(value) else None
    if text is None:
        # Floats, booleans and dates do not survive YAML unchanged (0.70 becomes 0.7,
        # "on" becomes true), so only the author knows the text the file needs.
        advice = (
            'write the text in quotes ("" for an empty value)'
            if value is None
            else "write the text exactly as the file needs it, in quotes"
        )
        fields.problem("value", f"must be a string or an integer, got {_describe(value)}: {advice}")
    return text


def _ini_edit(fields: _Fields) -> IniEdit | None:
    section = None
    written = fields.raw("section")
    if written is not _MISSING and written is not None:
        section = _ini_line(
            fields,
            "section",
            fields.string("section"),
            if_empty="must not be empty: use null (or leave the key out) for the keys "
            "before the first section header",
        )
    key = _ini_line(
        fields, "key", fields.string("key", required=True), if_empty="must not be empty"
    )
    value = _ini_value(fields)
    if_missing = _or(fields.choice("if_missing", IfMissing), IfMissing.ERROR)
    if key is None or value is None:
        return None
    return IniEdit(section=section, key=key, value=value, if_missing=if_missing)


def _check_json(report: _Report, path: str, value: object, walked: dict[int, bool]) -> None:
    """Record whatever in ``value`` cannot be written to a JSON document.

    ``walked`` maps the ``id`` of every list and mapping met so far to whether it is
    still being walked. YAML anchors can make a value contain itself (it would never
    end) or share one sub-tree many times (it would be walked again and again).
    """
    if value is None or isinstance(value, bool | str) or _is_integer(value):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            report.add(path, f"{_describe(value)} cannot be written in JSON")
        return
    if not isinstance(value, list | dict):
        report.add(path, f"{_describe(value)} is not a JSON value: quote it if it is text")
        return
    open_above = walked.get(id(value))
    if open_above is not None:
        if open_above:
            report.add(path, "contains itself through a YAML anchor: JSON cannot express that")
        return
    walked[id(value)] = True
    if isinstance(value, list):
        for index, entry in enumerate(value):
            _check_json(report, _item(path, index), entry, walked)
    else:
        for key, entry in value.items():
            if isinstance(key, str):
                _check_json(report, _key(path, key), entry, walked)
            else:
                report.add(
                    path, f"JSON object keys are strings, got {_describe(key)}: quote the key"
                )
    walked[id(value)] = False


def _json_path(fields: _Fields) -> tuple[str | int, ...] | None:
    """Read ``path``: strings select object keys, integers select array elements."""
    entries = fields.sequence("path", required=True, non_empty=True)
    if entries is None:
        return None
    steps: list[str | int] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str) or (_is_integer(entry) and entry >= 0):
            steps.append(entry)
        else:
            fields.report.add(
                _item(fields.at("path"), index),
                "must be a string (an object key) or an integer >= 0 (an array index), "
                f"got {_describe(entry)}",
            )
    return tuple(steps)


def _json_edit(fields: _Fields) -> JsonEdit | None:
    path = _json_path(fields)
    value = fields.raw("value", required=True)  # null is a JSON value: only absence is wrong
    if value is not _MISSING:
        _check_json(fields.report, fields.at("value"), value, {})
    if_missing = _or(fields.choice("if_missing", IfMissing), IfMissing.ERROR)
    if path is None or value is _MISSING:
        return None
    return JsonEdit(path=path, value=value, if_missing=if_missing)


def _text_edit(fields: _Fields) -> TextEdit | None:
    find = fields.string("find", required=True)
    replace = fields.string("replace", required=True)
    count = _or(fields.integer("count", minimum=1), 1)
    if find is not None:
        try:
            pattern = re.compile(find, re.MULTILINE)
        except (re.error, OverflowError, RecursionError, Warning) as exc:
            # Not only re.error: a huge repeat count overflows, very deep nesting exhausts
            # the stack, and with warnings turned into errors an ambiguous set raises too.
            fields.problem("find", f"is not a usable regular expression: {exc}")
        else:
            if replace is not None:
                try:
                    # Substituting into nothing is enough to make re parse the template.
                    pattern.sub(replace, "")
                except (re.error, IndexError) as exc:  # IndexError: unknown group name
                    fields.problem(
                        "replace",
                        f"is not a valid replacement for this pattern: {exc} "
                        "(\\g<1> refers to group 1, \\\\ is a backslash)",
                    )
    if find is None or replace is None:
        return None
    return TextEdit(find=find, replace=replace, count=count)


_EDIT_READERS: Mapping[ConfigFormat, Callable[[_Fields], ConfigEdit | None]] = {
    ConfigFormat.INI: _ini_edit,
    ConfigFormat.JSON: _json_edit,
    ConfigFormat.TEXT: _text_edit,
}


def _config_rule(fields: _Fields) -> ConfigRule | None:
    """Read one entry of ``configs``; ``None`` when its format is unusable."""
    match = fields.globs("match", required=True)
    exclude = fields.globs("exclude")
    kind = fields.choice("format", ConfigFormat, required=True)
    encoding = _encoding(fields)
    required = fields.boolean("required", default=True)
    if kind is None:
        # The keys of an edit depend on the format: without one, only the list itself
        # can be checked.
        entries = fields.sequence("edits", required=True, non_empty=True) or ()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                fields.report.add(
                    _item(fields.at("edits"), index), f"must be a mapping, got {_describe(entry)}"
                )
        return None
    read = _EDIT_READERS[kind]
    edits = [
        read(entry)
        for _index, entry in fields.mappings(
            "edits", _EDIT_KEYS[kind], required=True, non_empty=True
        )
    ]
    return ConfigRule(
        match=match,
        format=kind,
        edits=tuple(edit for edit in edits if edit is not None),
        exclude=exclude,
        encoding=encoding,
        required=required,
    )


def _binary(fields: _Fields | None) -> BinarySettings:
    if fields is None:
        return BinarySettings()
    name = fields.string("patchset_name")
    if name is not None and _PATCHSET_NAME.fullmatch(name) is None:
        fields.problem(
            "patchset_name",
            "must be 1 to 64 letters, digits, '.', '_' or '-' and start with a letter or "
            f"digit (it becomes a folder name), got {_describe(name)}",
        )
        name = None
    return BinarySettings(
        patchset_name=name, mode=_or(fields.choice("mode", ExefsMode), ExefsMode.IPS)
    )


# ----------------------------------------------------------------------- patches.yaml


def _byte_string(fields: _Fields, key: str) -> bytes | None:
    """Read ``expected_bytes`` / ``patch_bytes``; ``None`` when the text is not hex pairs."""
    text = fields.string(key, required=True)
    if text is None:
        return None
    try:
        data = parse_hex_bytes(text)
    except ValueError as exc:
        fields.problem(key, f'is not a byte string ({exc}): write hex pairs such as "1F 20 03 D5"')
        return None
    if len(data) > MAX_HUNK_BYTES:
        fields.problem(
            key,
            f"holds {len(data)} bytes; a byte string holds at most {MAX_HUNK_BYTES} "
            "(profiles describe small, surgical edits)",
        )
    return data


def _hunk_start(fields: _Fields, image_base: int | None, size: int) -> int | None:
    """Return the offset of a hunk in the module image, from ``offset`` or ``address``.

    ``image_base`` is ``None`` when the build's own ``image_base`` is unusable: an
    ``address`` can then be type-checked but not placed.
    """
    offset = fields.location("offset")
    address = fields.location("address")
    if fields.has("offset") == fields.has("address"):
        both = fields.has("offset")
        fields.report.add(
            fields.path,
            "offset and address are alternatives: give only one of them"
            if both
            else "one of offset / address is required",
        )
        return None
    if fields.has("offset"):
        key, start, origin = "offset", offset, ""
    elif address is None or image_base is None:
        return None
    else:
        key, start = "address", address - image_base
        origin = f" (address minus image_base {_hex(image_base)})"
    if start is None:
        return None
    if start < 0 or start + size > _IMAGE_END:
        fields.problem(
            key,
            f"places the hunk at offset {_hex(start)}{origin}, outside a module image: "
            f"offsets start at 0 and offset + length may not exceed {_hex(_IMAGE_END)}",
        )
        return None
    return start


def _hunk(fields: _Fields, image_base: int | None) -> tuple[Hunk | None, int]:
    """Read one hunk. Also return the size of its ``patch_bytes`` (0 when unreadable).

    A hunk is verified either by the hash of a window of the module (``expected_sha256``,
    the form that keeps the profile free of game bytes) or by literal ``expected_bytes``;
    exactly one of the two must be given.
    """
    hashed = fields.has("expected_sha256")
    literal = fields.has("expected_bytes")
    if hashed == literal:
        fields.report.add(
            fields.path,
            "expected_sha256 and expected_bytes are alternatives: give only one of them"
            if hashed
            else "one of expected_sha256 / expected_bytes is required "
            "(bea nso hunk prints a ready-made expected_sha256)",
        )
    expected = _byte_string(fields, "expected_bytes") if literal else None
    patch = _byte_string(fields, "patch_bytes")
    size = len(patch) if patch is not None else len(expected) if expected is not None else 0
    start = _hunk_start(fields, image_base, size)
    patch_size = 0 if patch is None else len(patch)
    if literal:
        for key in _WINDOW_KEYS:
            if fields.has(key):
                fields.problem(key, "goes with expected_sha256, not with expected_bytes")
    if expected is not None and patch is not None and len(expected) != len(patch):
        fields.problem(
            "patch_bytes",
            f"holds {len(patch)} bytes but expected_bytes holds {len(expected)}: "
            "a hunk replaces bytes in place, so both have the same length",
        )
        return None, patch_size
    digest, window = _hash_window(fields, start, patch_size) if hashed else (None, None)
    if patch is None or start is None or hashed == literal:
        return None, patch_size
    if hashed:
        if digest is None or window is None:
            return None, patch_size
        return (
            Hunk(
                offset=start,
                patch=patch,
                expected_sha256=digest,
                window_offset=window[0],
                window_length=window[1],
            ),
            patch_size,
        )
    if expected is None:
        return None, patch_size
    return Hunk(offset=start, patch=patch, expected=expected), patch_size


def _hash_window(
    fields: _Fields, start: int | None, patch_size: int
) -> tuple[str | None, tuple[int, int] | None]:
    """Read ``expected_sha256`` and its window; the window must hold the patched bytes."""
    digest = None
    text = fields.string("expected_sha256", required=True)
    if text is not None:
        if _SHA256.fullmatch(text) is None:
            fields.problem(
                "expected_sha256",
                f"must be 64 hexadecimal digits (a SHA-256), got {_describe(text)}",
            )
        else:
            digest = text.lower()
    window_offset = fields.location("window_offset") if fields.has("window_offset") else start
    length = fields.integer("window_length", minimum=MIN_WINDOW_LENGTH, maximum=MAX_WINDOW_LENGTH)
    if not fields.has("window_length"):
        length = max(DEFAULT_WINDOW_LENGTH, patch_size)
    if window_offset is None or length is None or start is None:
        return digest, None
    if window_offset < 0 or window_offset + length > _IMAGE_END:
        fields.problem(
            "window_offset",
            f"places the window at {_hex(window_offset)}, outside a module image",
        )
        return digest, None
    if not window_offset <= start or start + patch_size > window_offset + length:
        fields.problem(
            "window_offset",
            f"the verification window [{_hex(window_offset)}, {_hex(window_offset + length)}) "
            f"must contain the patched bytes [{_hex(start)}, {_hex(start + patch_size)})",
        )
        return digest, None
    return digest, (window_offset, length)


def _refuse_overlaps(fields: _Fields, spans: Sequence[tuple[int, int, int]]) -> None:
    """Record every hunk that shares bytes with another hunk of the same patch.

    ``spans`` holds ``(offset, size, index)`` per hunk. Sorted by offset, a hunk overlaps
    an earlier one exactly when it starts before the furthest end reached so far.
    """
    reach, reached_by = -1, -1
    for start, size, index in sorted(spans):
        if start < reach:
            fields.report.add(
                _item(fields.at("hunks"), index),
                f"overlaps hunks[{reached_by}]: both replace the byte at offset {_hex(start)}; "
                "the hunks of a patch may not overlap",
            )
        if start + size > reach:
            reach, reached_by = start + size, index


def _patch(fields: _Fields, image_base: int | None) -> tuple[Patch | None, int]:
    """Read one patch. Also return the total size of its ``patch_bytes``."""
    name = fields.string("name", required=True)
    if name is not None and _PATCH_NAME.fullmatch(name) is None:
        fields.problem(
            "name",
            f"may only contain letters, digits, '.', '_' and '-', got {_describe(name)}",
        )
        name = None
    description = _or(fields.string("description"), "")
    category = fields.string("category")
    if category is not None and category not in PATCH_CATEGORIES:
        options = sorted(PATCH_CATEGORIES)
        fields.problem(
            "category",
            f"must be one of {', '.join(options)}{_suggestion(category, options)}, "
            f"got {_describe(category)}",
        )
        category = None
    enabled = fields.boolean("enabled", default=True)
    hunks: list[Hunk] = []
    spans: list[tuple[int, int, int]] = []
    total = 0
    for index, entry in fields.mappings("hunks", _HUNK_KEYS, required=True, non_empty=True):
        hunk, size = _hunk(entry, image_base)
        total += size
        if hunk is not None:
            hunks.append(hunk)
            spans.append((hunk.offset, len(hunk.patch), index))
    _refuse_overlaps(fields, spans)
    if name is None:
        return None, total
    patch = Patch(
        name=name, hunks=tuple(hunks), description=description, enabled=enabled, category=category
    )
    return patch, total


def _build(fields: _Fields) -> BuildPatches | None:
    """Read one entry of ``builds``; ``None`` when its module or build ID is unusable."""
    build_id = None
    text = fields.string("build_id", required=True)
    if text is not None:
        try:
            build_id = normalize_build_id(text)
        except ValueError as exc:
            fields.problem("build_id", f"{exc} (copy the ID printed by `bea inspect`)")

    module: str | None = _DEFAULT_MODULE
    if fields.has("module"):
        module = fields.string("module")
        if module is not None and _MODULE_NAME.fullmatch(module) is None:
            fields.problem(
                "module",
                "must be an ExeFS file name made of lowercase letters and digits "
                f"(main, sdk, subsdk0 ...), got {_describe(module)}",
            )
            module = None

    label = fields.string("label")

    image_base: int | None = 0
    if fields.has("image_base"):
        image_base = fields.location("image_base")
        if image_base is not None and image_base < 0:
            fields.problem("image_base", f"must not be negative, got {_hex(image_base)}")
            image_base = None

    patches: list[Patch] = []
    first_use: dict[str, int] = {}
    total = 0
    for index, entry in fields.mappings("patches", _PATCH_KEYS, required=True):
        patch, size = _patch(entry, image_base)
        total += size
        if patch is None:
            continue
        if patch.name in first_use:
            entry.problem(
                "name",
                f"{patch.name!r} is already the name of patches[{first_use[patch.name]}]: "
                "patch names are unique within a build",
            )
        else:
            first_use[patch.name] = index
        patches.append(patch)
    if total > MAX_PATCH_BYTES_PER_BUILD:
        fields.problem(
            "patches",
            f"the patch_bytes add up to {total} bytes; one build holds at most "
            f"{MAX_PATCH_BYTES_PER_BUILD} (profiles describe small, surgical edits)",
        )
    if build_id is None or module is None:
        return None
    return BuildPatches(build_id=build_id, patches=tuple(patches), module=module, label=label)


def _patch_set(fields: _Fields, profile_title_id: str | None, profile_label: str) -> PatchSet:
    """Read the top level of ``patches.yaml``."""
    _check_schema(fields)
    title_id = _title_id(fields)
    if title_id is not None and profile_title_id is not None and title_id != profile_title_id:
        fields.problem(
            "title_id",
            f"is {title_id}, but {profile_label} says {profile_title_id}: "
            "the two files of a profile carry the same title ID",
        )
    found: list[tuple[int, BuildPatches]] = []
    for index, entry in fields.mappings("builds", _BUILD_KEYS, required=True):
        build = _build(entry)
        if build is None:
            continue
        for earlier_index, earlier in found:
            # Mod loaders zero-pad build IDs before comparing them, so IDs that differ
            # only in trailing zeros name the same build.
            if earlier.module == build.module and build_ids_equal(earlier.build_id, build.build_id):
                entry.problem(
                    "build_id",
                    f"duplicates builds[{earlier_index}] (module {build.module!r}, same "
                    "build ID): write one entry per module and build",
                )
                break
        found.append((index, build))
    return PatchSet(title_id=title_id or "", builds=tuple(build for _index, build in found))


# --------------------------------------------------------------------- files and API


def _assemble(
    profile_text: str | None,
    patches_text: str | None,
    profile_report: _Report,
    patches_report: _Report,
    source_dir: Path | None,
) -> Profile | None:
    """Validate both documents; ``None`` when ``profile.yaml`` could not be read at all.

    ``patches.yaml`` is validated even when ``profile.yaml`` is unusable, so that one run
    reports the problems of both files.
    """
    profile = None
    if profile_text is not None:
        fields = _document(profile_text, profile_report, _PROFILE_KEYS)
        if fields is not None:
            profile = _profile(fields, source_dir)
    if patches_text is not None:
        fields = _document(patches_text, patches_report, _PATCHES_KEYS)
        if fields is not None:
            title_id = None if profile is None else profile.title_id
            patches = _patch_set(fields, title_id, profile_report.label)
            if profile is not None:
                profile = dataclasses.replace(profile, patches=patches)
    return profile


def _result(profile: Profile | None, reports: Sequence[_Report], subject: str) -> Profile:
    """Return ``profile``, or raise the error that lists every recorded problem."""
    problems = [problem for report in reports for problem in report.items]
    if profile is None or problems:
        count = len(problems)
        headline = f"{subject} is not valid ({count} problem{'' if count == 1 else 's'}):"
        raise ProfileError(
            "\n".join([headline, *(f"  {problem}" for problem in problems)]), problems=problems
        )
    return profile


def _read_text(path: Path, report: _Report, *, required: bool) -> str | None:
    """Return the text of ``path``.

    Returns ``None`` when the file is absent (a problem only if ``required``) or cannot
    be used: unreadable, too large, not UTF-8.
    """
    try:
        with open(fs_path(path), "rb") as handle:
            data = handle.read(MAX_PROFILE_FILE_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        if required:
            report.add("", f"file not found in {path.parent} (every profile directory has one)")
        return None
    except OSError as exc:
        report.add("", f"cannot be read: {exc.strerror or exc}")
        return None
    if len(data) > MAX_PROFILE_FILE_BYTES:
        report.add(
            "",
            f"is larger than {MAX_PROFILE_FILE_BYTES} bytes: "
            "a profile is a small, declarative description, not a container for data",
        )
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.add(
            "",
            f"is not UTF-8 text (byte 0x{data[exc.start]:02X} at offset {exc.start}): "
            "save the file as UTF-8",
        )
        return None


def load_profile(directory: Path) -> Profile:
    """Load the profile stored in ``directory``.

    Reads ``profile.yaml`` and, when it exists, ``patches.yaml``; both are validated
    completely before anything is reported. When the directory itself is named like a
    title ID (16 hex digits), that name must be the profile's ``title_id`` (case is
    ignored). Any other directory name - a template, a folder of the user's - is not
    checked. ``Profile.source_dir`` is ``directory`` as given.

    Raises:
        ProfileError: a file is missing, unreadable or invalid. ``problems`` holds every
            finding as ``"<file name>: <key path>: <what is wrong>"`` and the message is
            a headline followed by those findings, one per line.
    """
    directory = Path(directory)
    profile_report = _Report(PROFILE_FILENAME)
    patches_report = _Report(PATCHES_FILENAME)
    profile = _assemble(
        _read_text(directory / PROFILE_FILENAME, profile_report, required=True),
        _read_text(directory / PATCHES_FILENAME, patches_report, required=False),
        profile_report,
        patches_report,
        directory,
    )
    # The absolute form, so that "." and trailing separators still yield the real name.
    name = Path(fs_path(directory)).name
    if (
        profile is not None
        and profile.title_id is not None
        and is_title_id(name)
        and name.upper() != profile.title_id
    ):
        profile_report.add(
            "title_id",
            f"is {profile.title_id}, but the directory is named {name}: "
            "a profile lives in a directory named after its title ID",
        )
    return _result(profile, (profile_report, patches_report), f"profile {directory}")


def load_profile_text(
    profile_text: str,
    patches_text: str | None = None,
    *,
    source: str = "<profile>",
    patches_source: str = "<patches>",
) -> Profile:
    """Load a profile from the text of ``profile.yaml`` and, optionally, ``patches.yaml``.

    The validation is the one :func:`load_profile` performs; there is no directory, so
    ``Profile.source_dir`` is ``None`` and no directory name is checked. ``source`` and
    ``patches_source`` are the file labels used in messages.

    Raises:
        ProfileError: a document is invalid. ``problems`` holds every finding as
            ``"<file label>: <key path>: <what is wrong>"``.
    """
    profile_report = _Report(source)
    patches_report = _Report(patches_source)
    profile = _assemble(profile_text, patches_text, profile_report, patches_report, None)
    return _result(profile, (profile_report, patches_report), f"profile {source}")


def _title_directories(profiles_dir: Path) -> list[str]:
    """Return the sorted names of the profile directories found in ``profiles_dir``.

    A profile directory is named like a title ID and holds a ``profile.yaml``. Templates
    and anything else are ignored, and so is a ``profiles_dir`` that does not exist.
    """
    try:
        with os.scandir(fs_path(profiles_dir)) as scan:
            names = sorted(
                entry.name for entry in scan if is_title_id(entry.name) and entry.is_dir()
            )
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [
        name
        for name in names
        if os.path.isfile(fs_path(Path(profiles_dir) / name / PROFILE_FILENAME))
    ]


def find_profile_dir(profiles_dir: Path, title_id: str) -> Path | None:
    """Return the profile directory of ``title_id`` inside ``profiles_dir``, or ``None``.

    That is the sub-directory whose name equals ``title_id`` ignoring case and that holds
    a ``profile.yaml``. ``None`` when there is none or ``profiles_dir`` does not exist.
    """
    wanted = title_id.upper()
    for name in _title_directories(profiles_dir):
        if name.upper() == wanted:
            return Path(profiles_dir) / name
    return None


def list_profile_dirs(profiles_dir: Path) -> list[Path]:
    """Return every profile directory inside ``profiles_dir``, sorted by name.

    A profile directory is a sub-directory named like a title ID (16 hex digits) that
    holds a ``profile.yaml``. A ``profiles_dir`` that does not exist gives ``[]``.
    """
    return [Path(profiles_dir) / name for name in _title_directories(profiles_dir)]


def validate_profile_dir(directory: Path) -> list[str]:
    """Return the problems of the profile in ``directory``; an empty list when it is valid.

    This is :func:`load_profile` for callers that want to show findings instead of
    handling an exception. Anything else that goes wrong while loading becomes a single
    problem string as well, so that validating many profiles never stops half-way.
    """
    try:
        load_profile(directory)
    except ProfileError as exc:
        return list(exc.problems) or [str(exc)]
    except Exception as exc:
        return [f"{directory}: cannot be validated ({type(exc).__name__}: {exc})"]
    return []
