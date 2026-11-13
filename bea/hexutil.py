"""Hexadecimal helpers shared by profiles, format parsers and the CLI.

Three kinds of identifiers are normalized here so that every other module compares
them in exactly one textual form:

* byte strings written by humans in profiles (``"1F 20 03 D5"``);
* title IDs (program IDs): 64-bit values printed as 16 uppercase hex digits;
* build IDs (NSO module IDs): up to 32 bytes, printed as uppercase hex with the
  trailing zero *bytes* removed.
"""

from __future__ import annotations

import re

__all__ = [
    "APPLICATION_ID_MAX",
    "APPLICATION_ID_MIN",
    "BUILD_ID_SIZE",
    "TITLE_ID_DIGITS",
    "base_program_id",
    "build_id_from_bytes",
    "build_id_to_bytes",
    "build_ids_equal",
    "format_hex_bytes",
    "is_application_id",
    "is_title_id",
    "is_update_id",
    "normalize_build_id",
    "normalize_title_id",
    "parse_hex_bytes",
    "parse_int",
]

#: Size in bytes of the module ID field of an NSO header.
BUILD_ID_SIZE = 32
#: Number of hex digits of a title ID in text form.
TITLE_ID_DIGITS = 16

#: Range of application (game) program IDs. Lower IDs belong to system software, which
#: BEA never patches. Written arithmetically: no title ID literal appears in the engine.
APPLICATION_ID_MIN = (0x0100 << 48) | 0x10000
APPLICATION_ID_MAX = (0x0200 << 48) - 1
#: Low bits that tell the members of one application's ID family apart (program index,
#: update, add-on content).
_ID_FAMILY_BITS = 0x1FFF
_UPDATE_BITS = 0x800

_HEX_RUN = re.compile(r"[0-9A-Fa-f]+")
_SEPARATORS = re.compile(r"[\s,;:_-]+")


def parse_hex_bytes(text: str) -> bytes:
    """Parse a human-written byte string into ``bytes``.

    Accepted spellings: ``"1F 20 03 D5"``, ``"1f2003d5"``, ``"0x1F, 0x20, 0x03, 0xD5"``,
    ``"1F2003D5 E0031F2A"``. Tokens are separated by whitespace or ``, ; : _ -``; each
    token may start with ``0x`` and must hold an even number of hex digits, so a typo
    such as ``"1F 2 03"`` is rejected instead of being silently re-grouped.

    Raises:
        TypeError: ``text`` is not a ``str`` (a YAML scalar that was not quoted often
            arrives as ``int``; callers should surface a "quote this value" hint).
        ValueError: the text is empty or is not valid hex.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected a hex string, got {type(text).__name__}")
    tokens = [token for token in _SEPARATORS.split(text.strip()) if token]
    if not tokens:
        raise ValueError("empty hex string")
    out = bytearray()
    for token in tokens:
        digits = token[2:] if token[:2] in ("0x", "0X") else token
        if not digits or _HEX_RUN.fullmatch(digits) is None:
            raise ValueError(f"not hexadecimal: {token!r}")
        if len(digits) % 2:
            raise ValueError(f"odd number of hex digits in {token!r}")
        out += bytes.fromhex(digits)
    return bytes(out)


def format_hex_bytes(data: bytes, sep: str = " ") -> str:
    """Format bytes as uppercase hex pairs joined by ``sep`` (``"1F 20 03 D5"``)."""
    return sep.join(f"{byte:02X}" for byte in data)


def parse_int(value: int | str) -> int:
    """Parse an integer given as ``int`` or as decimal / ``0x`` hexadecimal text.

    Raises:
        TypeError: for ``bool`` or any other type.
        ValueError: for text that is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    if isinstance(value, int):
        return value
    text = value.strip().replace("_", "")
    sign = -1 if text[:1] == "-" else 1
    if text[:1] in ("-", "+"):
        text = text[1:]
    # After the sign: digits only (int() would also take a second sign or white space).
    if not text or not text[0].isalnum():
        raise ValueError(f"not an integer: {value!r}")
    if text[:2] in ("0x", "0X"):
        digits = text[2:]
        if not digits or not digits[0].isalnum():
            raise ValueError(f"not an integer: {value!r}")
        return sign * int(digits, 16)
    return sign * int(text, 10)


def normalize_title_id(value: int | str) -> str:
    """Return a title ID as 16 uppercase hex digits.

    Accepts an ``int`` or text with an optional ``0x`` prefix.

    Raises:
        TypeError: for ``bool`` or any other type.
        ValueError: if the value is not exactly a 64-bit ID written with 16 hex digits.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError(f"expected a title ID, got {type(value).__name__}")
    if isinstance(value, int):
        if not 0 <= value < 1 << 64:
            raise ValueError(f"title ID out of range: {value}")
        return f"{value:016X}"
    text = value.strip()
    if text[:2] in ("0x", "0X"):
        text = text[2:]
    if len(text) != TITLE_ID_DIGITS or _HEX_RUN.fullmatch(text) is None:
        raise ValueError(f"a title ID is {TITLE_ID_DIGITS} hex digits, got {value!r}")
    return text.upper()


def is_title_id(value: object) -> bool:
    """Tell whether ``value`` is text made of exactly 16 hex digits."""
    return (
        isinstance(value, str)
        and len(value) == TITLE_ID_DIGITS
        and _HEX_RUN.fullmatch(value) is not None
    )


def _id_value(value: int | str) -> int:
    return int(normalize_title_id(value), 16)


def is_application_id(value: int | str) -> bool:
    """Tell whether a title ID belongs to an application rather than to system software.

    Raises:
        TypeError, ValueError: ``value`` is not a title ID (see :func:`normalize_title_id`).
    """
    return APPLICATION_ID_MIN <= _id_value(value) <= APPLICATION_ID_MAX


def is_update_id(value: int | str) -> bool:
    """Tell whether a title ID is the ID of an application's update (base ID + 0x800).

    Raises:
        TypeError, ValueError: ``value`` is not a title ID.
    """
    return _id_value(value) & _ID_FAMILY_BITS == _UPDATE_BITS


def base_program_id(value: int | str) -> str:
    """Return the base application ID of any member of its family, in canonical text.

    The members of a family (program *n*, update, add-on content) differ only in the
    low 13 bits, which are zero for the base ID.

    Raises:
        TypeError, ValueError: ``value`` is not a title ID.
    """
    return normalize_title_id(_id_value(value) & ~_ID_FAMILY_BITS)


def build_id_to_bytes(text: str) -> bytes:
    """Convert build-ID text into the 32-byte module ID it denotes (zero padded).

    Raises:
        TypeError: ``text`` is not a ``str``.
        ValueError: not hex, odd length, empty, or longer than 64 digits.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected a build ID string, got {type(text).__name__}")
    digits = text.strip()
    if not digits or _HEX_RUN.fullmatch(digits) is None:
        raise ValueError(f"a build ID is hexadecimal text, got {text!r}")
    if len(digits) % 2:
        raise ValueError(f"a build ID has an even number of hex digits, got {text!r}")
    if len(digits) > 2 * BUILD_ID_SIZE:
        raise ValueError(f"a build ID has at most {2 * BUILD_ID_SIZE} hex digits, got {text!r}")
    return bytes.fromhex(digits).ljust(BUILD_ID_SIZE, b"\x00")


def build_id_from_bytes(module_id: bytes) -> str:
    """Return the canonical text of a module ID: uppercase hex, trailing zero bytes removed.

    An all-zero ID is written ``"00"`` so that the result is never empty.
    """
    if len(module_id) > BUILD_ID_SIZE:
        raise ValueError(f"a module ID has at most {BUILD_ID_SIZE} bytes, got {len(module_id)}")
    trimmed = module_id.rstrip(b"\x00") or b"\x00"
    return trimmed.hex().upper()


def normalize_build_id(text: str) -> str:
    """Return the canonical form of build-ID text (see :func:`build_id_from_bytes`)."""
    return build_id_from_bytes(build_id_to_bytes(text))


def build_ids_equal(left: str, right: str) -> bool:
    """Compare two build IDs the way mod loaders do: zero-pad both to 32 bytes."""
    return build_id_to_bytes(left) == build_id_to_bytes(right)
