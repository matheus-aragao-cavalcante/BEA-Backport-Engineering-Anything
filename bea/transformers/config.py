"""Configuration transformer: declarative edits to text files of the RomFS.

A ``configs`` entry of a profile selects text files and lists the edits to make in them
(``docs/PROFILES.md`` is the normative description). Three rules shape this module:

* **Only the edit changes.** A file keeps its byte-order mark, its encoding, the ending
  of every line and all the text no edit targets. A file that already holds the
  requested values comes back byte for byte, which is how planning knows that there is
  nothing to do for it.
* **Never guess.** An edit that does not fit the file raises
  :class:`~bea.errors.MismatchError` naming the edit and what was found instead: the
  profile was written for another version of that file, and nothing is produced for it.
* **Edits are plain data.** :func:`apply_config_edits` is a pure function of the file
  content and of JSON-serializable parameters (:func:`edits_to_params`), so the result
  depends on nothing else and can be cached.

Text
    A byte-order mark (UTF-8, UTF-16 or UTF-32) decides the codec and is written back
    unchanged; without one the entry's ``encoding`` is used, else UTF-8. A file that the
    codec cannot reproduce byte for byte is refused. Nothing translates newlines:
    ``\\r\\n``, ``\\n`` and ``\\r`` each end a line and stay as they are, and the lines
    an edit adds use the first line ending found in the file (``\\n`` if it has none).

``ini``
    Edited line by line, never parsed and regenerated. ``[name]`` opens a section (text
    after the bracket is ignored), a line whose first non-blank character is ``;`` or
    ``#`` is a comment, and any other line holding ``=`` defines the key written before
    the first ``=``. Names compare with :meth:`str.casefold`. A section may be split in
    several blocks; a key defined more than once in its section is ambiguous. Setting a
    value replaces what follows ``=`` and the white space padding it. A missing key is
    added after the last non-blank line of the section's last block, a missing section
    at the end of the file; added lines always end with a line ending.

``json``
    Parsed, edited and written back in the style the document had: key order,
    indentation unit, one-line layout with its separators, line endings, the white space
    around the document, raw or escaped non-ASCII characters and the spelling of the
    numbers no edit replaced. A document with a duplicated object key is refused,
    because writing it back would drop one of the two definitions.

``text``
    Regular-expression substitution in multi-line mode. The replacement is used exactly
    as written and the number of matches must be the expected one.
"""

from __future__ import annotations

import codecs
import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from bea.errors import InputError, MismatchError, OutputError
from bea.fsutil import fs_path
from bea.model import ROMFS_PREFIX, FileEntry
from bea.profile.schema import (
    ConfigEdit,
    ConfigFormat,
    ConfigRule,
    IfMissing,
    IniEdit,
    JsonEdit,
    TextEdit,
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

__all__ = ["ConfigTransformer", "apply_config_edits", "edits_to_params"]

# ------------------------------------------------------------------------------ text

#: Byte-order marks with the codec each one announces. UTF-32 LE has to be tried before
#: UTF-16 LE: its mark starts with the same two bytes.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF8, "utf-8"),
)
#: Codecs that write a byte-order mark of their own, with the equivalent that does not.
#: They are only reached for a file that has no mark, where writing one would add bytes
#: the game never had. Little-endian is pinned so that every host reads such a file the
#: same way (the generic codecs follow the byte order of the machine).
_MARKLESS: Mapping[str, str] = {
    "utf-16": "utf-16-le",
    "utf-32": "utf-32-le",
    "utf-8-sig": "utf-8",
}
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
#: How many names an error message lists before it cuts the list short.
_LISTING_LIMIT = 8
#: Longest regular expression quoted in full by an error message.
_PATTERN_LIMIT = 60

_ENCODING_EXAMPLES = "utf-8, utf-16-le, cp1252 or shift_jis"


@dataclass(frozen=True, slots=True)
class _Decoded:
    """The text of a file plus what it takes to write it back the way it was."""

    text: str
    #: Byte-order mark the file starts with (written back unchanged), or ``b""``.
    bom: bytes
    #: Codec of everything after the mark; never one that emits a mark by itself.
    codec: str
    #: How messages call that codec.
    label: str

    def encode(self, text: str) -> bytes:
        """Return ``text`` as file content.

        Raises:
            MismatchError: an edit brought in a character the file's encoding lacks.
        """
        try:
            return self.bom + text.encode(self.codec)
        except UnicodeEncodeError as exc:
            char = exc.object[exc.start]
            raise MismatchError(
                f"the edited text cannot be stored as {self.label}: the character {char!r} "
                f"(U+{ord(char):04X}) cannot be encoded ({exc.reason}); use a value the "
                f"file's encoding can hold"
            ) from None
        except UnicodeError as exc:
            raise MismatchError(
                f"the edited text cannot be stored as {self.label}: {exc}"
            ) from None


def _decode(data: bytes, encoding: str | None) -> _Decoded:
    """Decode a file: a byte-order mark wins, then ``encoding``, then UTF-8.

    Raises:
        MismatchError: the bytes are not text in that encoding, the encoding is unknown,
            or the codec cannot reproduce the file byte for byte.
    """
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return _decode_as(
                data,
                bom,
                codec,
                f"{codec.upper()} (announced by its byte-order mark)",
                "the file is damaged or is not a text file",
            )
    if encoding is None:
        return _decode_as(
            data,
            b"",
            "utf-8",
            "UTF-8",
            "set `encoding` in the configs entry to the encoding the game uses "
            "(for example cp1252 or shift_jis)",
        )
    try:
        codec = codecs.lookup(encoding).name
    except (LookupError, ValueError):
        raise MismatchError(_not_an_encoding(encoding)) from None
    return _decode_as(
        data,
        b"",
        _MARKLESS.get(codec, codec),
        encoding,
        "set `encoding` in the configs entry to the encoding the game really uses",
    )


def _not_an_encoding(encoding: str) -> str:
    return (
        f"{encoding!r} is not a text encoding BEA knows: set `encoding` in the configs "
        f"entry to a Python codec name such as {_ENCODING_EXAMPLES}"
    )


def _decode_as(data: bytes, bom: bytes, codec: str, label: str, advice: str) -> _Decoded:
    body = data[len(bom) :]
    try:
        text = body.decode(codec)
    except UnicodeDecodeError as exc:
        offset = f"offset {len(bom) + exc.start}"
        if exc.start < len(body):
            offset = f"byte 0x{body[exc.start]:02X} at {offset}"
        raise MismatchError(
            f"the file is not valid {label} text ({offset}: {exc.reason}); {advice}"
        ) from None
    except UnicodeError as exc:
        raise MismatchError(f"the file cannot be read as {label} text ({exc}); {advice}") from None
    except LookupError:
        # The name exists but belongs to a bytes-to-bytes transform (hex, zlib ...).
        raise MismatchError(_not_an_encoding(label)) from None
    try:
        faithful = text.encode(codec) == body
    except UnicodeError:
        faithful = False
    if not faithful:
        # Legacy multi-byte encodings can spell one character in several ways and some
        # codecs are not encodings of text at all: writing the file back would change
        # bytes that no edit asked for.
        raise MismatchError(
            f"decoding the file as {label} and encoding it again does not give the same "
            f"bytes, so it cannot be edited without altering text the profile does not "
            f"mention; {advice}"
        )
    return _Decoded(text, bom, codec, label)


def _first_line_ending(text: str) -> str:
    """Return the line ending that text added to ``text`` must use."""
    match = _LINE_BREAK.search(text)
    return match.group() if match else "\n"


def _string(edit: Mapping[str, Any], name: str) -> str:
    """Return the text field ``name`` of an edit.

    Raises:
        TypeError: the field is not a string. Profiles are validated when they are
            loaded, so this is a programming error of the caller, not a user error.
    """
    value = edit[name]
    if not isinstance(value, str):
        raise TypeError(f"config edit field {name!r} must be a string, got {type(value).__name__}")
    return value


def _listing(names: Iterable[str]) -> str:
    """Join names for an error message, without repeats and cut short when there are many."""
    unique = list(dict.fromkeys(names))
    shown = ", ".join(unique[:_LISTING_LIMIT])
    return f"{shown}, ..." if len(unique) > _LISTING_LIMIT else shown


# ------------------------------------------------------------------------------- ini


@dataclass(slots=True)
class _IniLine:
    """One physical line of an INI file, classified once."""

    #: The line without its ending.
    body: str
    #: CR LF, LF or CR as found in the file; empty for a last line that has no ending.
    ending: str
    #: Case-folded name of the section this line opens, when it is a section header.
    header: str | None
    #: Case-folded key this line defines, when it is a key line.
    key: str | None


def _section_name(body: str) -> str | None:
    """Return the name of the section a header line opens (as written), or ``None``."""
    stripped = body.strip()
    if stripped.startswith("["):
        end = stripped.find("]")
        if end != -1:
            return stripped[1:end].strip()
    return None


def _key_name(body: str) -> str | None:
    """Return the key a line defines (as written), or ``None``.

    Only meaningful for a line that is not a section header.
    """
    if body.lstrip()[:1] in (";", "#") or "=" not in body:
        return None
    return body.partition("=")[0].strip()


def _ini_line(body: str, ending: str) -> _IniLine:
    section = _section_name(body)
    if section is not None:
        return _IniLine(body, ending, section.casefold(), None)
    key = _key_name(body)
    return _IniLine(body, ending, None, None if key is None else key.casefold())


def _split_ini(text: str) -> list[_IniLine]:
    """Split ``text`` into lines; joining bodies and endings gives ``text`` back."""
    lines: list[_IniLine] = []
    start = 0
    for match in _LINE_BREAK.finditer(text):
        lines.append(_ini_line(text[start : match.start()], match.group()))
        start = match.end()
    if start < len(text):
        lines.append(_ini_line(text[start:], ""))
    return lines


def _check_ini_edit(label: str, section: str | None, key: str, value: str) -> None:
    """Refuse an edit whose result could not be read back as the same section, key and value.

    Without this an edit could plant a comment, a header or a second line, and applying
    the profile again would no longer find what it wrote.

    Raises:
        MismatchError: the section, key or value cannot be expressed in an INI file.
    """
    if any(_LINE_BREAK.search(part) for part in (section or "", key, value)):
        raise MismatchError(
            f"{label}: a section name, a key and a value must each fit on one line, but "
            f"one of them contains a line break; fix the profile"
        )
    line = f"{key}={value}"
    if _section_name(line) is not None or _key_name(line) != key:
        raise MismatchError(
            f"{label}: the line {line!r} would not be read back as the key {key!r} (a key "
            f"cannot contain '=', start with ';' or '#', or look like a [section] header); "
            f"fix the profile"
        )
    if value != value.lstrip():
        raise MismatchError(
            f"{label}: the value {value!r} starts with white space, which an INI file "
            f"cannot tell apart from the padding after '='; remove it from the profile"
        )
    if section is not None and "]" in section:
        raise MismatchError(
            f"{label}: a section name cannot contain ']' (the header would end there); "
            f"fix the profile"
        )


def _missing_ini_key(
    label: str, lines: Sequence[_IniLine], blocks: Sequence[tuple[int, int]], section: str | None
) -> MismatchError:
    """Build the error for a key that is not there, saying what is there instead."""
    hint = "set if_missing to add or ignore if a version of the game may lack it"
    if not blocks:
        headers = [f"[{_section_name(line.body)}]" for line in lines if line.header is not None]
        found = f"its sections are {_listing(headers)}" if headers else "it has no section"
        return MismatchError(f"{label}: the file has no [{section}] section ({found}); {hint}")
    keys = [
        line.body.partition("=")[0].strip()
        for start, end in blocks
        for line in lines[start:end]
        if line.key is not None
    ]
    found = f"the keys there are {_listing(keys)}" if keys else "no key is defined there"
    place = "before the first section header" if section is None else "in that section"
    return MismatchError(f"{label}: the key is not defined {place} ({found}); {hint}")


def _apply_ini_edit(
    lines: list[_IniLine], edit: Mapping[str, Any], number: int, newline: str
) -> None:
    """Apply one INI edit to ``lines`` in place.

    Raises:
        MismatchError: the key is missing (``if_missing: error``) or ambiguous, or the
            edit cannot be expressed in an INI file.
    """
    section = None if edit.get("section") is None else _string(edit, "section").strip()
    key = _string(edit, "key").strip()
    value = _string(edit, "value")
    policy = IfMissing(edit.get("if_missing", IfMissing.ERROR))
    place = "before the first section" if section is None else f"in [{section}]"
    label = f"edits[{number}] (key {key!r} {place})"
    _check_ini_edit(label, section, key, value)

    wanted_section = None if section is None else section.casefold()
    wanted_key = key.casefold()
    # The half-open line ranges that make up the section. The region before the first
    # header always exists (it may be empty); a named section may come in several blocks.
    blocks: list[tuple[int, int]] = []
    hits: list[int] = []
    start: int | None = 0 if wanted_section is None else None
    for index, line in enumerate(lines):
        if line.header is not None:
            if start is not None:
                blocks.append((start, index))
            start = index if line.header == wanted_section else None
        elif start is not None and line.key == wanted_key:
            hits.append(index)
    if start is not None:
        blocks.append((start, len(lines)))

    if len(hits) > 1:
        numbers = ", ".join(str(index + 1) for index in hits)
        raise MismatchError(
            f"{label}: the key is defined {len(hits)} times there (lines {numbers}); BEA "
            f"cannot tell which definition the game uses"
        )
    if hits:
        line = lines[hits[0]]
        head, _, rest = line.body.partition("=")
        # White space right after "=" is padding, not value: it stays as it was.
        padding = rest[: len(rest) - len(rest.lstrip())]
        line.body = f"{head}={padding}{value}"
        return
    if policy is IfMissing.IGNORE:
        return
    if policy is IfMissing.ERROR:
        raise _missing_ini_key(label, lines, blocks, section)

    if not blocks:
        if lines:
            if not lines[-1].ending:
                lines[-1].ending = newline
            lines.append(_ini_line("", newline))
        lines.append(_ini_line(f"[{section}]", newline))
        lines.append(_ini_line(f"{key}={value}", newline))
        return
    first, end = blocks[-1]
    # After the last non-blank line, so that the blank lines separating the block from
    # the next section stay below the new key. Only the region before the first header
    # can be entirely blank (a named block starts with its header): the key then goes to
    # the very top of the file.
    position = first
    for index in range(first, end):
        if lines[index].body.strip():
            position = index + 1
    if position == len(lines) and lines and not lines[-1].ending:
        lines[-1].ending = newline
    lines.insert(position, _ini_line(f"{key}={value}", newline))


def _edit_ini(text: str, edits: Sequence[Mapping[str, Any]]) -> str:
    lines = _split_ini(text)
    newline = _first_line_ending(text)
    for number, edit in enumerate(edits):
        _apply_ini_edit(lines, edit, number, newline)
    return "".join(line.body + line.ending for line in lines)


# ------------------------------------------------------------------------------ json

#: The only characters JSON treats as white space.
_JSON_SPACE = " \t\r\n"
#: A string, or one of the two separators when it stands outside of a string.
_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[:,]')
#: The leading white space of the first line that is indented.
_INDENTED_LINE = re.compile(r"(?:\r\n|\r|\n)([ \t]+)(?=[^ \t\r\n])")


class _FloatLiteral(float):
    """A float read from a document that remembers how it was spelled.

    Writing ``1.50`` back as ``1.5`` or ``1e3`` as ``1000.0`` would be harmless, but a
    literal with more digits than a double holds, or too large for one, would come back
    as a different number. Keeping the spelling avoids both.
    """

    __slots__ = ("literal",)

    literal: str

    def __new__(cls, literal: str) -> Self:
        self = super().__new__(cls, literal)
        self.literal = literal
        return self


def _parse_int(literal: str) -> int | _FloatLiteral:
    """Convert an integer literal, keeping the one spelling ``int`` would not give back."""
    # "-0" has no fraction, so it reaches this hook, but as an int it would lose its
    # sign and be written back as "0".
    return _FloatLiteral(literal) if literal == "-0" else int(literal)


@dataclass(frozen=True, slots=True)
class _JsonStyle:
    """How a document was written, which is how it is written back."""

    #: Indentation unit of a multi-line document; ``None`` keeps everything on one line.
    indent: str | None
    #: Separators of a one-line document (a multi-line one breaks the line after ",").
    item_separator: str
    key_separator: str
    newline: str
    #: Writes strings and other scalars, escaping non-ASCII only if the file did.
    encoder: json.JSONEncoder


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object from its members, refusing a key that is defined twice.

    Raises:
        MismatchError: a key is repeated. Parsers disagree on which definition counts,
            and writing the document back would silently drop one of them.
    """
    members = dict(pairs)
    if len(members) != len(pairs):
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise MismatchError(
                    f"the file defines the key {json.dumps(key, ensure_ascii=False)} twice in "
                    f"the same JSON object, so BEA cannot tell which value the game reads "
                    f"nor write the file back without dropping one; edit this file with "
                    f"format: text instead"
                )
            seen.add(key)
    return members


def _load_json(text: str) -> Any:
    """Parse a JSON document.

    Raises:
        MismatchError: the text is not JSON, or an object defines a key twice.
    """
    try:
        return json.loads(
            text, parse_float=_FloatLiteral, parse_int=_parse_int, object_pairs_hook=_unique_keys
        )
    except json.JSONDecodeError as exc:
        raise MismatchError(
            f"the file is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from None
    except ValueError as exc:
        # Not a syntax error: a number the interpreter refuses to convert, for example.
        raise MismatchError(f"the file is not JSON that BEA can read ({exc})") from None


def _json_kind(value: Any) -> str:
    """Name the JSON type of a parsed value, with its article."""
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    if value is None:
        return "null"
    return "a number"


def _json_data(value: Any, label: str) -> Any:
    """Return a private copy of an edit's value made of plain JSON types only.

    The copy matters: the value ends up inside the document, where a later edit of the
    same list may modify it, and the caller's parameters must stay untouched.

    Raises:
        MismatchError: the value holds something JSON cannot represent.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        members: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MismatchError(
                    f"{label}: the value has the object key {key!r}, but JSON object keys "
                    f"are strings; quote it in the profile"
                )
            members[key] = _json_data(item, label)
        return members
    if isinstance(value, list | tuple):
        return [_json_data(item, label) for item in value]
    raise MismatchError(
        f"{label}: the value holds {type(value).__name__} data, which JSON cannot "
        f"represent; write it as a string, number, boolean, null, list or mapping"
    )


def _same_json(left: Any, right: Any) -> bool:
    """Tell whether two parsed JSON values are the same value.

    Unlike ``==`` this keeps JSON types apart: ``1``, ``1.0`` and ``true`` are three
    different things to the parser of a game. The order of object keys does not matter.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json(item, right[key]) for key, item in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_same_json, left, right))
    if isinstance(left, float) and isinstance(right, float):
        return left == right or (math.isnan(left) and math.isnan(right))
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is None and right is None


def _path_text(path: Sequence[Any]) -> str:
    """Write a JSON path the way a profile does: ``["video", "modes", 0]``."""
    items = (
        json.dumps(step, ensure_ascii=False) if isinstance(step, str) else repr(step)
        for step in path
    )
    return f"[{', '.join(items)}]"


def _apply_json_edit(document: Any, edit: Mapping[str, Any], number: int) -> bool:
    """Apply one JSON edit to ``document`` in place and tell whether it changed anything.

    Raises:
        MismatchError: the path meets the wrong kind of value, or something is missing
            and ``if_missing`` does not allow (or cannot arrange for) it to be absent.
    """
    path = list(edit["path"])
    label = f"edits[{number}] (path {_path_text(path)})"
    policy = IfMissing(edit.get("if_missing", IfMissing.ERROR))
    if not path:
        raise MismatchError(f"{label}: the path is empty; it must name a key or an index")
    for step in path:
        is_index = isinstance(step, int) and not isinstance(step, bool) and step >= 0
        if not isinstance(step, str) and not is_index:
            raise MismatchError(
                f"{label}: {step!r} is not a path item; use a string for an object key "
                f"and an integer counted from 0 for an array element"
            )
    value = _json_data(edit["value"], label)
    hint = "set if_missing to add or ignore if a version of the game may lack it"

    node = document
    last = len(path) - 1
    for depth, step in enumerate(path):
        where = "the document" if depth == 0 else f"the value at {_path_text(path[:depth])}"
        if isinstance(step, str):
            # A value of the wrong kind is not something missing: no if_missing policy
            # excuses it, the document simply is not shaped the way the profile assumes.
            if not isinstance(node, dict):
                raise MismatchError(
                    f"{label}: {where} is {_json_kind(node)}, but the key "
                    f"{json.dumps(step, ensure_ascii=False)} needs an object there"
                )
            if step not in node:
                if policy is IfMissing.IGNORE:
                    return False
                if policy is IfMissing.ERROR:
                    raise MismatchError(
                        f"{label}: {where} has no key {json.dumps(step, ensure_ascii=False)} "
                        f"(its keys are {_listing(node) or 'none'}); {hint}"
                    )
                for later in path[depth + 1 :]:
                    if not isinstance(later, str):
                        raise MismatchError(
                            f"{label}: {where} has no key "
                            f"{json.dumps(step, ensure_ascii=False)}, and the rest of the path "
                            f"needs an array (index {later}); BEA creates missing objects but "
                            f"never arrays"
                        )
                for name in reversed(path[depth + 1 :]):
                    value = {name: value}
                node[step] = value
                return True
        else:
            if not isinstance(node, list):
                raise MismatchError(
                    f"{label}: {where} is {_json_kind(node)}, but the index {step} needs "
                    f"an array there"
                )
            if step >= len(node):
                if policy is IfMissing.IGNORE:
                    return False
                advice = "BEA never extends an array" if policy is IfMissing.ADD else hint
                raise MismatchError(
                    f"{label}: {where} is an array of {len(node)}, so it has no index "
                    f"{step}; {advice}"
                )
        if depth < last:
            node = node[step]
    final = path[-1]
    if _same_json(node[final], value):
        return False
    node[final] = value
    return True


def _json_style(text: str, body: str) -> _JsonStyle:
    """Work out how the document ``body`` of the file ``text`` was written."""
    encoder = json.JSONEncoder(ensure_ascii=text.isascii())
    newline = _first_line_ending(text)
    if _LINE_BREAK.search(body) is not None:
        indented = _INDENTED_LINE.search(body)
        return _JsonStyle(indented.group(1) if indented else "", ",", ": ", newline, encoder)
    # One line: keep it so, with a space after each separator only where there was one.
    after: dict[str, str] = {}
    for match in _JSON_TOKEN.finditer(body):
        token = match.group()
        if token in (":", ",") and token not in after:
            after[token] = " " if body[match.end() : match.end() + 1] in (" ", "\t") else ""
            if len(after) == 2:
                break
    # A document that shows only one of the two separators gets the same spacing for
    # the other; one that shows neither gets the usual ", " and ": ".
    fallback = next(iter(after.values()), " ")
    return _JsonStyle(
        None, "," + after.get(",", fallback), ":" + after.get(":", fallback), newline, encoder
    )


def _render_json(value: Any, style: _JsonStyle, depth: int = 0) -> str:
    """Write a parsed (and edited) document back as text in ``style``."""
    if isinstance(value, _FloatLiteral):
        return value.literal
    if isinstance(value, dict):
        members = [
            f"{style.encoder.encode(key)}{style.key_separator}"
            f"{_render_json(item, style, depth + 1)}"
            for key, item in value.items()
        ]
        return _enclose("{", members, "}", style, depth)
    if isinstance(value, list):
        items = [_render_json(item, style, depth + 1) for item in value]
        return _enclose("[", items, "]", style, depth)
    return style.encoder.encode(value)


def _enclose(
    opening: str, items: Sequence[str], closing: str, style: _JsonStyle, depth: int
) -> str:
    if not items:
        return opening + closing
    if style.indent is None:
        return f"{opening}{style.item_separator.join(items)}{closing}"
    inner = style.newline + style.indent * (depth + 1)
    outer = style.newline + style.indent * depth
    return f"{opening}{inner}{(',' + inner).join(items)}{outer}{closing}"


def _escape_unencodable(text: str, codec: str) -> str:
    """Write the characters of a JSON document that ``codec`` lacks as ``\\uXXXX`` escapes.

    Outside of strings a JSON document is plain ASCII, so such a character always sits in
    a string, where its escape means the same. This is what lets an edit put any text in
    a file of any encoding, and keeps a lone surrogate that the file spelled as an escape
    from making the whole document unwritable.
    """
    escapes: dict[int, str] = {}
    for char in set(text):
        if not char.isascii():
            try:
                char.encode(codec)
            except UnicodeError:
                escapes[ord(char)] = json.dumps(char)[1:-1]
    return text.translate(escapes) if escapes else text


def _edit_json(text: str, edits: Sequence[Mapping[str, Any]]) -> str:
    try:
        document = _load_json(text)
        changed = False
        for number, edit in enumerate(edits):
            changed = _apply_json_edit(document, edit, number) or changed
        if not changed:
            # Rendering would normalize whatever was written unusually; a document that
            # already holds the requested values must come back untouched.
            return text
        lead = text[: len(text) - len(text.lstrip(_JSON_SPACE))]
        trail = text[len(text.rstrip(_JSON_SPACE)) :]
        body = text[len(lead) : len(text) - len(trail)]
        return lead + _render_json(document, _json_style(text, body)) + trail
    except RecursionError:
        raise MismatchError(
            "the JSON document is nested too deeply for BEA to edit it; use format: text "
            "for this file"
        ) from None


# ------------------------------------------------------------------------------ regex


def _edit_text(text: str, edits: Sequence[Mapping[str, Any]]) -> str:
    for number, edit in enumerate(edits):
        find = _string(edit, "find")
        replace = _string(edit, "replace")
        count = edit.get("count", 1)
        shown = find if len(find) <= _PATTERN_LIMIT else find[: _PATTERN_LIMIT - 3] + "..."
        label = f"edits[{number}] (find {shown!r})"
        try:
            pattern = re.compile(find, re.MULTILINE)
        except re.error as exc:
            raise MismatchError(
                f"{label}: the regular expression is not valid ({exc}); fix the profile"
            ) from None
        try:
            text, found = pattern.subn(replace, text)
        except (re.error, IndexError) as exc:
            # The template is checked before any match is looked for, so this depends on
            # the profile alone: a reference to a group the expression does not have.
            raise MismatchError(
                f"{label}: the replacement {replace!r} is not valid ({exc}); fix the profile"
            ) from None
        if found != count:
            expected = "1 match" if count == 1 else f"{count} matches"
            raise MismatchError(f"{label}: expected {expected}, found {found}")
    return text


# ----------------------------------------------------------------------- public API

_EDITORS: Mapping[str, Callable[[str, Sequence[Mapping[str, Any]]], str]] = {
    ConfigFormat.INI.value: _edit_ini,
    ConfigFormat.JSON.value: _edit_json,
    ConfigFormat.TEXT.value: _edit_text,
}
_EDIT_TYPES: Mapping[ConfigFormat, type] = {
    ConfigFormat.INI: IniEdit,
    ConfigFormat.JSON: JsonEdit,
    ConfigFormat.TEXT: TextEdit,
}


def apply_config_edits(
    data: bytes,
    fmt: str,
    edits: Sequence[Mapping[str, Any]],
    *,
    encoding: str | None = None,
) -> bytes:
    """Return the content of a configuration file after ``edits``.

    This is a pure function: it depends on its arguments only and modifies none of them.
    ``fmt`` and ``edits`` are the ``"format"`` and ``"edits"`` of :func:`edits_to_params`.
    Edits apply in order, each to the result of the previous one. When they change
    nothing, ``data`` itself is returned.

    Args:
        data: The whole file.
        fmt: ``"ini"``, ``"json"`` or ``"text"``.
        edits: One mapping per edit, shaped after ``fmt``.
        encoding: Python codec name for a file without a byte-order mark (default UTF-8).

    Raises:
        MismatchError: an edit cannot be applied to this file. The message names the
            edit (``edits[n]`` plus its key, path or expression) and what was found.
        ValueError: ``fmt`` or an ``if_missing`` is not a known one.
        TypeError: a text field of an edit is not a string. Like ``ValueError`` this is
            a mistake of the caller: a loaded profile never produces such parameters.
    """
    editor = _EDITORS.get(str(fmt))
    if editor is None:
        raise ValueError(f"unknown config format {fmt!r}; expected one of {sorted(_EDITORS)}")
    decoded = _decode(data, encoding)
    edited = editor(decoded.text, edits)
    if edited == decoded.text:
        return data
    if editor is _edit_json:
        edited = _escape_unencodable(edited, decoded.codec)
    return decoded.encode(edited)


def _plain(value: Any) -> Any:
    """Copy a profile value into plain ``dict`` / ``list`` containers."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _edit_to_params(edit: ConfigEdit) -> dict[str, Any]:
    if isinstance(edit, IniEdit):
        return {
            "section": edit.section,
            "key": edit.key,
            "value": edit.value,
            "if_missing": IfMissing(edit.if_missing).value,
        }
    if isinstance(edit, JsonEdit):
        return {
            "path": list(edit.path),
            "value": _plain(edit.value),
            "if_missing": IfMissing(edit.if_missing).value,
        }
    return {"find": edit.find, "replace": edit.replace, "count": edit.count}


def edits_to_params(rule: ConfigRule) -> dict[str, Any]:
    """Return the JSON-serializable form of ``rule`` used as :attr:`Action.params`.

    The result is ``{"format": ..., "encoding": ..., "edits": [...]}`` where an ``ini``
    edit is ``{"section", "key", "value", "if_missing"}``, a ``json`` edit is
    ``{"path", "value", "if_missing"}`` and a ``text`` edit is
    ``{"find", "replace", "count"}``. It holds everything
    :meth:`ConfigTransformer.transform` needs and shares no mutable object with the
    profile.

    Raises:
        TypeError: an edit is not of the kind the rule's ``format`` calls for (the
            profile loader never builds such a rule).
    """
    fmt = ConfigFormat(rule.format)
    expected = _EDIT_TYPES[fmt]
    for number, edit in enumerate(rule.edits):
        if not isinstance(edit, expected):
            raise TypeError(
                f"edits[{number}] is a {type(edit).__name__}, but format {fmt.value!r} "
                f"takes {expected.__name__} entries"
            )
    return {
        "format": fmt.value,
        "encoding": rule.encoding,
        "edits": [_edit_to_params(edit) for edit in rule.edits],
    }


def _read_bytes(path: Path) -> bytes:
    with open(fs_path(path), "rb") as handle:
        return handle.read()


class ConfigTransformer(Transformer):
    """Applies the ``configs`` entries of a profile to text files of the RomFS."""

    name = "config"
    version = 1

    def supports(self, path: str) -> bool:
        """Tell whether ``path`` is a RomFS file.

        Configuration files have no telling name or extension: which of them are edited
        is entirely up to the rules of the profile.
        """
        return path.startswith(ROMFS_PREFIX) and len(path) > len(ROMFS_PREFIX)

    def plan(self, context: PlanContext) -> Iterator[PlanItem]:
        """Yield what the ``configs`` entries of the profile mean for this game.

        Every selected file is read and edited in memory, so that a profile that does
        not fit is reported before anything is built:

        * an :class:`Action` for a file the edits change;
        * nothing for a file that already holds the requested values;
        * a :class:`Skip` of kind ``MISMATCH`` for a required entry that selects no file
          (its source is ``configs[i]``), for a file selected by several entries, and for
          a file an edit cannot be applied to;
        * a :class:`Skip` of kind ``INVALID`` for a file that cannot be read.
        """
        rules = context.profile.configs
        files = context.game.romfs_files
        selected: list[list[int]] = [[] for _ in rules]
        owners: list[list[int]] = [[] for _ in files]
        for position, entry in enumerate(files):
            for index, rule in enumerate(rules):
                if rule.matches(entry.path):
                    selected[index].append(position)
                    owners[position].append(index)

        for index, rule in enumerate(rules):
            if not selected[index]:
                if rule.required:
                    yield Skip(self.name, f"configs[{index}]", SkipKind.MISMATCH, _no_file(rule))
                continue
            params = edits_to_params(rule)
            for position in selected[index]:
                entry = files[position]
                claimed = owners[position]
                if len(claimed) > 1:
                    # Reported once, by the first entry that selects the file. Applying
                    # the entries one after the other would make the result depend on
                    # their order, which a profile never states.
                    if claimed[0] == index:
                        names = [f"configs[{owner}]" for owner in claimed]
                        yield Skip(
                            self.name,
                            entry.logical,
                            SkipKind.MISMATCH,
                            f"selected by {', '.join(names[:-1])} and {names[-1]}; a file may "
                            f"be selected by one configs entry only",
                        )
                    continue
                item = self._plan_file(context, entry, params)
                if item is not None:
                    yield item

    def _plan_file(
        self, context: PlanContext, entry: FileEntry, params: Mapping[str, Any]
    ) -> PlanItem | None:
        """Try the edits on one file in memory and say what has to be done."""
        logical = entry.logical
        try:
            data = _read_bytes(context.game.resolve(logical))
        except OSError as exc:
            reason = f"cannot read the file: {exc.strerror or exc}"
            return Skip(self.name, logical, SkipKind.INVALID, reason)
        except ValueError as exc:
            return Skip(self.name, logical, SkipKind.INVALID, f"cannot read the file: {exc}")
        fmt: str = params["format"]
        edits: Sequence[Mapping[str, Any]] = params["edits"]
        try:
            edited = apply_config_edits(data, fmt, edits, encoding=params["encoding"])
        except MismatchError as exc:
            return Skip(self.name, logical, SkipKind.MISMATCH, str(exc))
        if edited == data:
            return None
        count = len(edits)
        return Action(
            transformer=self.name,
            source=logical,
            output=logical,
            params=params,
            summary=f"{count} {'edit' if count == 1 else 'edits'} ({fmt})",
            details={"format": fmt, "edits": count},
        )

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        """Write to ``dst`` the content of ``src`` after the edits of ``config``.

        ``src`` is only read. Nothing is written when an edit cannot be applied.

        Raises:
            MismatchError: an edit does not fit the file (see :func:`apply_config_edits`).
            InputError: ``src`` cannot be read.
            OutputError: ``dst`` cannot be written.
        """
        fmt: str = config["format"]
        edits: Sequence[Mapping[str, Any]] = config["edits"]
        try:
            data = _read_bytes(src)
        except OSError as exc:
            raise InputError(f"cannot read {src}: {exc.strerror or exc}") from exc
        edited = apply_config_edits(data, fmt, edits, encoding=config.get("encoding"))
        try:
            with open(fs_path(dst), "wb") as handle:
                handle.write(edited)
        except OSError as exc:
            raise OutputError(f"cannot write {dst}: {exc.strerror or exc}") from exc
        return TransformResult(
            details={
                "format": fmt,
                "edits": len(edits),
                "bytes_in": len(data),
                "bytes_out": len(edited),
            }
        )


def _no_file(rule: ConfigRule) -> str:
    """Explain that a required ``configs`` entry selects no file of this game."""
    patterns = ", ".join(repr(pattern) for pattern in rule.match)
    excluded = ""
    if rule.exclude:
        excluded = f" (excluding {', '.join(repr(pattern) for pattern in rule.exclude)})"
    return (
        f"no RomFS file matches {patterns}{excluded}, but the profile expects one to edit; "
        f"set required: false in that configs entry if the file is optional"
    )
