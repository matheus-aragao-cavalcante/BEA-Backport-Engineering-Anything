"""Tests for bea.formats.ips: the IPS / IPS32 writer, the strict reader and the applier.

Expected bytes are either the golden vectors of the format notes or laid out by hand
from the documented layout (big-endian offset of 3 or 4 bytes, 16-bit size, payload).
Every target buffer is synthetic: random bytes from a seeded generator.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import random
import re
import struct
from collections.abc import Iterable
from typing import NamedTuple

import pytest

from bea.errors import BeaError, FormatError
from bea.formats import ips
from bea.formats.ips import IpsFormat, IpsPatch, IpsRecord

# ----------------------------------------------------------------------------------
# The documented layout, written down independently of the module under test
# ----------------------------------------------------------------------------------

#: The offsets whose bytes spell a terminator: no record may start there.
EOF_OFFSET = int.from_bytes(b"EOF", "big")
EEOF_OFFSET = int.from_bytes(b"EEOF", "big")
#: The most one record carries (its size field has 16 bits).
FULL = 0xFFFF


class Flavour(NamedTuple):
    """What tells IPS and IPS32 apart, as the documentation states it."""

    fmt: IpsFormat
    magic: bytes
    terminator: bytes
    #: Width of a record offset, in bytes.
    width: int
    #: The offset no record may start at.
    forbidden: int


CLASSIC = Flavour(IpsFormat.IPS, b"PATCH", b"EOF", 3, EOF_OFFSET)
WIDE = Flavour(IpsFormat.IPS32, b"IPS32", b"EEOF", 4, EEOF_OFFSET)
BOTH_FLAVOURS = pytest.mark.parametrize("flavour", [CLASSIC, WIDE], ids=["ips", "ips32"])


def vector(text: str) -> bytes:
    """Turn a vector written as hex pairs into bytes (bars and spaces separate fields)."""
    return bytes.fromhex(text.replace("|", " "))


# V1: literal 1F 20 03 D5 at 0x001334 and eight zero bytes at 0x002100 written as RLE.
V1_IPS = vector(
    "50 41 54 43 48 | 00 13 34 | 00 04 | 1F 20 03 D5 | 00 21 00 | 00 00 | 00 08 | 00 | 45 4F 46"
)
V1_IPS32 = vector(
    "49 50 53 33 32 | 00 00 13 34 | 00 04 | 1F 20 03 D5 "
    "| 00 00 21 00 | 00 00 | 00 08 | 00 | 45 45 4F 46"
)
#: The two changes of V1.
V1_CHANGES = [(0x1334, bytes.fromhex("1F2003D5")), (0x2100, bytes(8))]
# The same two changes the way the writer puts them: two literal records, no RLE.
V1_LITERAL = vector(
    "50 41 54 43 48 | 00 13 34 | 00 04 | 1F 20 03 D5 "
    "| 00 21 00 | 00 08 | 00 00 00 00 00 00 00 00 | 45 4F 46"
)
V1_LITERAL_IPS32 = vector(
    "49 50 53 33 32 | 00 00 13 34 | 00 04 | 1F 20 03 D5 "
    "| 00 00 21 00 | 00 08 | 00 00 00 00 00 00 00 00 | 45 45 4F 46"
)
# Write FF at 0x10, then cut the output down to 0x20 bytes.
TRUNCATING = vector("50 41 54 43 48 00 00 10 00 01 FF 45 4F 46 00 00 20")
# Writing AA at 0x454F46 where the byte before it holds 11: the right and the wrong way.
COLLISION_RIGHT = vector("50 41 54 43 48 | 45 4F 45 | 00 02 | 11 AA | 45 4F 46")
COLLISION_WRONG = vector("50 41 54 43 48 45 4F 46 00 01 AA 45 4F 46")
SMALLEST_IPS = vector("50 41 54 43 48 45 4F 46")
SMALLEST_IPS32 = vector("49 50 53 33 32 45 45 4F 46")


def literal(offset: int, payload: bytes, width: int = 3) -> bytes:
    """Lay out one literal record by hand: big-endian offset, 16-bit size, payload."""
    assert 1 <= len(payload) <= FULL
    return offset.to_bytes(width, "big") + struct.pack(">H", len(payload)) + payload


def rle(offset: int, run: int, fill: int, width: int = 3) -> bytes:
    """Lay out one RLE record by hand: offset, a size of 0, 16-bit run length, fill byte."""
    return offset.to_bytes(width, "big") + struct.pack(">HHB", 0, run, fill)


def patch_file(flavour: Flavour, records: Iterable[tuple[int, bytes]]) -> bytes:
    """Lay out a whole file of literal records by hand."""
    body = b"".join(literal(offset, payload, flavour.width) for offset, payload in records)
    return flavour.magic + body + flavour.terminator


def read_back(blob: bytes) -> list[tuple[int, bytes]]:
    """Read what the writer produced with an independent walker.

    The walker follows the documented reading order (the terminator first, then offset,
    size and payload) and fails on everything the writer promises never to emit: an RLE
    record, records that are out of order or overlap, bytes after the terminator.
    """
    flavour = {b"PATCH": CLASSIC, b"IPS32": WIDE}[blob[:5]]
    header = flavour.width + 2
    position = 5
    previous_end = 0
    records = []
    while blob[position : position + flavour.width] != flavour.terminator:
        offset = int.from_bytes(blob[position : position + flavour.width], "big")
        (size,) = struct.unpack_from(">H", blob, position + flavour.width)
        assert size != 0, "the writer emitted an RLE record"
        payload = blob[position + header : position + header + size]
        assert len(payload) == size, "the last record is cut short"
        assert offset >= previous_end, "records are out of order or overlap"
        records.append((offset, payload))
        previous_end = offset + size
        position += header + size
    assert position + flavour.width == len(blob), "something follows the terminator"
    return records


def edit(target: bytes, changes: Iterable[tuple[int, bytes]], bias: int = 0) -> bytes:
    """The expected outcome, computed directly: overwrite each range of a copy in memory."""
    out = bytearray(target)
    for offset, data in changes:
        start = offset - bias
        # A slice assignment out of range would silently grow the copy.
        assert 0 <= start <= len(out) - len(data), "this change does not lie inside the target"
        out[start : start + len(data)] = data
    return bytes(out)


class Target:
    """A synthetic stretch of target bytes that begins at ``base``.

    Its ``original_byte`` is the callback the writer may ask for a byte of the target; it
    records every question.
    """

    def __init__(self, base: int, size: int, seed: int) -> None:
        self.base = base
        self.data = random.Random(seed).randbytes(size)
        self.asked: list[int] = []

    def at(self, offset: int) -> int:
        return self.data[offset - self.base]

    def original_byte(self, offset: int) -> int:
        self.asked.append(offset)
        return self.at(offset)

    def edited(self, changes: Iterable[tuple[int, bytes]]) -> bytes:
        return edit(self.data, changes, self.base)

    def patched(self, patch: IpsPatch | bytes) -> bytes:
        return ips.apply_ips(self.data, patch, bias=self.base)


def around(flavour: Flavour, seed: int, reach: int = 0x40) -> Target:
    """A synthetic target that covers ``reach`` bytes on each side of the forbidden offset."""
    return Target(flavour.forbidden - reach, 2 * reach, seed)


def must_not_be_called(offset: int) -> int:
    raise AssertionError(f"original_byte(0x{offset:X}) was called although no byte is needed")


def assert_means(blob: bytes, flavour: Flavour, target: Target, changes: list) -> None:
    """Prove that a file from the writer does what was asked, and that readers get it.

    The independent walker must reach the terminator without meeting a record at the
    forbidden offset, the module's own reader must see the same records, and applying
    them must give the same bytes as editing the target directly.
    """
    records = read_back(blob)
    assert blob[:5] == flavour.magic
    assert all(offset != flavour.forbidden for offset, _payload in records)
    assert all(1 <= len(payload) <= FULL for _offset, payload in records)
    patch = ips.parse_ips(blob)
    assert patch.format is flavour.fmt
    assert patch.truncate is None
    assert [(record.offset, record.data) for record in patch.records] == records
    assert target.patched(patch) == target.edited(changes)
    assert target.patched(blob) == target.edited(changes)


# ----------------------------------------------------------------------------------
# Constants and plain data types
# ----------------------------------------------------------------------------------


def test_constants_follow_the_documented_layout() -> None:
    assert (ips.IPS_MAGIC, ips.IPS_TERMINATOR) == (b"PATCH", b"EOF")
    assert (ips.IPS32_MAGIC, ips.IPS32_TERMINATOR) == (b"IPS32", b"EEOF")
    assert ips.MAX_RECORD_SIZE == 0xFFFF
    assert (EOF_OFFSET, EEOF_OFFSET) == (0x454F46, 0x45454F46)


def test_format_properties() -> None:
    assert [fmt.value for fmt in IpsFormat] == ["ips", "ips32"]
    assert IpsFormat("ips") is IpsFormat.IPS
    assert IpsFormat("ips32") is IpsFormat.IPS32
    classic, wide = IpsFormat.IPS, IpsFormat.IPS32
    assert (classic.magic, classic.terminator, classic.offset_size) == (b"PATCH", b"EOF", 3)
    assert (wide.magic, wide.terminator, wide.offset_size) == (b"IPS32", b"EEOF", 4)
    assert (classic.max_offset, wide.max_offset) == (0xFFFFFF, 0xFFFFFFFF)
    assert (classic.forbidden_offset, wide.forbidden_offset) == (0x454F46, 0x45454F46)
    # A member is its own JSON form, so it can go into a report as it is.
    assert json.dumps({"format": wide}) == '{"format": "ips32"}'


def test_record_and_patch_are_frozen_values() -> None:
    record = IpsRecord(0x1334, bytes.fromhex("1F2003D5"))
    assert record.end == 0x1338
    assert IpsRecord(0x10, b"").end == 0x10
    assert record == IpsRecord(0x1334, b"\x1f\x20\x03\xd5")
    assert len({record, IpsRecord(0x1334, b"\x1f\x20\x03\xd5"), IpsRecord(0x1335, b"\x1f")}) == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.offset = 0  # type: ignore[misc]

    patch = IpsPatch(IpsFormat.IPS, (record,))
    assert patch.truncate is None
    assert patch == IpsPatch(IpsFormat.IPS, (record,), None)
    assert patch != IpsPatch(IpsFormat.IPS32, (record,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        patch.truncate = 0x20  # type: ignore[misc]


# ----------------------------------------------------------------------------------
# parse_ips: valid files
# ----------------------------------------------------------------------------------


def test_parse_golden_vector_expands_the_rle_record() -> None:
    assert len(V1_IPS) == 25
    assert ips.parse_ips(V1_IPS) == IpsPatch(
        IpsFormat.IPS,
        (IpsRecord(0x1334, bytes.fromhex("1F2003D5")), IpsRecord(0x2100, bytes(8))),
        None,
    )


def test_parse_golden_vector_in_ips32() -> None:
    assert len(V1_IPS32) == 28
    assert ips.parse_ips(V1_IPS32) == IpsPatch(
        IpsFormat.IPS32,
        (IpsRecord(0x1334, bytes.fromhex("1F2003D5")), IpsRecord(0x2100, bytes(8))),
        None,
    )


def test_parse_reads_the_truncation_length_of_classic_ips() -> None:
    assert ips.parse_ips(TRUNCATING) == IpsPatch(IpsFormat.IPS, (IpsRecord(0x10, b"\xff"),), 0x20)
    # The three bytes are a big-endian number, and zero is a length like any other.
    assert ips.parse_ips(SMALLEST_IPS + bytes.fromhex("ABCDEF")).truncate == 0xABCDEF
    assert ips.parse_ips(SMALLEST_IPS + bytes(3)).truncate == 0
    # They are not looked at as a record: even the terminator's own bytes are a length.
    assert ips.parse_ips(SMALLEST_IPS + b"EOF") == IpsPatch(IpsFormat.IPS, (), EOF_OFFSET)


def test_parse_smallest_valid_files() -> None:
    assert ips.parse_ips(SMALLEST_IPS) == IpsPatch(IpsFormat.IPS, ())
    assert ips.parse_ips(SMALLEST_IPS32) == IpsPatch(IpsFormat.IPS32, ())


def test_parse_accepts_any_kind_of_bytes() -> None:
    expected = ips.parse_ips(V1_IPS)
    assert ips.parse_ips(bytearray(V1_IPS)) == expected
    assert ips.parse_ips(memoryview(V1_IPS)) == expected
    # A window into a larger buffer: only the window is the file.
    padded = b"\xee" * 7 + V1_IPS + b"\xee" * 9
    assert ips.parse_ips(memoryview(padded)[7:-9]) == expected
    record = expected.records[0]
    assert type(record.data) is bytes


def test_parse_rle_records_of_every_size() -> None:
    blob = (
        b"PATCH"
        + rle(0x10, 1, 0xAB)
        + literal(0x20, b"xyz")
        + rle(0xFFFFFF, FULL, 0x5A)
        + rle(0x30, 3, 0x00)
        + b"EOF"
    )
    assert ips.parse_ips(blob).records == (
        IpsRecord(0x10, b"\xab"),
        IpsRecord(0x20, b"xyz"),
        IpsRecord(0xFFFFFF, b"\x5a" * FULL),
        IpsRecord(0x30, bytes(3)),
    )
    wide = b"IPS32" + rle(0x12345678, 3, 0xFF, width=4) + b"EEOF"
    assert ips.parse_ips(wide) == IpsPatch(IpsFormat.IPS32, (IpsRecord(0x12345678, b"\xff" * 3),))


def test_parse_literal_payload_that_begins_with_zeros_is_not_rle() -> None:
    # Only the size field decides: 00 00 08 00 as a payload is four ordinary bytes.
    blob = b"PATCH" + literal(0x2100, bytes.fromhex("00000800")) + b"EOF"
    assert ips.parse_ips(blob).records == (IpsRecord(0x2100, bytes.fromhex("00000800")),)


def test_parse_keeps_file_order_and_overlapping_records() -> None:
    blob = b"PATCH" + literal(0x30, b"late") + literal(0x10, b"early") + rle(0x12, 2, 0x21) + b"EOF"
    assert ips.parse_ips(blob).records == (
        IpsRecord(0x30, b"late"),
        IpsRecord(0x10, b"early"),
        IpsRecord(0x12, b"!!"),
    )


def test_parse_reads_offsets_at_both_ends_of_the_range() -> None:
    blob = b"PATCH" + literal(0, b"a") + literal(0xFFFFFF, b"z") + b"EOF"
    assert ips.parse_ips(blob).records == (IpsRecord(0, b"a"), IpsRecord(0xFFFFFF, b"z"))
    wide = b"IPS32" + literal(0, b"a", 4) + literal(0xFFFFFFFF, b"z", 4) + b"EEOF"
    assert ips.parse_ips(wide).records == (IpsRecord(0, b"a"), IpsRecord(0xFFFFFFFF, b"z"))
    # In IPS32 the classic forbidden offset is an offset like any other.
    wide = b"IPS32" + literal(EOF_OFFSET, b"z", 4) + b"EEOF"
    assert ips.parse_ips(wide).records == (IpsRecord(EOF_OFFSET, b"z"),)


def test_parse_only_looks_for_the_terminator_where_a_record_may_start() -> None:
    # Payloads that are, begin with, end with or contain the terminator.
    payloads = [b"EOF", b"EOFxx", b"xxEOF", b"xEOFx", b"EOFEOFEOF", b"EEOF", b"PATCH", b"IPS32"]
    for flavour in (CLASSIC, WIDE):
        records = [(0x100 * (index + 1), payload) for index, payload in enumerate(payloads)]
        blob = patch_file(flavour, records)
        assert ips.parse_ips(blob) == IpsPatch(
            flavour.fmt, tuple(IpsRecord(offset, payload) for offset, payload in records)
        )

    # "EOF" spelled by the last offset byte and the size field: 00 00 45 | 4F 46 | ...
    data = random.Random(1).randbytes(0x4F46)
    blob = b"PATCH" + literal(0x45, data) + b"EOF"
    assert blob[5:10] == bytes.fromhex("000045 4F46")
    assert ips.parse_ips(blob).records == (IpsRecord(0x45, data),)
    # "EEOF" spelled the same way in IPS32: 00 00 45 45 | 4F 46 | ...
    blob = b"IPS32" + literal(0x4545, data, 4) + b"EEOF"
    assert blob[5:11] == bytes.fromhex("00004545 4F46")
    assert ips.parse_ips(blob).records == (IpsRecord(0x4545, data),)
    # ... and by the run length and the fill byte of an RLE record: | 45 4F | 46 |
    blob = b"PATCH" + rle(0x10, 0x454F, 0x46) + b"EOF"
    assert ips.parse_ips(blob).records == (IpsRecord(0x10, b"F" * 0x454F),)


# ----------------------------------------------------------------------------------
# parse_ips: malformed files
# ----------------------------------------------------------------------------------

MALFORMED = [
    # --- magic
    pytest.param(b"", "magic", 0x0, id="empty file"),
    pytest.param(b"PATC", "magic", 0x0, id="magic cut short"),
    pytest.param(b"PATCXEOF", "magic", 0x0, id="unknown magic"),
    pytest.param(b"patchEOF", "magic", 0x0, id="lowercase magic"),
    pytest.param(b"IPS33EEOF", "magic", 0x0, id="almost IPS32"),
    pytest.param(b"EOF", "magic", 0x0, id="terminator only"),
    pytest.param(bytes(16), "magic", 0x0, id="zeros"),
    # --- missing terminator / offset cut short
    pytest.param(b"PATCH", "terminator", 0x5, id="ips: nothing after the magic"),
    pytest.param(b"IPS32", "terminator", 0x5, id="ips32: nothing after the magic"),
    pytest.param(b"PATCH" + literal(0x10, b"\xff"), "terminator", 0xB, id="ips: no terminator"),
    pytest.param(
        b"IPS32" + literal(0x10, b"\xff", 4), "terminator", 0xC, id="ips32: no terminator"
    ),
    pytest.param(
        b"PATCH" + literal(0x10, b"\xff") + b"EO", "terminator", 0xB, id="ips: terminator cut"
    ),
    pytest.param(
        b"IPS32" + literal(0x10, b"\xff", 4) + b"EEO", "terminator", 0xC, id="ips32: terminator cut"
    ),
    pytest.param(b"IPS32EOF", "terminator", 0x5, id="ips32 ended by the classic terminator"),
    pytest.param(b"PATCH\x00\x13", "offset of record #1", 0x5, id="ips: offset cut short"),
    pytest.param(b"IPS32\x00\x00\x13", "offset of record #1", 0x5, id="ips32: offset cut short"),
    pytest.param(
        b"PATCH" + literal(0x10, b"ab") + b"\x00",
        "offset of record #2",
        0xC,
        id="second offset cut",
    ),
    # --- size
    pytest.param(b"PATCH\x00\x13\x34", "size of record #1", 0x8, id="ips: no size"),
    pytest.param(b"PATCH\x00\x13\x34\x00", "size of record #1", 0x8, id="ips: size cut short"),
    pytest.param(b"IPS32\x00\x00\x13\x34\x00", "size of record #1", 0x9, id="ips32: size cut"),
    # --- payload
    pytest.param(b"PATCH\x00\x13\x34\x00\x04", "payload of record #1", 0xA, id="ips: no payload"),
    pytest.param(
        b"PATCH\x00\x13\x34\x00\x04\x1f\x20\x03", "payload of record #1", 0xA, id="ips: payload cut"
    ),
    pytest.param(
        b"IPS32\x00\x00\x13\x34\x00\x04\x1f", "payload of record #1", 0xB, id="ips32: payload cut"
    ),
    pytest.param(
        b"PATCH" + literal(0x10, b"ab") + b"\x00\x00\x20\x00\x03c",
        "payload of record #2",
        0x11,
        id="payload of the second record cut",
    ),
    pytest.param(
        b"PATCH\x00\x00\x00\xff\xff\x00", "payload of record #1", 0xA, id="largest size, one byte"
    ),
    pytest.param(
        b"PATCH" + literal(0x10, b"abcd")[:-1] + b"EOF",
        "terminator",
        0xE,
        id="payload one byte short swallows the terminator",
    ),
    # --- RLE fields
    pytest.param(b"PATCH\x00\x21\x00\x00\x00", "RLE run length of record #1", 0xA, id="no run"),
    pytest.param(
        b"PATCH\x00\x21\x00\x00\x00\x00", "RLE run length of record #1", 0xA, id="run cut short"
    ),
    pytest.param(
        b"PATCH\x00\x21\x00\x00\x00\x00\x08", "RLE fill byte of record #1", 0xC, id="no fill byte"
    ),
    pytest.param(
        b"IPS32\x00\x00\x21\x00\x00\x00\x00\x08",
        "RLE fill byte of record #1",
        0xD,
        id="ips32: no fill byte",
    ),
    pytest.param(b"PATCH" + rle(0x2100, 0, 0x00) + b"EOF", "run length", 0xA, id="ips: run of 0"),
    pytest.param(
        b"IPS32" + rle(0x2100, 0, 0x00, 4) + b"EEOF", "run length", 0xB, id="ips32: run of 0"
    ),
    pytest.param(b"PATCH\x00\x21\x00\x00\x00\x00\x00", "run length", 0xA, id="run of 0, no fill"),
    # --- after the terminator
    pytest.param(SMALLEST_IPS + b"\x00", "after the terminator", 0x8, id="ips: 1 extra byte"),
    pytest.param(SMALLEST_IPS + b"\x00\x20", "after the terminator", 0x8, id="ips: 2 extra bytes"),
    pytest.param(SMALLEST_IPS + bytes(4), "after the terminator", 0x8, id="ips: 4 extra bytes"),
    pytest.param(TRUNCATING + b"\x00", "after the terminator", 0xE, id="ips: trailer plus 1"),
    pytest.param(V1_IPS + V1_IPS[5:], "after the terminator", 0x19, id="ips: a second body"),
    pytest.param(
        SMALLEST_IPS32 + b"\x00\x00\x20", "after the terminator", 0x9, id="ips32: 3-byte trailer"
    ),
    pytest.param(SMALLEST_IPS32 + b"\x00", "after the terminator", 0x9, id="ips32: 1 extra byte"),
    pytest.param(
        V1_IPS32 + b"\x00\x00\x20", "after the terminator", 0x1C, id="ips32: trailer after records"
    ),
    pytest.param(V1_IPS32 + b"EEOF", "after the terminator", 0x1C, id="ips32: two terminators"),
    # A record that starts at the forbidden offset reads as the terminator, and what
    # follows it as junk: this is what a writer without care would produce.
    pytest.param(COLLISION_WRONG, "after the terminator", 0x8, id="ips: record at 0x454F46"),
    pytest.param(
        b"IPS32" + literal(EEOF_OFFSET, b"\xaa", 4) + b"EEOF",
        "after the terminator",
        0x9,
        id="ips32: record at 0x45454F46",
    ),
]


@pytest.mark.parametrize(("blob", "what", "where"), MALFORMED)
def test_parse_rejects_malformed_files(blob: bytes, what: str, where: int) -> None:
    with pytest.raises(FormatError) as caught:
        ips.parse_ips(blob)
    message = str(caught.value)
    assert isinstance(caught.value, BeaError)
    # The message says what could not be read and names its offset in the patch file.
    assert what in message
    assert re.search(rf"at offset 0x{where:X}\b", message), message


@pytest.mark.parametrize(
    "blob",
    [V1_IPS, V1_IPS32, V1_LITERAL, V1_LITERAL_IPS32, COLLISION_RIGHT, TRUNCATING],
    ids=["v1", "v1-ips32", "v1-literal", "v1-literal-ips32", "collision", "truncating"],
)
def test_every_truncation_of_a_valid_file_is_a_format_error(blob: bytes) -> None:
    # The truncating vector without its last three bytes is a complete patch again.
    complete = {TRUNCATING[:-3]}
    for length in range(len(blob)):
        prefix = blob[:length]
        if prefix in complete:
            assert ips.parse_ips(prefix).truncate is None
            continue
        with pytest.raises(FormatError):
            ips.parse_ips(prefix)


def reference_parse(blob: bytes) -> IpsPatch | None:
    """A second, deliberately naive reader used as an oracle; ``None`` means "malformed".

    It eats the file from the front, slice by slice, exactly in the documented order.
    """
    for flavour in (CLASSIC, WIDE):
        if blob[:5] == flavour.magic:
            break
    else:
        return None
    rest = blob[5:]
    records = []
    while True:
        if len(rest) < flavour.width:
            return None  # the terminator never came
        head, rest = rest[: flavour.width], rest[flavour.width :]
        if head == flavour.terminator:
            break
        if len(rest) < 2:
            return None
        size, rest = rest[0] << 8 | rest[1], rest[2:]
        if size:
            if len(rest) < size:
                return None
            payload, rest = rest[:size], rest[size:]
        else:
            if len(rest) < 3 or rest[:2] == b"\x00\x00":
                return None
            payload, rest = rest[2:3] * (rest[0] << 8 | rest[1]), rest[3:]
        records.append(IpsRecord(int(head.hex(), 16), payload))
    if not rest:
        return IpsPatch(flavour.fmt, tuple(records), None)
    if flavour is CLASSIC and len(rest) == 3:
        return IpsPatch(flavour.fmt, tuple(records), int(rest.hex(), 16))
    return None


def test_damaged_files_are_read_like_the_reference_reader_or_rejected() -> None:
    # The oracle itself is held to the golden vectors.
    v1_records = tuple(IpsRecord(offset, data) for offset, data in V1_CHANGES)
    assert reference_parse(V1_IPS) == IpsPatch(IpsFormat.IPS, v1_records)
    assert reference_parse(V1_IPS32) == IpsPatch(IpsFormat.IPS32, v1_records)
    assert reference_parse(TRUNCATING) == IpsPatch(IpsFormat.IPS, (IpsRecord(0x10, b"\xff"),), 0x20)
    assert reference_parse(COLLISION_WRONG) is None

    rng = random.Random(0x1B5)
    seeds = [
        V1_IPS,
        V1_IPS32,
        V1_LITERAL,
        V1_LITERAL_IPS32,
        TRUNCATING,
        COLLISION_RIGHT,
        SMALLEST_IPS,
        SMALLEST_IPS32,
        b"PATCH" + rle(0x10, 3, 0x7E) + literal(0x20, b"EOF") + rle(0x30, 300, 0x00) + b"EOF",
        b"IPS32" + rle(0x45454F, 2, 0x46, 4) + literal(0x454F46, b"EEOF", 4) + b"EEOF",
    ]
    accepted = 0
    rounds = 20000
    for _ in range(rounds):
        blob = bytearray(rng.choice(seeds))
        for _ in range(rng.randrange(1, 4)):
            # Mostly the body: there is little to learn from one more damaged magic.
            position = rng.randrange(0 if rng.random() < 0.05 else 5, len(blob))
            match rng.randrange(3):
                case 0:
                    blob[position] = rng.randrange(256)
                case 1:
                    blob.insert(position, rng.randrange(256))
                case _:
                    del blob[position]
        damaged = bytes(blob)
        try:
            patch = ips.parse_ips(damaged)
        except FormatError:  # anything else that escapes fails the test
            patch = None
        assert patch == reference_parse(damaged), damaged.hex(" ")
        accepted += patch is not None
    # Both outcomes are well represented.
    assert rounds // 20 < accepted < rounds - rounds // 20


@pytest.mark.parametrize("value", ["PATCHEOF", None, 8, 1.5, [0x50, 0x41]])
def test_parse_rejects_what_is_not_bytes(value: object) -> None:
    with pytest.raises(TypeError):
        ips.parse_ips(value)  # type: ignore[arg-type]


def test_rle_expansion_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = (
        b"PATCH" + rle(0x000, 60, 0xAA) + literal(0x100, bytes(500)) + rle(0x400, 40, 0xBB) + b"EOF"
    )
    monkeypatch.setattr(ips, "MAX_RLE_EXPANSION", 100)
    # Exactly at the limit, and literal data does not count: the file pays for it.
    assert [len(record.data) for record in ips.parse_ips(blob).records] == [60, 500, 40]
    monkeypatch.setattr(ips, "MAX_RLE_EXPANSION", 99)
    with pytest.raises(FormatError, match="expand") as caught:
        ips.parse_ips(blob)
    # The run length that crosses the limit is the one of the third record.
    assert "record #3" in str(caught.value)
    assert "0x20B" in str(caught.value)  # 5 + 8 + (5 + 500) + 3 + 2 = 523


def test_rle_bomb_is_refused_with_the_default_limit() -> None:
    # Under 10 KiB of file that would expand to more than 64 MiB.
    assert ips.MAX_RLE_EXPANSION == 64 * 1024 * 1024
    count = 1100
    assert count * FULL > ips.MAX_RLE_EXPANSION
    bomb = b"PATCH" + rle(0, FULL, 0xEE) * count + b"EOF"
    assert len(bomb) < 10 * 1024
    with pytest.raises(FormatError, match="expand"):
        ips.parse_ips(bomb)
    with pytest.raises(FormatError, match="expand"):
        ips.apply_ips(bytes(16), bomb)
    # The limit leaves room for everything a classic patch can address (16 MiB and a bit).
    assert ips.MAX_RLE_EXPANSION >= 0xFFFFFF + FULL


# ----------------------------------------------------------------------------------
# build_ips: plain cases
# ----------------------------------------------------------------------------------


def test_build_writes_literal_records_for_the_golden_changes() -> None:
    assert len(V1_LITERAL) == 30
    assert ips.build_ips(V1_CHANGES) == V1_LITERAL
    # RLE is never written, although the second change is a run of zeros.
    assert ips.build_ips(V1_CHANGES) != V1_IPS
    assert ips.parse_ips(ips.build_ips(V1_CHANGES)).records == ips.parse_ips(V1_IPS).records


def test_build_accepts_records_pairs_and_any_iterable() -> None:
    records = [IpsRecord(offset, data) for offset, data in V1_CHANGES]
    assert ips.build_ips(records) == V1_LITERAL
    assert ips.build_ips(tuple(V1_CHANGES)) == V1_LITERAL
    assert ips.build_ips(iter(V1_CHANGES)) == V1_LITERAL
    assert ips.build_ips(change for change in V1_CHANGES) == V1_LITERAL
    assert ips.build_ips([records[1], V1_CHANGES[0]]) == V1_LITERAL
    assert ips.build_ips(ips.parse_ips(V1_IPS).records) == V1_LITERAL
    mutable = [(0x1334, bytearray.fromhex("1F2003D5")), (0x2100, memoryview(bytes(8)))]
    assert ips.build_ips(mutable) == V1_LITERAL  # type: ignore[arg-type]
    assert type(ips.build_ips(V1_CHANGES)) is bytes


def test_build_without_records_gives_the_smallest_file() -> None:
    assert ips.build_ips([]) == SMALLEST_IPS
    assert ips.build_ips(iter(())) == SMALLEST_IPS
    assert ips.build_ips([], fmt=IpsFormat.IPS) == SMALLEST_IPS
    assert ips.build_ips([], fmt=IpsFormat.IPS32) == SMALLEST_IPS32
    assert ips.build_ips([], original_byte=must_not_be_called) == SMALLEST_IPS


def test_build_never_writes_rle_or_a_truncation_length() -> None:
    blob = ips.build_ips([(0x2100, bytes(1000))])
    assert blob == b"PATCH" + bytes.fromhex("002100 03E8") + bytes(1000) + b"EOF"
    assert read_back(blob) == [(0x2100, bytes(1000))]


def test_build_sorts_records_by_offset() -> None:
    changes = [(0x300, b"C"), (0x100, b"A"), (0x200, b"B")]
    expected = vector(
        "50 41 54 43 48 | 00 01 00 | 00 01 | 41 | 00 02 00 | 00 01 | 42 "
        "| 00 03 00 | 00 01 | 43 | 45 4F 46"
    )
    for order in itertools.permutations(changes):
        assert ips.build_ips(order) == expected


def test_build_writes_offset_zero_and_single_bytes() -> None:
    assert ips.build_ips([(0, b"\x7f")]) == vector(
        "50 41 54 43 48 | 00 00 00 | 00 01 | 7F | 45 4F 46"
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param([(0x10, b"abcd"), (0x12, b"x")], id="inside"),
        pytest.param([(0x10, b"abcd"), (0x13, b"xy")], id="last byte shared"),
        pytest.param([(0x10, b"abcd"), (0x10, b"x")], id="same start"),
        pytest.param([(0x10, b"abcd"), (0x0E, b"xyz")], id="given out of order"),
        pytest.param([(0x10, b"a"), (0x10, b"a")], id="the same record twice"),
        pytest.param([(0x10, b"ab"), (0x20, b"cd"), (0x11, bytes(0x10))], id="covers two others"),
        pytest.param([(0x10, bytes(FULL + 10)), (0x10 + FULL + 9, b"x")], id="tail of long data"),
    ],
)
def test_build_rejects_overlapping_records(changes: list[tuple[int, bytes]]) -> None:
    with pytest.raises(ValueError, match="overlap"):
        ips.build_ips(changes)
    with pytest.raises(ValueError, match="overlap"):
        ips.build_ips(changes, fmt=IpsFormat.IPS32)


def test_build_keeps_touching_records_separate() -> None:
    blob = ips.build_ips([(0x14, b"ef"), (0x10, b"abcd"), (0x16, b"g")])
    assert blob == vector(
        "50 41 54 43 48 | 00 00 10 | 00 04 | 61 62 63 64 | 00 00 14 | 00 02 | 65 66 "
        "| 00 00 16 | 00 01 | 67 | 45 4F 46"
    )


def test_build_never_merges_across_unchanged_bytes() -> None:
    # One unchanged byte lies between the two changes. A single record over both would
    # have to carry it: a byte of the target inside the patch.
    blob = ips.build_ips([(0x10, b"ab"), (0x13, b"cd")])
    assert blob == vector(
        "50 41 54 43 48 | 00 00 10 | 00 02 | 61 62 | 00 00 13 | 00 02 | 63 64 | 45 4F 46"
    )


def test_build_splits_long_data_at_exact_boundaries() -> None:
    data = b"\x01" * FULL + b"\x02" * FULL + b"\x03" * 5
    blob = ips.build_ips([(0x100, data)])
    assert blob == (
        b"PATCH"
        + bytes.fromhex("000100 FFFF")
        + b"\x01" * FULL
        + bytes.fromhex("0100FF FFFF")
        + b"\x02" * FULL
        + bytes.fromhex("0200FE 0005")
        + b"\x03" * 5
        + b"EOF"
    )


@pytest.mark.parametrize(
    ("length", "sizes"),
    [
        (FULL - 1, [FULL - 1]),
        (FULL, [FULL]),
        (FULL + 1, [FULL, 1]),
        (2 * FULL, [FULL, FULL]),
        (2 * FULL + 1, [FULL, FULL, 1]),
        (3 * FULL + 5, [FULL, FULL, FULL, 5]),
    ],
)
def test_build_splits_only_data_longer_than_one_record(length: int, sizes: list[int]) -> None:
    data = random.Random(length).randbytes(length)
    for flavour in (CLASSIC, WIDE):
        blob = ips.build_ips([(0x1000, data)], fmt=flavour.fmt)
        records = read_back(blob)
        assert [len(payload) for _offset, payload in records] == sizes
        # The pieces follow each other without a gap and add up to the data.
        assert [offset for offset, _payload in records] == [
            0x1000 + sum(sizes[:index]) for index in range(len(sizes))
        ]
        assert b"".join(payload for _offset, payload in records) == data
        assert blob == patch_file(flavour, records)


def test_build_splits_each_record_on_its_own() -> None:
    first = random.Random(1).randbytes(FULL + 2)
    second = random.Random(2).randbytes(3)
    blob = ips.build_ips([(0x20000, second), (0x10, first)])
    assert blob == patch_file(
        CLASSIC, [(0x10, first[:FULL]), (0x10 + FULL, first[FULL:]), (0x20000, second)]
    )


# ----------------------------------------------------------------------------------
# build_ips: choosing the format
# ----------------------------------------------------------------------------------


def test_build_stays_classic_up_to_the_last_24_bit_offset() -> None:
    assert ips.build_ips([(0xFFFFFF, b"\xaa")]) == vector(
        "50 41 54 43 48 | FF FF FF | 00 01 | AA | 45 4F 46"
    )
    # Only where a record starts counts: it may run on past the last 24-bit offset.
    data = random.Random(3).randbytes(FULL)
    assert ips.build_ips([(0xFFFFFF, data)]) == patch_file(CLASSIC, [(0xFFFFFF, data)])
    assert ips.build_ips([(0x10, b"\x01"), (0xFFFFFF, b"\xaa")]) == vector(
        "50 41 54 43 48 | 00 00 10 | 00 01 | 01 | FF FF FF | 00 01 | AA | 45 4F 46"
    )


def test_build_switches_to_ips32_at_the_first_offset_that_needs_it() -> None:
    assert ips.build_ips([(0x1000000, b"\xaa")]) == vector(
        "49 50 53 33 32 | 01 00 00 00 | 00 01 | AA | 45 45 4F 46"
    )
    # One far record makes the whole file IPS32: every offset is four bytes wide.
    assert ips.build_ips([(0x1000000, b"\xbb"), (0x10, b"\xaa")]) == vector(
        "49 50 53 33 32 | 00 00 00 10 | 00 01 | AA | 01 00 00 00 | 00 01 | BB | 45 45 4F 46"
    )


def test_build_counts_the_records_it_writes_not_the_ones_it_is_given() -> None:
    # The change starts below the limit, but its second piece cannot.
    data = random.Random(4).randbytes(FULL + 1)
    blob = ips.build_ips([(0xFFFFFE, data)])
    assert blob == patch_file(WIDE, [(0xFFFFFE, data[:FULL]), (0x100FFFD, data[FULL:])])
    with pytest.raises(ValueError, match="0x100FFFD"):
        ips.build_ips([(0xFFFFFE, data)], fmt=IpsFormat.IPS)


def test_build_can_be_forced_to_either_format() -> None:
    assert ips.build_ips(V1_CHANGES, fmt=IpsFormat.IPS32) == V1_LITERAL_IPS32
    assert len(V1_LITERAL_IPS32) == 33
    assert ips.build_ips(V1_CHANGES, fmt=IpsFormat.IPS) == V1_LITERAL
    assert ips.build_ips([(0xFFFFFF, b"\xaa")], fmt=IpsFormat.IPS32) == vector(
        "49 50 53 33 32 | 00 FF FF FF | 00 01 | AA | 45 45 4F 46"
    )
    # The value of a member is accepted in its place.
    assert ips.build_ips(V1_CHANGES, fmt="ips32") == V1_LITERAL_IPS32  # type: ignore[arg-type]


def test_build_refuses_to_force_classic_ips_beyond_its_offsets() -> None:
    with pytest.raises(ValueError, match="0x1000000"):
        ips.build_ips([(0x1000000, b"\xaa")], fmt=IpsFormat.IPS)
    with pytest.raises(ValueError, match="0xFFFFFF"):
        ips.build_ips([(0x10, b"\x01"), (0x12345678, b"\xaa")], fmt=IpsFormat.IPS)


def test_build_ips32_ends_where_32_bits_end() -> None:
    assert ips.build_ips([(0xFFFFFFFF, b"\xaa")]) == vector(
        "49 50 53 33 32 | FF FF FF FF | 00 01 | AA | 45 45 4F 46"
    )
    assert ips.build_ips([(0xFFFFFFFE, b"\xaa\xbb")]) == vector(
        "49 50 53 33 32 | FF FF FF FE | 00 02 | AA BB | 45 45 4F 46"
    )
    for fmt in (None, IpsFormat.IPS32):
        # Starts inside the range but would end one byte past it.
        with pytest.raises(ValueError, match="0x100000001"):
            ips.build_ips([(0xFFFFFFFF, b"\xaa\xbb")], fmt=fmt)
        # Cannot even start.
        with pytest.raises(ValueError, match="0x100000000"):
            ips.build_ips([(0x100000000, b"\xaa")], fmt=fmt)
        with pytest.raises(ValueError):
            ips.build_ips([(1 << 40, b"\xaa")], fmt=fmt)
    with pytest.raises(ValueError):
        ips.build_ips([(0x100000000, b"\xaa")], fmt=IpsFormat.IPS)


# ----------------------------------------------------------------------------------
# build_ips: bad arguments
# ----------------------------------------------------------------------------------


def test_build_rejects_empty_data() -> None:
    with pytest.raises(ValueError, match="empty"):
        ips.build_ips([(0x10, b"")])
    with pytest.raises(ValueError, match="empty"):
        ips.build_ips([(0x10, b"ab"), IpsRecord(0x20, b"")])
    with pytest.raises(ValueError, match="empty"):
        ips.build_ips([(0x10, bytearray())])  # type: ignore[list-item]


def test_build_rejects_negative_offsets() -> None:
    with pytest.raises(ValueError, match="negative"):
        ips.build_ips([(-1, b"ab")])
    with pytest.raises(ValueError, match="negative"):
        ips.build_ips([IpsRecord(0x10, b"ab"), IpsRecord(-0x100, b"cd")])


@pytest.mark.parametrize(
    "records",
    [
        pytest.param([(0x10, "text")], id="text as data"),
        pytest.param([(0x10, 4)], id="a count as data"),
        pytest.param([(0x10, None)], id="no data"),
        pytest.param([(0x10, [0x1F, 0x20])], id="a list of ints as data"),
        pytest.param([("0x10", b"ab")], id="text as offset"),
        pytest.param([(1.5, b"ab")], id="float offset"),
        pytest.param([(True, b"ab")], id="bool offset"),
        pytest.param([(None, b"ab")], id="no offset"),
        pytest.param([(0x10, b"ab", b"cd")], id="three items"),
        pytest.param([(0x10,)], id="one item"),
        pytest.param([[0x10, b"ab"]], id="a list instead of a pair"),
        pytest.param([b"ab"], id="bare bytes"),
        pytest.param([None], id="None"),
        pytest.param((0x10, b"ab"), id="one pair instead of a list of pairs"),
        pytest.param(b"ab", id="bytes instead of records"),
    ],
)
def test_build_rejects_records_of_the_wrong_shape(records: object) -> None:
    with pytest.raises(TypeError):
        ips.build_ips(records)  # type: ignore[arg-type]


def test_build_rejects_an_unknown_format() -> None:
    with pytest.raises(ValueError):
        ips.build_ips([], fmt="bps")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# build_ips: the offset that reads as the terminator
# ----------------------------------------------------------------------------------


def test_build_golden_collision_vector() -> None:
    asked: list[int] = []

    def original_byte(offset: int) -> int:
        asked.append(offset)
        return 0x11

    blob = ips.build_ips([(0x454F46, b"\xaa")], original_byte=original_byte)
    assert blob == COLLISION_RIGHT
    assert blob != COLLISION_WRONG
    assert asked == [0x454F45]
    # What the wrong file would have meant to a reader: an empty patch followed by junk.
    with pytest.raises(FormatError, match="after the terminator"):
        ips.parse_ips(COLLISION_WRONG)
    assert ips.parse_ips(blob).records == (IpsRecord(0x454F45, b"\x11\xaa"),)


@BOTH_FLAVOURS
def test_record_at_the_forbidden_offset_starts_one_byte_earlier(flavour: Flavour) -> None:
    target = around(flavour, seed=1)
    changes = [(flavour.forbidden, b"\xaa")]
    blob = ips.build_ips(changes, original_byte=target.original_byte)
    kept = bytes([target.at(flavour.forbidden - 1)])
    assert blob == patch_file(flavour, [(flavour.forbidden - 1, kept + b"\xaa")])
    assert target.asked == [flavour.forbidden - 1]
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_record_at_the_forbidden_offset_needs_the_original_byte(flavour: Flavour) -> None:
    with pytest.raises(ValueError, match="original_byte") as caught:
        ips.build_ips([(flavour.forbidden, b"\xaa")])
    # The message says which offset is the problem and which byte is needed.
    assert f"0x{flavour.forbidden:X}" in str(caught.value)
    assert f"0x{flavour.forbidden - 1:X}" in str(caught.value)
    # Other records do not help unless one of them ends exactly there.
    with pytest.raises(ValueError, match="original_byte"):
        ips.build_ips([(flavour.forbidden - 3, b"\x01\x02"), (flavour.forbidden, b"\xaa")])
    with pytest.raises(ValueError, match="original_byte"):
        ips.build_ips([(flavour.forbidden, b"\xaa"), (flavour.forbidden + 1, b"\xbb")])


@BOTH_FLAVOURS
def test_record_at_the_forbidden_offset_continues_the_record_that_ends_there(
    flavour: Flavour,
) -> None:
    target = around(flavour, seed=2)
    changes = [(flavour.forbidden, b"\xaa\xbb"), (flavour.forbidden - 2, b"\x01\x02")]
    expected = patch_file(flavour, [(flavour.forbidden - 2, b"\x01\x02\xaa\xbb")])
    # No byte of the target is needed: with or without a callback, it is not asked.
    assert ips.build_ips(changes) == expected
    assert ips.build_ips(changes, original_byte=must_not_be_called) == expected
    assert_means(expected, flavour, target, changes)

    # Only the pair that meets at the forbidden offset is joined.
    changes = [
        (flavour.forbidden - 6, b"\x0a\x0b"),
        (flavour.forbidden - 4, b"\x01\x02\x03\x04"),
        (flavour.forbidden, b"\xaa"),
        (flavour.forbidden + 1, b"\xbb"),
    ]
    blob = ips.build_ips(changes, original_byte=must_not_be_called)
    assert blob == patch_file(
        flavour,
        [
            (flavour.forbidden - 6, b"\x0a\x0b"),
            (flavour.forbidden - 4, b"\x01\x02\x03\x04\xaa"),
            (flavour.forbidden + 1, b"\xbb"),
        ],
    )
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_record_that_ends_one_byte_before_the_forbidden_offset_is_not_joined(
    flavour: Flavour,
) -> None:
    # The byte in between is not being changed: joining would be merging across it.
    target = around(flavour, seed=3)
    changes = [(flavour.forbidden - 5, b"\x01\x02\x03\x04"), (flavour.forbidden, b"\xaa")]
    blob = ips.build_ips(changes, original_byte=target.original_byte)
    kept = bytes([target.at(flavour.forbidden - 1)])
    assert blob == patch_file(
        flavour,
        [(flavour.forbidden - 5, b"\x01\x02\x03\x04"), (flavour.forbidden - 1, kept + b"\xaa")],
    )
    assert target.asked == [flavour.forbidden - 1]
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_records_near_the_forbidden_offset_that_need_no_care(flavour: Flavour) -> None:
    target = around(flavour, seed=4)
    spot = flavour.forbidden
    for changes in (
        [(spot - 4, b"abcd")],  # ends exactly at the forbidden offset
        [(spot - 4, b"abcdefgh")],  # runs across it
        [(spot - 1, b"abcd")],  # starts just before it
        [(spot + 1, b"abcd")],  # starts just after it
        [(spot - 4, b"abcd"), (spot + 1, b"efgh")],  # leaves only that one byte alone
    ):
        blob = ips.build_ips(changes, original_byte=must_not_be_called)
        assert blob == patch_file(flavour, changes)
        assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_long_data_is_not_split_at_the_forbidden_offset(flavour: Flavour) -> None:
    spot = flavour.forbidden
    target = around(flavour, seed=5, reach=0x30000)
    # Cut into full records, the second piece would start exactly at the forbidden offset.
    data = random.Random(6).randbytes(FULL + 1)
    changes = [(spot - FULL, data)]
    blob = ips.build_ips(changes)  # the byte before is part of the data: nothing to ask
    assert blob == patch_file(
        flavour, [(spot - FULL, data[: FULL - 1]), (spot - 1, data[FULL - 1 :])]
    )
    assert_means(blob, flavour, target, changes)

    # The same with the third piece, and more data after it.
    data = random.Random(7).randbytes(3 * FULL)
    changes = [(spot - 2 * FULL, data)]
    blob = ips.build_ips(changes, original_byte=must_not_be_called)
    assert blob == patch_file(
        flavour,
        [
            (spot - 2 * FULL, data[:FULL]),
            (spot - FULL, data[FULL : 2 * FULL - 1]),
            (spot - 1, data[2 * FULL - 1 : 3 * FULL - 1]),
            (spot - 1 + FULL, data[3 * FULL - 1 :]),
        ],
    )
    assert_means(blob, flavour, target, changes)

    # Data that merely ends there, in full records, is left as it is.
    data = random.Random(8).randbytes(2 * FULL)
    changes = [(spot - 2 * FULL, data)]
    blob = ips.build_ips(changes, original_byte=must_not_be_called)
    assert blob == patch_file(flavour, [(spot - 2 * FULL, data[:FULL]), (spot - FULL, data[FULL:])])
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_joined_records_that_exceed_one_record_are_split_elsewhere(flavour: Flavour) -> None:
    spot = flavour.forbidden
    target = around(flavour, seed=9, reach=0x30000)
    # A full record ends at the forbidden offset: what follows cannot be appended to it,
    # and cannot start a record either. The boundary moves one byte back.
    head = random.Random(10).randbytes(FULL)
    tail = b"\xaa\xbb\xcc"
    changes = [(spot - FULL, head), (spot, tail)]
    blob = ips.build_ips(changes)
    assert blob == patch_file(
        flavour, [(spot - FULL, head[: FULL - 1]), (spot - 1, head[FULL - 1 :] + tail)]
    )
    assert_means(blob, flavour, target, changes)

    # A short record ends there and a full one follows: one record of 0xFFFF bytes, then
    # the rest, split at an ordinary place.
    head = random.Random(11).randbytes(10)
    tail = random.Random(12).randbytes(FULL)
    changes = [(spot, tail), (spot - 10, head)]
    blob = ips.build_ips(changes, original_byte=must_not_be_called)
    joined = head + tail
    assert blob == patch_file(
        flavour, [(spot - 10, joined[:FULL]), (spot - 10 + FULL, joined[FULL:])]
    )
    assert_means(blob, flavour, target, changes)

    # The record before it was itself split: only its last piece is continued.
    head = random.Random(13).randbytes(FULL + 7)
    changes = [(spot - FULL - 7, head), (spot, tail)]
    blob = ips.build_ips(changes)
    joined = head + tail
    assert blob == patch_file(
        flavour,
        [
            (spot - FULL - 7, joined[:FULL]),
            (spot - 7, joined[FULL : 2 * FULL]),
            (spot - 7 + FULL, joined[2 * FULL :]),
        ],
    )
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
def test_long_record_at_the_forbidden_offset(flavour: Flavour) -> None:
    spot = flavour.forbidden
    target = around(flavour, seed=14, reach=0x30000)
    # 0xFFFF bytes of data plus the borrowed byte no longer fit one record.
    data = random.Random(15).randbytes(FULL)
    changes = [(spot, data)]
    blob = ips.build_ips(changes, original_byte=target.original_byte)
    whole = bytes([target.at(spot - 1)]) + data
    assert blob == patch_file(flavour, [(spot - 1, whole[:FULL]), (spot - 1 + FULL, whole[FULL:])])
    assert target.asked == [spot - 1]
    assert_means(blob, flavour, target, changes)


@BOTH_FLAVOURS
@pytest.mark.parametrize("seed", range(30))
def test_random_changes_around_the_forbidden_offset(flavour: Flavour, seed: int) -> None:
    rng = random.Random(seed)
    spot = flavour.forbidden
    reach = 0x28000
    target = around(flavour, seed=1000 + seed, reach=reach)
    # Boundaries that make records start, end or get split at the forbidden offset,
    # mixed with random ones. Each stretch between two boundaries is changed or left.
    special = [spot + delta for delta in (-2 * FULL, -FULL - 1, -FULL, -FULL + 1, -2, -1)]
    special += [spot + delta for delta in (0, 1, 2, FULL - 1, FULL, FULL + 1)]
    cuts = set(rng.sample(special, rng.randrange(2, len(special) + 1)))
    cuts.update(spot + rng.randrange(-reach + 1, reach) for _ in range(rng.randrange(0, 6)))
    stretches = list(itertools.pairwise(sorted(cuts)))
    kept = [stretch for stretch in stretches if rng.random() < 0.7] or [rng.choice(stretches)]
    changes = [(low, rng.randbytes(high - low)) for low, high in kept]
    rng.shuffle(changes)

    blob = ips.build_ips(changes, original_byte=target.original_byte)
    assert_means(blob, flavour, target, changes)

    # A byte of the target is asked for in one situation only, and at most once.
    starts_there = any(offset == spot for offset, _data in changes)
    ends_there = any(offset + len(data) == spot for offset, data in changes)
    borrows = starts_there and not ends_there
    assert target.asked == ([spot - 1] if borrows else [])

    # The patch writes the changed bytes and nothing else, except for that one byte.
    def coverage(records: Iterable[tuple[int, bytes]]) -> bytearray:
        mask = bytearray(len(target.data))
        for offset, payload in records:
            mask[offset - target.base : offset - target.base + len(payload)] = b"\x01" * len(
                payload
            )
        return mask

    wanted = coverage(changes)
    if borrows:
        wanted[spot - 1 - target.base] = 1
    assert coverage(read_back(blob)) == wanted


def test_classic_forbidden_offset_is_an_ordinary_offset_in_ips32() -> None:
    blob = ips.build_ips([(EOF_OFFSET, b"\xaa")], fmt=IpsFormat.IPS32)
    assert blob == vector("49 50 53 33 32 | 00 45 4F 46 | 00 01 | AA | 45 45 4F 46")
    assert ips.parse_ips(blob).records == (IpsRecord(EOF_OFFSET, b"\xaa"),)


def test_format_is_chosen_before_the_forbidden_offset_is_dealt_with() -> None:
    # The far record makes this an IPS32 file, in which 0x454F46 needs no care at all:
    # no callback may be demanded for it.
    blob = ips.build_ips([(EOF_OFFSET, b"\xaa"), (0x1000000, b"\xbb")])
    assert blob == vector(
        "49 50 53 33 32 | 00 45 4F 46 | 00 01 | AA | 01 00 00 00 | 00 01 | BB | 45 45 4F 46"
    )
    assert (
        ips.build_ips(
            [(EOF_OFFSET, b"\xaa"), (0x1000000, b"\xbb")], original_byte=must_not_be_called
        )
        == blob
    )
    # Forced into classic IPS, the far record is what fails.
    with pytest.raises(ValueError, match="0x1000000"):
        ips.build_ips([(EOF_OFFSET, b"\xaa"), (0x1000000, b"\xbb")], fmt=IpsFormat.IPS)


def test_borrowed_byte_can_push_a_patch_out_of_classic_ips() -> None:
    # 187 full records starting at the forbidden offset: laid out naively, the last one
    # would still start below 0xFFFFFF. But classic IPS cannot start at 0x454F46; with
    # the borrowed byte in front, a 188th record appears and starts beyond the limit.
    # The file has to become IPS32, where the offset is ordinary and no byte is needed.
    count = 187
    beyond = EOF_OFFSET - 1 + count * FULL  # where that 188th record would start
    assert EOF_OFFSET + (count - 1) * FULL <= 0xFFFFFF < beyond
    data = random.Random(16).randbytes(count * FULL)
    blob = ips.build_ips([(EOF_OFFSET, data)])
    assert blob == patch_file(
        WIDE,
        [
            (EOF_OFFSET + index * FULL, data[index * FULL : (index + 1) * FULL])
            for index in range(count)
        ],
    )
    # Forced into classic IPS it does not fit, callback or not.
    with pytest.raises(ValueError, match=f"0x{beyond:X}"):
        ips.build_ips([(EOF_OFFSET, data)], fmt=IpsFormat.IPS, original_byte=lambda offset: 0)
    with pytest.raises(ValueError, match=f"0x{beyond:X}"):
        ips.build_ips([(EOF_OFFSET, data)], fmt=IpsFormat.IPS)


@pytest.mark.parametrize("value", [256, -1, 0x1100, -256])
def test_original_byte_must_return_a_byte_value(value: int) -> None:
    with pytest.raises(ValueError, match="original_byte"):
        ips.build_ips([(EOF_OFFSET, b"\xaa")], original_byte=lambda offset: value)


@pytest.mark.parametrize("value", [b"\x11", "11", None, 17.0, True, [0x11]])
def test_original_byte_must_return_an_integer(value: object) -> None:
    with pytest.raises(TypeError, match="original_byte"):
        ips.build_ips([(EOF_OFFSET, b"\xaa")], original_byte=lambda offset: value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, 0x11, 255])
def test_original_byte_values_at_both_ends_of_the_range(value: int) -> None:
    blob = ips.build_ips([(EOF_OFFSET, b"\xaa")], original_byte=lambda offset: value)
    assert blob == b"PATCH" + bytes.fromhex("454F45 0002") + bytes([value, 0xAA]) + b"EOF"


def test_original_byte_errors_are_not_swallowed() -> None:
    def failing(offset: int) -> int:
        raise LookupError(f"nothing at 0x{offset:X}")

    with pytest.raises(LookupError, match="0x454F45"):
        ips.build_ips([(EOF_OFFSET, b"\xaa")], original_byte=failing)


# ----------------------------------------------------------------------------------
# Payloads that contain the terminator
# ----------------------------------------------------------------------------------


@BOTH_FLAVOURS
def test_payloads_may_contain_the_terminator(flavour: Flavour) -> None:
    terminator = flavour.terminator
    changes = [
        (0x100, terminator),
        (0x200, terminator + b"xx"),
        (0x300, b"xx" + terminator),
        (0x400, terminator * 4),
        (0x500, b"PATCH" + terminator + b"IPS32"),
        (0x600, terminator[:-1]),
        (0x700, terminator),  # the last payload, right before the real terminator
    ]
    blob = ips.build_ips(changes, fmt=flavour.fmt)
    assert blob == patch_file(flavour, changes)
    assert blob.count(terminator) > len(changes)
    assert read_back(blob) == changes
    assert ips.parse_ips(blob).records == tuple(IpsRecord(*change) for change in changes)
    target = Target(0, 0x800, seed=17)
    assert target.patched(blob) == target.edited(changes)


def test_offset_and_size_fields_may_spell_the_terminator() -> None:
    # 00 00 45 | 4F 46: "EOF" across the offset and the size of one record.
    data = random.Random(18).randbytes(0x4F46)
    blob = ips.build_ips([(0x45, data)])
    assert blob == b"PATCH" + bytes.fromhex("000045 4F46") + data + b"EOF"
    assert ips.parse_ips(blob).records == (IpsRecord(0x45, data),)
    # 00 00 45 45 | 4F 46: "EEOF" in the same place of an IPS32 record.
    blob = ips.build_ips([(0x4545, data)], fmt=IpsFormat.IPS32)
    assert blob == b"IPS32" + bytes.fromhex("00004545 4F46") + data + b"EEOF"
    assert ips.parse_ips(blob).records == (IpsRecord(0x4545, data),)


# ----------------------------------------------------------------------------------
# Round trips: build -> parse -> apply against a direct edit
# ----------------------------------------------------------------------------------


def random_changes(rng: random.Random, start: int, end: int) -> list[tuple[int, bytes]]:
    """Pick changes inside ``[start, end)`` that do not overlap; some of them touch."""
    changes = []
    position = start
    while True:
        position += rng.choice([0, 0, 1, 2, rng.randrange(1, 300)])
        length = rng.randrange(1, 64)
        if position + length > end:
            return changes
        changes.append((position, rng.randbytes(length)))
        position += length


@pytest.mark.parametrize("seed", range(40))
def test_round_trip_matches_a_direct_edit(seed: int) -> None:
    rng = random.Random(seed)
    target = Target(0, rng.randrange(1, 6000), seed=2000 + seed)
    changes = random_changes(rng, 0, len(target.data))
    expected = target.edited(changes)
    shuffled = rng.sample(changes, len(changes))

    for flavour, fmt in ((CLASSIC, None), (CLASSIC, IpsFormat.IPS), (WIDE, IpsFormat.IPS32)):
        blob = ips.build_ips(shuffled, fmt=fmt, original_byte=must_not_be_called)
        # One literal record per change, sorted, nothing else.
        assert blob == patch_file(flavour, changes)
        patch = ips.parse_ips(blob)
        assert patch == IpsPatch(flavour.fmt, tuple(IpsRecord(*change) for change in changes))
        assert ips.apply_ips(target.data, patch) == expected
        assert ips.apply_ips(bytearray(target.data), blob) == expected


@pytest.mark.parametrize("seed", range(20))
def test_round_trip_at_high_offsets(seed: int) -> None:
    rng = random.Random(seed)
    # A window of target bytes high in the 32-bit range: across the edge where classic
    # IPS ends, right after it, across the sign bit, and up to the very last offset.
    base = [0xFFFFFF - 0x800, 0x1000000, 0x7FFFF800, 0xFFFFF000][seed % 4]
    target = Target(base, 0x1000, seed=3000 + seed)
    changes = random_changes(rng, base, base + len(target.data))
    blob = ips.build_ips(rng.sample(changes, len(changes)))
    needs_wide = any(offset > 0xFFFFFF for offset, _data in changes)
    flavour = WIDE if needs_wide else CLASSIC
    assert blob == patch_file(flavour, changes)
    assert_means(blob, flavour, target, changes)


@pytest.mark.parametrize("seed", range(6))
def test_round_trip_with_long_records(seed: int) -> None:
    rng = random.Random(seed)
    target = Target(0x1000, 0x80000, seed=4000 + seed)
    changes = []
    position = target.base
    while True:
        position += rng.choice([0, 1, rng.randrange(1, 0x4000)])
        length = rng.choice([1, FULL - 1, FULL, FULL + 1, rng.randrange(1, 3 * FULL)])
        if position + length > target.base + len(target.data):
            break
        changes.append((position, rng.randbytes(length)))
        position += length
    blob = ips.build_ips(changes, original_byte=must_not_be_called)
    assert_means(blob, CLASSIC, target, changes)
    # Every change comes back as full records followed by the remainder.
    expected = [
        (offset + start, data[start : start + FULL])
        for offset, data in changes
        for start in range(0, len(data), FULL)
    ]
    assert read_back(blob) == expected


# ----------------------------------------------------------------------------------
# apply_ips
# ----------------------------------------------------------------------------------


def test_apply_takes_a_patch_or_the_bytes_of_one() -> None:
    target = bytes(range(256)) * 0x30
    expected = edit(target, V1_CHANGES)
    assert expected != target
    for patch in (V1_IPS, V1_LITERAL, V1_IPS32, ips.parse_ips(V1_IPS), ips.parse_ips(V1_IPS32)):
        assert ips.apply_ips(target, patch) == expected
    assert ips.apply_ips(target, bytearray(V1_IPS)) == expected  # type: ignore[arg-type]
    assert ips.apply_ips(memoryview(target), memoryview(V1_IPS)) == expected  # type: ignore[arg-type]
    assert type(ips.apply_ips(bytearray(target), V1_IPS)) is bytes


def test_apply_leaves_the_target_untouched() -> None:
    target = bytearray(range(256)) * 0x30
    before = bytes(target)
    patched = ips.apply_ips(target, V1_IPS)
    assert patched != before
    assert bytes(target) == before
    # ... also when it fails half-way: the first record fits, the second does not.
    short = bytearray(0x2000)
    with pytest.raises(ValueError):
        ips.apply_ips(short, V1_IPS)
    assert short == bytearray(0x2000)


def test_apply_with_an_empty_patch_copies_the_target() -> None:
    target = bytes(range(32))
    assert ips.apply_ips(target, SMALLEST_IPS) == target
    assert ips.apply_ips(target, SMALLEST_IPS32) == target
    assert ips.apply_ips(b"", SMALLEST_IPS) == b""
    assert ips.apply_ips(target, IpsPatch(IpsFormat.IPS, ())) == target


def test_apply_writes_records_in_file_order() -> None:
    blob = b"PATCH" + literal(0x2, b"AAAA") + literal(0x4, b"b") + literal(0x0, b"cc") + b"EOF"
    assert ips.apply_ips(bytes(8), blob) == b"ccAAbA\x00\x00"
    # The other way round the long record is written last and wins.
    blob = b"PATCH" + literal(0x4, b"b") + literal(0x2, b"AAAA") + b"EOF"
    assert ips.apply_ips(bytes(8), blob) == b"\x00\x00AAAA\x00\x00"
    # An RLE record is a write like any other.
    blob = b"PATCH" + literal(0x0, b"abcdefgh") + rle(0x2, 4, 0x2E) + b"EOF"
    assert ips.apply_ips(bytes(8), blob) == b"ab....gh"


def test_apply_with_the_bias_of_a_module_header() -> None:
    # A loader patches the decompressed image of a module but counts offsets from the
    # 0x100-byte header in front of it: record offset = offset in the image + 0x100.
    image = random.Random(0xBEA).randbytes(0x3000)
    sites = [(0x1234, bytes.fromhex("1F2003D5")), (0x2000, bytes(8))]  # offsets in the image
    blob = ips.build_ips([(offset + 0x100, data) for offset, data in sites])
    assert blob == V1_LITERAL

    expected = image[:0x1234] + bytes.fromhex("1F2003D5") + image[0x1238:0x2000] + bytes(8)
    expected += image[0x2008:]
    assert ips.apply_ips(image, blob, bias=0x100) == expected
    # The golden vector describes the same two changes, with an RLE record.
    assert ips.apply_ips(image, V1_IPS, bias=0x100) == expected
    assert ips.apply_ips(image, ips.parse_ips(V1_IPS32), bias=0x100) == expected
    # Without the bias everything lands 0x100 bytes too far.
    shifted = ips.apply_ips(image, blob)
    assert shifted != expected
    assert shifted[0x1334:0x1338] == bytes.fromhex("1F2003D5")
    assert shifted[0x1234:0x1238] == image[0x1234:0x1238]


def test_module_patch_site_that_lands_on_the_forbidden_offset() -> None:
    # Offset 0x454E46 of the image is record offset 0x454F46: the callback sees record
    # offsets and has to take the bias away itself.
    image = random.Random(0xBEA5).randbytes(0x455000)
    site = EOF_OFFSET - 0x100
    new = bytes.fromhex("1F2003D5")
    asked: list[int] = []

    def original_byte(offset: int) -> int:
        asked.append(offset)
        return image[offset - 0x100]

    blob = ips.build_ips([(site + 0x100, new)], original_byte=original_byte)
    assert asked == [0x454F45]
    assert blob == b"PATCH" + bytes.fromhex("454F45 0005") + image[site - 1 : site] + new + b"EOF"
    assert ips.apply_ips(image, blob, bias=0x100) == image[:site] + new + image[site + 4 :]


def test_apply_accepts_any_integer_bias() -> None:
    patch = IpsPatch(IpsFormat.IPS, (IpsRecord(0x10, b"ab"),))
    assert ips.apply_ips(bytes(4), patch, bias=0x10) == b"ab\x00\x00"
    assert ips.apply_ips(bytes(4), patch, bias=0xE) == b"\x00\x00ab"
    # A negative bias moves records up.
    assert ips.apply_ips(bytes(0x16), patch, bias=-4) == bytes(0x14) + b"ab"


def test_apply_rejects_records_below_the_target() -> None:
    patch = IpsPatch(IpsFormat.IPS, (IpsRecord(0xFF, b"ab"),))
    with pytest.raises(ValueError, match="bias"):
        ips.apply_ips(bytes(0x400), patch, bias=0x100)
    # Even when its last byte would fall inside.
    with pytest.raises(ValueError, match="bias"):
        ips.apply_ips(bytes(0x400), V1_IPS, bias=0x1335)
    assert ips.apply_ips(bytes(0x400), patch, bias=0xFF)[:3] == b"ab\x00"
    with pytest.raises(ValueError):
        ips.apply_ips(bytes(0x400), IpsPatch(IpsFormat.IPS, (IpsRecord(-1, b"ab"),)))


def test_apply_never_grows_the_target() -> None:
    target = bytes(16)
    fits = IpsPatch(IpsFormat.IPS, (IpsRecord(14, b"ab"),))
    assert ips.apply_ips(target, fits) == bytes(14) + b"ab"
    for record in (
        IpsRecord(15, b"ab"),  # the last byte is past the end
        IpsRecord(16, b"a"),  # starts right at the end
        IpsRecord(100, b"a"),  # starts far beyond
        IpsRecord(0, bytes(17)),  # larger than the whole target
    ):
        with pytest.raises(ValueError, match="past the end"):
            ips.apply_ips(target, IpsPatch(IpsFormat.IPS, (record,)))
    with pytest.raises(ValueError, match="past the end"):
        ips.apply_ips(b"", IpsPatch(IpsFormat.IPS, (IpsRecord(0, b"a"),)))
    with pytest.raises(ValueError, match="past the end"):
        ips.apply_ips(bytes(0x2104), V1_IPS)  # the RLE record needs four more bytes
    with pytest.raises(ValueError, match="past the end"):
        ips.apply_ips(bytes(0x3000), V1_IPS, bias=-0x1000)


def test_apply_truncates_after_the_records() -> None:
    target = bytes(range(0x40))
    expected = bytes(range(0x10)) + b"\xff" + bytes(range(0x11, 0x20))
    assert ips.apply_ips(target, TRUNCATING) == expected
    assert ips.apply_ips(target, ips.parse_ips(TRUNCATING)) == expected
    # A length equal to the size of the target changes nothing ...
    assert ips.apply_ips(target[:0x20], TRUNCATING) == expected
    # ... and one that is larger cannot be honoured: the target never grows.
    with pytest.raises(ValueError, match="truncation"):
        ips.apply_ips(target[:0x1F], TRUNCATING)
    # The records are written first: one beyond the cut is still checked and applied.
    late = IpsPatch(IpsFormat.IPS, (IpsRecord(0x30, b"zz"),), truncate=0x08)
    assert ips.apply_ips(target, late) == bytes(range(8))
    with pytest.raises(ValueError, match="past the end"):
        ips.apply_ips(target[:0x30], late)
    assert ips.apply_ips(target, IpsPatch(IpsFormat.IPS, (), truncate=0)) == b""


def test_apply_subtracts_the_bias_from_the_truncation_length() -> None:
    patch = IpsPatch(IpsFormat.IPS, (IpsRecord(0x110, b"\xff"),), truncate=0x120)
    target = bytes(range(0x40))
    assert ips.apply_ips(target, patch, bias=0x100) == (
        bytes(range(0x10)) + b"\xff" + bytes(range(0x11, 0x20))
    )
    beyond = IpsPatch(IpsFormat.IPS, (), truncate=0x120)
    assert ips.apply_ips(target, beyond, bias=0xE0) == target  # 0x40 bytes: all of them
    with pytest.raises(ValueError, match="truncation"):
        ips.apply_ips(target, beyond, bias=0xDF)  # 0x41 bytes: one more than there is
    below = IpsPatch(IpsFormat.IPS, (), truncate=0x50)
    with pytest.raises(ValueError, match="truncation"):
        ips.apply_ips(target, below, bias=0x100)  # a negative length
    assert ips.apply_ips(target, below, bias=0x50) == b""


def test_apply_reports_a_malformed_patch_file() -> None:
    with pytest.raises(FormatError):
        ips.apply_ips(bytes(16), b"PATCH\x00\x00\x01")
    with pytest.raises(FormatError):
        ips.apply_ips(bytes(16), b"")


@pytest.mark.parametrize(
    ("target", "patch", "bias"),
    [
        pytest.param("text", SMALLEST_IPS, 0, id="text target"),
        pytest.param(16, SMALLEST_IPS, 0, id="a size instead of a target"),
        pytest.param(None, SMALLEST_IPS, 0, id="no target"),
        pytest.param(bytes(16), "PATCHEOF", 0, id="text patch"),
        pytest.param(bytes(16), None, 0, id="no patch"),
        pytest.param(bytes(16), [IpsRecord(0, b"a")], 0, id="bare records"),
        pytest.param(bytes(16), SMALLEST_IPS, "0x100", id="text bias"),
        pytest.param(bytes(16), SMALLEST_IPS, 1.0, id="float bias"),
        pytest.param(bytes(16), SMALLEST_IPS, True, id="bool bias"),
        pytest.param(bytes(16), SMALLEST_IPS, None, id="no bias"),
    ],
)
def test_apply_rejects_arguments_of_the_wrong_type(
    target: object, patch: object, bias: object
) -> None:
    with pytest.raises(TypeError):
        ips.apply_ips(target, patch, bias=bias)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# describe_ips
# ----------------------------------------------------------------------------------


def test_describe_summarizes_a_patch() -> None:
    summary = ips.describe_ips(ips.parse_ips(V1_IPS))
    assert summary == {
        "format": "ips",
        "records": 2,
        "patched_bytes": 12,
        "lowest_offset": 0x1334,
        "highest_offset": 0x2107,
        "truncate": None,
    }
    assert json.loads(json.dumps(summary)) == summary
    assert type(summary["format"]) is str


def test_describe_looks_at_every_record_whatever_the_order() -> None:
    patch = IpsPatch(
        IpsFormat.IPS32,
        (IpsRecord(0x500, b"ab"), IpsRecord(0x10, b"c"), IpsRecord(0x400, bytes(0x200))),
    )
    assert ips.describe_ips(patch) == {
        "format": "ips32",
        "records": 3,
        "patched_bytes": 0x203,
        "lowest_offset": 0x10,
        "highest_offset": 0x5FF,
        "truncate": None,
    }


def test_describe_an_empty_and_a_truncating_patch() -> None:
    assert ips.describe_ips(ips.parse_ips(SMALLEST_IPS32)) == {
        "format": "ips32",
        "records": 0,
        "patched_bytes": 0,
        "lowest_offset": None,
        "highest_offset": None,
        "truncate": None,
    }
    summary = ips.describe_ips(ips.parse_ips(TRUNCATING))
    assert summary == {
        "format": "ips",
        "records": 1,
        "patched_bytes": 1,
        "lowest_offset": 0x10,
        "highest_offset": 0x10,
        "truncate": 0x20,
    }
    assert json.loads(json.dumps(summary)) == summary


def test_describe_what_the_writer_produced() -> None:
    data = random.Random(19).randbytes(FULL + 3)
    summary = ips.describe_ips(ips.parse_ips(ips.build_ips([(0x2000, data), (0x10, b"ab")])))
    assert summary == {
        "format": "ips",
        "records": 3,  # the long change takes two records
        "patched_bytes": FULL + 5,
        "lowest_offset": 0x10,
        "highest_offset": 0x2000 + FULL + 2,
        "truncate": None,
    }
