"""Tests for bea.hexutil: human hex strings, title IDs and build IDs."""

from __future__ import annotations

import pytest

from bea import hexutil


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1F 20 03 D5", b"\x1f\x20\x03\xd5"),
        ("1f2003d5", b"\x1f\x20\x03\xd5"),
        ("0x1F, 0x20, 0x03, 0xD5", b"\x1f\x20\x03\xd5"),
        ("1F2003D5 E0031F2A", b"\x1f\x20\x03\xd5\xe0\x03\x1f\x2a"),
        ("  00\t01\n02  ", b"\x00\x01\x02"),
        ("DE:AD-BE_EF", b"\xde\xad\xbe\xef"),
        ("0xDEADBEEF", b"\xde\xad\xbe\xef"),
    ],
)
def test_parse_hex_bytes_accepts_common_spellings(text: str, expected: bytes) -> None:
    assert hexutil.parse_hex_bytes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0X1f", b"\x1f"),
        ("1f:20;03,d5", b"\x1f\x20\x03\xd5"),
        ("0x1F_0x20", b"\x1f\x20"),
        ("1F\r\n20\t03", b"\x1f\x20\x03"),
        ("1F 20", b"\x1f\x20"),  # a no-break space pasted from a web page is a space
        ("00", b"\x00"),
        ("ff" * 300, b"\xff" * 300),  # no length limit here: limits belong to the loader
    ],
)
def test_parse_hex_bytes_accepts_more_spellings(text: str, expected: bytes) -> None:
    assert hexutil.parse_hex_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "1F 2 03", "XY", "0x", "1F 0xZZ", "123", "1F,,G0"])
def test_parse_hex_bytes_rejects_bad_text(text: str) -> None:
    with pytest.raises(ValueError):
        hexutil.parse_hex_bytes(text)


@pytest.mark.parametrize(
    "text",
    [
        " , ; ",  # separators only
        "0x1",  # one digit is half a byte
        "1F 20 0",
        "1F 0x",
        "0x0x1F",
        "x1F",
        "1F h",
        "1F+20",
        "1F.20",
        "１Ｆ",  # full-width "1F": looks like hex, is not
        "١٢",  # digits of another script
    ],
)
def test_parse_hex_bytes_rejects_more_bad_text(text: str) -> None:
    with pytest.raises(ValueError):
        hexutil.parse_hex_bytes(text)


def test_parse_hex_bytes_names_the_offending_token() -> None:
    with pytest.raises(ValueError, match="'2'"):
        hexutil.parse_hex_bytes("1F 2 03")
    with pytest.raises(ValueError, match="'G0'"):
        hexutil.parse_hex_bytes("1F G0")


@pytest.mark.parametrize("value", [12345678, 0x1F, None, b"1F", 1.5])
def test_parse_hex_bytes_rejects_non_strings(value: object) -> None:
    # An unquoted YAML scalar such as `expected: 12345678` arrives as an int.
    with pytest.raises(TypeError):
        hexutil.parse_hex_bytes(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [bytearray(b"1F"), ["1F"], ("1F",), True])
def test_parse_hex_bytes_rejects_more_non_strings(value: object) -> None:
    with pytest.raises(TypeError):
        hexutil.parse_hex_bytes(value)  # type: ignore[arg-type]


def test_format_hex_bytes_round_trips() -> None:
    data = bytes(range(0, 256, 17))
    assert hexutil.format_hex_bytes(b"\x1f\x20\x03\xd5") == "1F 20 03 D5"
    assert hexutil.format_hex_bytes(b"\x1f\x20", sep="") == "1F20"
    assert hexutil.format_hex_bytes(b"") == ""
    assert hexutil.parse_hex_bytes(hexutil.format_hex_bytes(data)) == data


def test_format_hex_bytes_pads_and_uses_upper_case() -> None:
    assert hexutil.format_hex_bytes(b"\x00\x0a\xab\xff") == "00 0A AB FF"
    assert hexutil.format_hex_bytes(b"\x00") == "00"
    assert hexutil.format_hex_bytes(b"\x0a\x0b", sep=", ") == "0A, 0B"
    assert hexutil.format_hex_bytes(bytearray(b"\x01\x02")) == "01 02"
    assert hexutil.format_hex_bytes(memoryview(b"\x01\x02\x03")[1:]) == "02 03"
    # Whatever accepted separator is used to print, parsing gives the bytes back.
    everything = bytes(range(256))
    for sep in (" ", "", ",", ", ", ":", "-", "_", "\n"):
        assert hexutil.parse_hex_bytes(hexutil.format_hex_bytes(everything, sep)) == everything


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (255, 255), ("255", 255), ("0x1A2B3C", 0x1A2B3C), ("0X10", 16), (" 0x1_000 ", 4096)],
)
def test_parse_int(value: int | str, expected: int) -> None:
    assert hexutil.parse_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-5, -5),
        (1 << 80, 1 << 80),
        ("0", 0),
        ("0x0", 0),
        ("0xff", 255),
        ("0XFF", 255),
        ("0x7100000000", 0x7100000000),
        ("1_000_000", 1000000),
        ("\t42\n", 42),
        # A leading zero does not mean octal: the text is decimal unless it says 0x.
        ("0100", 100),
        # Signs, for decimal and hexadecimal alike.
        ("-5", -5),
        ("+7", 7),
        ("-0x10", -16),
        ("+0x1F", 31),
        ("-0X1f", -31),
    ],
)
def test_parse_int_more_spellings(value: int | str, expected: int) -> None:
    assert hexutil.parse_int(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "0x", "12.5", "1A"])
def test_parse_int_rejects_bad_text(value: str) -> None:
    with pytest.raises(ValueError):
        hexutil.parse_int(value)


@pytest.mark.parametrize(
    "value",
    [
        "   ",
        "_",
        "-",
        "+",
        "--5",
        "0x-5",
        "-0x",
        "0xG",
        "ff",  # hexadecimal digits without the prefix are not a decimal number
        "0b101",
        "0o17",
        "1e3",
        "1 2",
        "0x 10",
        "twelve",
    ],
)
def test_parse_int_rejects_more_bad_text(value: str) -> None:
    with pytest.raises(ValueError):
        hexutil.parse_int(value)


@pytest.mark.parametrize("value", [True, False, None, 1.0, b"1"])
def test_parse_int_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeError):
        hexutil.parse_int(value)  # type: ignore[arg-type]


def test_normalize_title_id() -> None:
    synthetic = (0x0100 << 48) | 0xABCDEF123000
    text = f"{synthetic:016x}"
    assert hexutil.normalize_title_id(text) == text.upper()
    assert hexutil.normalize_title_id("0x" + text) == text.upper()
    assert hexutil.normalize_title_id(f"  {text.upper()}  ") == text.upper()
    assert hexutil.normalize_title_id(synthetic) == text.upper()
    assert hexutil.normalize_title_id(0) == "0" * 16
    assert hexutil.is_title_id(text)
    assert not hexutil.is_title_id(text[:-1])
    assert not hexutil.is_title_id(text + "0")
    assert not hexutil.is_title_id("G" * 16)
    assert not hexutil.is_title_id(synthetic)


@pytest.mark.parametrize(
    "value", ["", "0100", "0100ABCDEF12300", "0100ABCDEF1230000", "Z" * 16, -1, 1 << 64]
)
def test_normalize_title_id_rejects_bad_values(value: int | str) -> None:
    with pytest.raises(ValueError):
        hexutil.normalize_title_id(value)


@pytest.mark.parametrize("value", [True, None, 1.5])
def test_normalize_title_id_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeError):
        hexutil.normalize_title_id(value)  # type: ignore[arg-type]


def test_build_id_canonical_form_strips_trailing_zero_bytes() -> None:
    sha1_like = bytes(range(1, 21))  # 20 significant bytes, as most linkers emit
    module_id = sha1_like + bytes(12)
    canonical = sha1_like.hex().upper()
    assert hexutil.build_id_from_bytes(module_id) == canonical
    assert hexutil.build_id_from_bytes(sha1_like) == canonical
    assert hexutil.normalize_build_id(module_id.hex()) == canonical
    assert hexutil.normalize_build_id(canonical.lower()) == canonical
    assert hexutil.build_id_to_bytes(canonical) == module_id
    assert len(hexutil.build_id_to_bytes("AB")) == hexutil.BUILD_ID_SIZE


def test_build_id_keeps_interior_and_handles_trailing_zero_byte() -> None:
    # A real ID may legitimately end in a zero byte: trimming it is still unambiguous
    # because loaders zero-pad file names before comparing.
    assert hexutil.normalize_build_id("AB00CD00") == "AB00CD"
    assert hexutil.normalize_build_id("00AB") == "00AB"
    assert hexutil.build_ids_equal("AB00CD00", "ab00cd")
    assert hexutil.build_ids_equal("AB00CD", "AB00CD" + "00" * 29)
    assert not hexutil.build_ids_equal("AB00CD", "AB00CE")
    assert not hexutil.build_ids_equal("AB", "00AB")


def test_build_id_all_zero_is_never_empty() -> None:
    assert hexutil.build_id_from_bytes(bytes(32)) == "00"
    assert hexutil.normalize_build_id("0000") == "00"
    assert hexutil.build_id_from_bytes(b"") == "00"


@pytest.mark.parametrize("text", ["", "ABC", "XYZW", "AB" * 33, "AB CD"])
def test_build_id_rejects_bad_text(text: str) -> None:
    with pytest.raises(ValueError):
        hexutil.build_id_to_bytes(text)


def test_build_id_rejects_non_strings_and_oversized_bytes() -> None:
    with pytest.raises(TypeError):
        hexutil.build_id_to_bytes(1234)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hexutil.build_id_from_bytes(bytes(33))


def synthetic_title_id() -> int:
    # Built at run time so that no title-ID-shaped literal appears in this file.
    return (0x0100 << 48) | 0xBEA0BEA0B000


def test_normalize_title_id_more_spellings() -> None:
    value = synthetic_title_id()
    upper = f"{value:016X}"
    assert len(upper) == 16
    for spelling in (upper, upper.lower(), "0x" + upper, "0X" + upper.lower(), f"\t{upper}\n"):
        assert hexutil.normalize_title_id(spelling) == upper
    assert hexutil.normalize_title_id(value) == upper
    # Normalizing twice changes nothing, and the result is what is_title_id() accepts.
    assert hexutil.normalize_title_id(hexutil.normalize_title_id(value)) == upper
    assert hexutil.is_title_id(hexutil.normalize_title_id(value))
    # Integers are padded to the full width.
    assert hexutil.normalize_title_id(1) == "0" * 15 + "1"
    assert hexutil.normalize_title_id(0xABC) == "0" * 13 + "ABC"
    assert hexutil.normalize_title_id((1 << 64) - 1) == "F" * 16


def test_normalize_title_id_rejects_more_bad_values() -> None:
    upper = f"{synthetic_title_id():016X}"
    bad_texts = [
        "0x" + upper[:14],  # sixteen characters, but only fourteen digits
        "0x" + upper + "0",
        upper[:8] + " " + upper[8:],
        upper[:8] + "-" + upper[8:],
        upper[:15] + "G",
        "0x",
        "   ",
        "０" * 16,  # full-width zeros
        "+" + upper[1:],
    ]
    for text in bad_texts:
        with pytest.raises(ValueError):
            hexutil.normalize_title_id(text)
    with pytest.raises(ValueError):
        hexutil.normalize_title_id(-(1 << 63))
    for value in (b"0" * 16, 1.0, [upper], False):
        with pytest.raises(TypeError):
            hexutil.normalize_title_id(value)  # type: ignore[arg-type]


def test_is_title_id_is_exact() -> None:
    upper = f"{synthetic_title_id():016X}"
    assert hexutil.is_title_id(upper)
    assert hexutil.is_title_id(upper.lower())
    assert hexutil.is_title_id("0" * 16)
    for value in (
        "0x" + upper,
        " " + upper,
        upper + "\n",
        upper[:15],
        upper[:15] + "g",
        "０" * 16,
        upper.encode(),
        None,
        16,
        ["0" * 16],
    ):
        assert not hexutil.is_title_id(value), value


def test_build_id_accepts_the_full_32_bytes() -> None:
    longest = "AB" * 32
    assert hexutil.build_id_to_bytes(longest) == b"\xab" * 32
    assert hexutil.normalize_build_id(longest) == longest
    assert hexutil.normalize_build_id(f"  {longest.lower()}\n") == longest
    assert hexutil.build_id_from_bytes(b"\xab" * 32) == longest
    assert hexutil.build_id_from_bytes(bytearray(b"\x01\x00\x02\x00")) == "010002"
    with pytest.raises(ValueError, match="at most 64"):
        hexutil.build_id_to_bytes(longest + "00")


@pytest.mark.parametrize("text", ["0xAB", "AB-CD", "AB:CD", "A", "ABG0", " ", "ＡＢ"])
def test_build_id_rejects_more_bad_text(text: str) -> None:
    for function in (hexutil.build_id_to_bytes, hexutil.normalize_build_id):
        with pytest.raises(ValueError):
            function(text)
    with pytest.raises(ValueError):
        hexutil.build_ids_equal(text, "AB")
    with pytest.raises(ValueError):
        hexutil.build_ids_equal("AB", text)


def test_build_id_text_and_bytes_round_trip() -> None:
    # Every length, with and without zero bytes in the middle and at the end.
    for length in range(33):
        for module_id in (
            bytes(range(1, length + 1)),
            bytes(length),
            bytes(range(1, length + 1))[: length // 2] + bytes(length - length // 2),
            bytes(i if i % 3 else 0 for i in range(1, length + 1)),
        ):
            text = hexutil.build_id_from_bytes(module_id)
            assert text == text.upper() and len(text) % 2 == 0 and text
            padded = module_id.ljust(32, b"\x00")
            assert hexutil.build_id_to_bytes(text) == padded
            assert hexutil.normalize_build_id(text) == text
            assert hexutil.normalize_build_id(padded.hex()) == text
            assert hexutil.build_ids_equal(text, padded.hex())
            # Canonical text never ends in a zero byte, unless the ID is all zeros.
            assert not text.endswith("00") or text == "00"


def test_build_ids_equal_is_not_prefix_matching() -> None:
    assert hexutil.build_ids_equal("ab", "AB")
    assert hexutil.build_ids_equal("AB", "AB00")
    assert hexutil.build_ids_equal("AB" + "00" * 31, "AB")
    assert not hexutil.build_ids_equal("AB", "ABCD")
    assert not hexutil.build_ids_equal("ABCD", "AB")
    assert not hexutil.build_ids_equal("AB", "00AB")
    assert not hexutil.build_ids_equal("00", "01")
    assert hexutil.build_ids_equal("00", "0000")


def test_every_public_name_is_exported() -> None:
    for name in hexutil.__all__:
        assert hasattr(hexutil, name), name
    defined = {
        name
        for name, value in vars(hexutil).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == hexutil.__name__
    }
    assert defined <= set(hexutil.__all__)
    assert (hexutil.BUILD_ID_SIZE, hexutil.TITLE_ID_DIGITS) == (32, 16)


def _title(value: int) -> str:
    return f"{value:016X}"


def test_application_id_range() -> None:
    first = (0x0100 << 48) | 0x10000
    assert first == hexutil.APPLICATION_ID_MIN
    assert hexutil.is_application_id(_title(first))
    assert hexutil.is_application_id(first + 0x1234_5000)
    assert not hexutil.is_application_id(first - 1)  # system software
    assert not hexutil.is_application_id(_title(0x0100 << 48))
    assert hexutil.is_application_id((0x0200 << 48) - 1)
    assert not hexutil.is_application_id(0x0200 << 48)
    with pytest.raises(ValueError):
        hexutil.is_application_id("not an id")


def test_update_and_base_ids() -> None:
    base = (0x0100 << 48) | 0xABCDE_2000
    assert hexutil.is_update_id(base + 0x800)
    assert not hexutil.is_update_id(base)
    assert not hexutil.is_update_id(base + 0x1000)  # add-on content base
    assert not hexutil.is_update_id(base + 0x1800)  # add-on content item 0x800
    assert not hexutil.is_update_id(base + 1)  # program index 1
    for member in (base, base + 1, base + 0x800, base + 0x1000, base + 0x1FFF):
        assert hexutil.base_program_id(member) == _title(base)
    assert hexutil.base_program_id(_title(base + 0x800).lower()) == _title(base)
