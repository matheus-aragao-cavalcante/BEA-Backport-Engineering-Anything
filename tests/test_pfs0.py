"""Tests for bea.formats.pfs0: the table of contents of a PFS0 archive.

Every archive is laid out by hand with ``struct`` from the documented layout (a
``0x10``-byte header, ``0x18``-byte entry records, the string table, then the data), so
the parser is checked against bytes it did not produce and the writer against a layout
it did not compute.
"""

from __future__ import annotations

import io
import random
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bea import fsutil
from bea.errors import FormatError
from bea.formats import pfs0
from bea.formats.pfs0 import Pfs0Archive, Pfs0Entry

HEADER = struct.Struct("<4sIII")  # magic, entry count, string table size, reserved
RECORD = struct.Struct("<QQII")  # data offset, data size, name offset, reserved

#: What the three entries of :func:`three_entries` hold.
PAYLOADS = (b"A" * 5, b"B" * 300, b"C")

#: Size of the large entry of :func:`write_sparse_package`.
BULK = 3 * 1024 * 1024


def assemble(
    records: Sequence[tuple[int, int, int]],
    string_table: bytes,
    data: bytes = b"",
    *,
    magic: bytes = b"PFS0",
    count: int | None = None,
    table_size: int | None = None,
) -> bytes:
    """Lay an archive out by hand.

    ``records`` are ``(data offset, data size, name offset)``. ``count`` and
    ``table_size`` replace what the header announces, to build headers that lie.
    """
    header = HEADER.pack(
        magic,
        len(records) if count is None else count,
        len(string_table) if table_size is None else table_size,
        0,
    )
    table = b"".join(RECORD.pack(offset, size, name, 0) for offset, size, name in records)
    return header + table + string_table + data


def three_entries() -> bytes:
    """Return a complete archive of three files, written out field by field.

    The data region starts at ``0x10 + 3 * 0x18 + 0x28 = 0x80``. Filler bytes separate
    the files, so only the stored offsets (not a running total of the sizes) find them.
    """
    return b"".join(
        [
            struct.pack("<4sIII", b"PFS0", 3, 0x28, 0),
            struct.pack("<QQII", 0x000, 5, 0x00, 0),
            struct.pack("<QQII", 0x010, 300, 0x0A, 0),
            struct.pack("<QQII", 0x200, 1, 0x0F, 0),
            # "alpha.bin" at 0x00, "beta" at 0x0A, "gamma.dat" at 0x0F, then padding.
            b"alpha.bin\x00beta\x00gamma.dat\x00".ljust(0x28, b"\x00"),
            PAYLOADS[0].ljust(0x010, b"\xee"),  # file offsets 0x080 to 0x090
            PAYLOADS[1].ljust(0x1F0, b"\xee"),  # file offsets 0x090 to 0x280
            PAYLOADS[2],  # file offset 0x280
        ]
    )


def one_name(raw_name: bytes) -> bytes:
    """Return an archive with a single empty entry whose stored name is ``raw_name``."""
    return assemble([(0, 0, 0)], raw_name + b"\x00")


# ------------------------------------------------------------------------- parsing


def test_parse_hand_assembled_archive_field_by_field() -> None:
    blob = three_entries()
    assert len(blob) == 0x281

    archive = pfs0.parse_pfs0(blob)

    assert archive.data_offset == 0x80
    assert len(archive.entries) == 3
    first, second, third = archive.entries
    assert (first.name, first.offset, first.size) == ("alpha.bin", 0x80, 5)
    assert (second.name, second.offset, second.size) == ("beta", 0x90, 300)
    assert (third.name, third.offset, third.size) == ("gamma.dat", 0x280, 1)
    assert archive == Pfs0Archive(
        (
            Pfs0Entry("alpha.bin", 0x80, 5),
            Pfs0Entry("beta", 0x90, 300),
            Pfs0Entry("gamma.dat", 0x280, 1),
        ),
        0x80,
    )


def test_offsets_are_absolute() -> None:
    blob = three_entries()
    archive = pfs0.parse_pfs0(blob, file_size=len(blob))
    # Slicing the file with what an entry says yields its payload: offsets count from
    # the start of the file, not from the start of the data region.
    for entry, payload in zip(archive.entries, PAYLOADS, strict=True):
        assert blob[entry.offset : entry.offset + entry.size] == payload


def test_entry_lookup_is_exact() -> None:
    archive = pfs0.parse_pfs0(three_entries())
    assert archive.entry("beta") is archive.entries[1]
    assert archive.entry("gamma.dat") == Pfs0Entry("gamma.dat", 0x280, 1)
    assert archive.entry("BETA") is None
    assert archive.entry("bet") is None
    assert archive.entry("beta ") is None
    assert archive.entry("") is None


def test_results_are_immutable_values() -> None:
    archive = pfs0.parse_pfs0(three_entries())
    with pytest.raises(AttributeError):
        archive.entries[0].size = 99  # type: ignore[misc]
    with pytest.raises(AttributeError):
        archive.data_offset = 0  # type: ignore[misc]
    assert hash(archive) == hash(pfs0.parse_pfs0(three_entries()))


def test_last_name_may_end_with_the_string_table() -> None:
    # "two" has no terminator: the string table ends right after it, and the data region
    # that follows starts with bytes that must not be taken for more of the name.
    blob = assemble([(0, 1, 0), (1, 2, 4)], b"one\x00two", b"xyz")
    archive = pfs0.parse_pfs0(blob, file_size=len(blob))
    assert archive.data_offset == 0x10 + 2 * 0x18 + 7 == 71
    assert archive.entries == (Pfs0Entry("one", 71, 1), Pfs0Entry("two", 72, 2))

    # A table that is nothing but one unterminated name.
    solo = pfs0.parse_pfs0(assemble([(0, 3, 0)], b"solo", b"xyz"))
    assert solo.entries == (Pfs0Entry("solo", 0x10 + 0x18 + 4, 3),)


def test_names_in_any_order_inside_a_padded_string_table() -> None:
    # Padding in front, the names stored in reverse order, padding behind: "second" is
    # at offset 2, "first" at offset 9, 19 bytes in all. The data is not stored in table
    # order either.
    string_table = b"\x00\x00second\x00first\x00\x00\x00\x00\x00"
    assert len(string_table) == 19
    blob = assemble([(3, 2, 9), (0, 3, 2)], string_table, b"222" + b"11")

    archive = pfs0.parse_pfs0(blob, file_size=len(blob))

    assert archive.data_offset == 0x10 + 2 * 0x18 + 19 == 83
    assert archive.entries == (Pfs0Entry("first", 86, 2), Pfs0Entry("second", 83, 3))
    assert blob[86:88] == b"11"
    assert blob[83:86] == b"222"


def test_reserved_fields_are_ignored() -> None:
    blob = (
        struct.pack("<4sIII", b"PFS0", 1, 4, 0xFFFFFFFF)
        + struct.pack("<QQII", 0, 2, 0, 0xA5A5A5A5)
        + b"res\x00"
        + b"ok"
    )
    assert pfs0.parse_pfs0(blob, file_size=len(blob)) == Pfs0Archive(
        (Pfs0Entry("res", 0x2C, 2),), 0x2C
    )


def test_names_may_share_bytes_of_the_string_table() -> None:
    # The second name starts in the middle of the first one and ends at the same NUL.
    archive = pfs0.parse_pfs0(assemble([(0, 0, 0), (0, 0, 2)], b"abc.bin\x00"))
    assert [entry.name for entry in archive.entries] == ["abc.bin", "c.bin"]


def test_names_are_utf8() -> None:
    names = ["ação.bin", "日本語.dat", "smile-\U0001f600", "plain"]
    encoded = [name.encode("utf-8") for name in names]
    assert [len(raw) for raw in encoded] == [10, 13, 10, 5]  # bytes, not characters
    # Each name follows the previous one and its NUL: offsets 0, 11, 25 and 36.
    archive = pfs0.parse_pfs0(
        assemble([(0, 0, 0), (0, 0, 11), (0, 0, 25), (0, 0, 36)], b"\x00".join(encoded) + b"\x00")
    )
    assert [entry.name for entry in archive.entries] == names


@pytest.mark.parametrize("name", ["...", ".hidden", "a..b", "with space.bin", "dot.at.end.", "~"])
def test_unusual_but_plain_names_are_accepted(name: str) -> None:
    assert pfs0.parse_pfs0(one_name(name.encode())).entries[0].name == name


def test_zero_entries() -> None:
    bare = struct.pack("<4sIII", b"PFS0", 0, 0, 0)
    assert pfs0.parse_pfs0(bare, file_size=0x10) == Pfs0Archive((), 0x10)

    padded = struct.pack("<4sIII", b"PFS0", 0, 0x10, 0) + bytes(0x10)
    archive = pfs0.parse_pfs0(padded, file_size=0x20)
    assert archive.entries == ()
    assert archive.data_offset == 0x20
    assert archive.entry("anything") is None


def test_only_the_complete_header_matters_whatever_holds_it() -> None:
    blob = three_entries()
    expected = pfs0.parse_pfs0(blob)
    assert pfs0.parse_pfs0(blob[:0x80]) == expected  # the complete header and nothing else
    assert pfs0.parse_pfs0(blob + b"\xff" * 99) == expected
    assert pfs0.parse_pfs0(bytearray(blob)) == expected
    assert pfs0.parse_pfs0(memoryview(blob)) == expected
    assert pfs0.parse_pfs0(memoryview(bytearray(blob))[:0x80]) == expected
    # A view made of wider items is still measured in bytes: 0x80 bytes are 32 items.
    words = memoryview(blob[:0x80]).cast("I")
    assert len(words) == 0x20
    assert pfs0.parse_pfs0(words) == expected


def test_a_failed_parse_leaves_the_callers_bytearray_resizable() -> None:
    blob = three_entries()
    buffer = bytearray(blob[:0x30])  # the header is not complete yet
    with pytest.raises(FormatError, match="truncated header") as caught:
        pfs0.parse_pfs0(buffer)
    # "Read more and try again" has to work while the error, whose traceback keeps the
    # parser's frame alive, still exists: no view of the buffer may survive the call.
    buffer += blob[0x30:]
    assert pfs0.parse_pfs0(buffer) == pfs0.parse_pfs0(blob)
    assert caught.value.__traceback__ is not None  # the error really was still around


def test_parse_rejects_what_is_not_bytes() -> None:
    with pytest.raises(TypeError):
        pfs0.parse_pfs0("PFS0")  # type: ignore[arg-type]


# ------------------------------------------------------------ is_pfs0 / header_size


def test_is_pfs0_looks_at_the_magic_only() -> None:
    assert pfs0.PFS0_MAGIC == b"PFS0"
    assert pfs0.is_pfs0(b"PFS0")
    assert pfs0.is_pfs0(three_entries())
    assert pfs0.is_pfs0(b"PFS0" + b"\xff" * 12)  # a header that parse_pfs0 would refuse
    for other in (b"", b"P", b"PFS", b"pfs0", b"HFS0", b"0SFP", b"\x00PFS0", b"XPFS0"):
        assert not pfs0.is_pfs0(other)


@pytest.mark.parametrize(
    ("count", "table_size", "expected"),
    [
        (0, 0, 0x10),
        (1, 0, 0x28),
        (0, 5, 0x15),
        (2, 7, 0x47),
        (3, 0x28, 0x80),
        (1000, 12345, 36361),  # 16 + 24000 + 12345
    ],
)
def test_header_size(count: int, table_size: int, expected: int) -> None:
    prefix = struct.pack("<4sIII", b"PFS0", count, table_size, 0)
    assert len(prefix) == 0x10
    assert pfs0.header_size(prefix) == expected
    # Only the first 0x10 bytes are looked at, and the reserved field does not count.
    assert pfs0.header_size(prefix + b"\xff" * 40) == expected
    assert pfs0.header_size(struct.pack("<4sIII", b"PFS0", count, table_size, 0xFFFFFFFF)) == (
        expected
    )


def test_header_size_is_where_the_data_starts() -> None:
    blob = three_entries()
    assert pfs0.header_size(blob[:0x10]) == 0x80 == pfs0.parse_pfs0(blob).data_offset


def test_header_size_accepts_the_limits_and_nothing_above() -> None:
    assert pfs0.MAX_ENTRIES == 65536
    assert pfs0.MAX_STRING_TABLE == 16 * 1024 * 1024
    at_the_limits = struct.pack("<4sIII", b"PFS0", 65536, 16 * 1024 * 1024, 0)
    # 16 + 65536 * 24 + 16777216
    assert pfs0.header_size(at_the_limits) == 18_350_096
    with pytest.raises(FormatError, match="entry count"):
        pfs0.header_size(struct.pack("<4sIII", b"PFS0", 65537, 0, 0))
    with pytest.raises(FormatError, match="string table size"):
        pfs0.header_size(struct.pack("<4sIII", b"PFS0", 0, 16 * 1024 * 1024 + 1, 0))


@pytest.mark.parametrize("length", [0, 1, 3, 4, 8, 15])
def test_header_size_needs_the_whole_fixed_header(length: int) -> None:
    prefix = struct.pack("<4sIII", b"PFS0", 1, 8, 0)[:length]
    with pytest.raises(FormatError, match="truncated header"):
        pfs0.header_size(prefix)


# --------------------------------------------------------------------- FormatError


@pytest.mark.parametrize("magic", [b"PFS1", b"pfs0", b"HFS0", b"0SFP", b"\x00\x00\x00\x00"])
def test_wrong_magic(magic: bytes) -> None:
    blob = magic + three_entries()[4:]
    with pytest.raises(FormatError, match="magic") as caught:
        pfs0.parse_pfs0(blob)
    assert repr(magic) in str(caught.value)  # what was found, next to what was expected
    assert repr(b"PFS0") in str(caught.value)
    with pytest.raises(FormatError, match="magic"):
        pfs0.header_size(blob[:0x10])


def test_a_short_file_with_other_bytes_has_the_wrong_magic() -> None:
    # Too short for a header, but already not what a PFS0 archive starts with.
    for prefix in (b"X", b"PX", b"PFX"):
        with pytest.raises(FormatError, match="magic"):
            pfs0.parse_pfs0(prefix)


def test_counts_above_the_limits() -> None:
    with pytest.raises(FormatError, match="entry count at offset 0x4 is 65537"):
        pfs0.parse_pfs0(struct.pack("<4sIII", b"PFS0", pfs0.MAX_ENTRIES + 1, 0, 0))
    with pytest.raises(FormatError, match="string table size at offset 0x8 is 0x1000001"):
        pfs0.parse_pfs0(struct.pack("<4sIII", b"PFS0", 0, pfs0.MAX_STRING_TABLE + 1, 0))


def test_hostile_headers_fail_at_once() -> None:
    # Counts that would mean gigabytes: refused from the 16 bytes alone.
    with pytest.raises(FormatError, match="entry count"):
        pfs0.parse_pfs0(struct.pack("<4sIII", b"PFS0", 0xFFFFFFFF, 0xFFFFFFFF, 0))
    with pytest.raises(FormatError, match="string table size"):
        pfs0.parse_pfs0(struct.pack("<4sIII", b"PFS0", 0, 0xFFFFFFFF, 0))
    # Within the limits, but there is nothing behind the announcement.
    liar = struct.pack("<4sIII", b"PFS0", pfs0.MAX_ENTRIES, pfs0.MAX_STRING_TABLE, 0)
    with pytest.raises(FormatError, match="truncated header"):
        pfs0.parse_pfs0(liar)
    # Thousands of entries that all point into one long run of bytes without a single
    # terminator: decoding that run once per entry would take gigabytes.
    endless = assemble([(0, 0, start) for start in range(4000)], b"n" * 400_000)
    with pytest.raises(FormatError, match="longer than"):
        pfs0.parse_pfs0(endless)


def test_every_truncation_of_the_header_is_reported() -> None:
    blob = three_entries()
    for length in range(0x80):
        with pytest.raises(FormatError, match="truncated header"):
            pfs0.parse_pfs0(blob[:length])
    assert pfs0.parse_pfs0(blob[:0x80]).data_offset == 0x80


@pytest.mark.parametrize("name_offset", [8, 9, 0x100, 0xFFFFFFFF])
def test_name_offset_outside_the_string_table(name_offset: int) -> None:
    blob = assemble([(0, 0, 0), (0, 0, name_offset)], b"inside\x00\x00")
    with pytest.raises(FormatError, match="name offset") as caught:
        pfs0.parse_pfs0(blob)
    # The report says which record is at fault and where that record is in the file.
    assert "entry 1 (offset 0x28)" in str(caught.value)
    assert f"0x{name_offset:X}" in str(caught.value)


def test_an_empty_string_table_holds_no_name() -> None:
    with pytest.raises(FormatError, match="name offset"):
        pfs0.parse_pfs0(assemble([(0, 0, 0)], b""))


@pytest.mark.parametrize(
    "raw_name",
    [
        b"\xff",
        b"caf\xe9.bin",  # Latin-1, not UTF-8
        b"cut\xe3\x81",  # a three-byte sequence missing its last byte
        b"\xc0\xaf",  # an overlong encoding of "/"
        b"\xed\xa0\x80",  # a surrogate
    ],
)
def test_names_must_be_utf8(raw_name: bytes) -> None:
    with pytest.raises(FormatError, match="UTF-8"):
        pfs0.parse_pfs0(one_name(raw_name))
    # The same bytes without a terminator, at the very end of the table.
    with pytest.raises(FormatError, match="UTF-8"):
        pfs0.parse_pfs0(assemble([(0, 0, 0)], raw_name))


@pytest.mark.parametrize(
    ("raw_name", "message"),
    [
        (b"dir/file.bin", "path separator"),
        (b"dir\\file.bin", "path separator"),
        (b"/rooted", "path separator"),
        (b"../escape", "path separator"),
        (b"..", "directory reference"),
        (b".", "directory reference"),
        (b"", "empty"),
    ],
)
def test_names_must_be_plain_file_names(raw_name: bytes, message: str) -> None:
    blob = assemble([(0, 0, 0), (0, 0, 5)], b"fine\x00" + raw_name + b"\x00")
    with pytest.raises(FormatError, match=message) as caught:
        pfs0.parse_pfs0(blob)
    assert "entry 1" in str(caught.value)


def test_duplicate_names() -> None:
    # Two records that point at the same name.
    with pytest.raises(FormatError, match="duplicate name 'same'"):
        pfs0.parse_pfs0(assemble([(0, 1, 0), (1, 1, 0)], b"same\x00"))
    # The same name stored twice; the report names both records.
    blob = assemble([(0, 0, 0), (0, 0, 5), (0, 0, 11)], b"twin\x00other\x00twin\x00")
    with pytest.raises(FormatError, match="duplicate name 'twin'") as caught:
        pfs0.parse_pfs0(blob)
    assert "entry 2" in str(caught.value)
    assert "entry 0" in str(caught.value)


def test_names_that_differ_only_by_case_are_different_names() -> None:
    archive = pfs0.parse_pfs0(assemble([(0, 1, 0), (1, 2, 9)], b"Data.bin\x00data.bin\x00"))
    assert archive.entry("Data.bin") == Pfs0Entry("Data.bin", 0x52, 1)
    assert archive.entry("data.bin") == Pfs0Entry("data.bin", 0x53, 2)


def test_name_length_limit() -> None:
    limit = pfs0.MAX_NAME_BYTES
    assert limit == 0x300
    longest = b"n" * limit
    assert pfs0.parse_pfs0(one_name(longest)).entries[0].name == "n" * limit
    # Unterminated, ending with the table.
    assert pfs0.parse_pfs0(assemble([(0, 0, 0)], longest)).entries[0].name == "n" * limit
    with pytest.raises(FormatError, match="longer than"):
        pfs0.parse_pfs0(one_name(longest + b"n"))
    with pytest.raises(FormatError, match="longer than"):
        pfs0.parse_pfs0(assemble([(0, 0, 0)], longest + b"n"))

    # The limit counts bytes, not characters: 256 three-byte characters are 0x300 bytes.
    wide = "あ" * 256
    assert pfs0.parse_pfs0(one_name(wide.encode())).entries[0].name == wide
    with pytest.raises(FormatError, match="longer than"):
        pfs0.parse_pfs0(one_name((wide + "あ").encode()))


def test_entries_must_lie_inside_the_file() -> None:
    blob = three_entries()  # entries end at 0x85, 0x1BC and 0x281
    whole = pfs0.parse_pfs0(blob)
    assert pfs0.parse_pfs0(blob, file_size=0x281) == whole
    assert pfs0.parse_pfs0(blob, file_size=0x281 + 1000) == whole  # trailing bytes are fine

    # One byte short: only the last entry does not fit.
    with pytest.raises(FormatError, match=r"truncated package.*'gamma\.dat'"):
        pfs0.parse_pfs0(blob, file_size=0x280)
    # The second and third entries do not fit: the first of them is the one reported.
    for file_size in (0x1BB, 0x100, 0x85):
        with pytest.raises(FormatError, match=r"truncated package.*'beta'"):
            pfs0.parse_pfs0(blob, file_size=file_size)
    for file_size in (0x84, 0x80):
        with pytest.raises(FormatError, match=r"truncated package.*'alpha\.bin'"):
            pfs0.parse_pfs0(blob, file_size=file_size)
    # Without a file size nothing can be checked: the header alone is accepted.
    assert pfs0.parse_pfs0(blob[:0x80]) == whole


def test_an_empty_entry_may_sit_at_the_very_end_of_the_file() -> None:
    at_the_end = assemble([(0, 0, 0)], b"empty\x00")
    assert len(at_the_end) == 0x2E
    assert pfs0.parse_pfs0(at_the_end, file_size=0x2E).entries == (Pfs0Entry("empty", 0x2E, 0),)
    # Empty, but placed one byte past the end of the file.
    with pytest.raises(FormatError, match=r"truncated package.*'empty'"):
        pfs0.parse_pfs0(assemble([(1, 0, 0)], b"empty\x00"), file_size=0x2E)


def test_the_largest_offset_and_size_do_not_overflow() -> None:
    top = 2**64 - 1
    blob = assemble([(top, top, 0)], b"huge\x00")
    archive = pfs0.parse_pfs0(blob)  # nothing to check them against
    assert archive.entries == (Pfs0Entry("huge", 0x2D + top, top),)
    with pytest.raises(FormatError, match=r"truncated package.*'huge'"):
        pfs0.parse_pfs0(blob, file_size=len(blob))
    with pytest.raises(FormatError, match=r"truncated package.*'huge'"):
        pfs0.parse_pfs0(blob, file_size=2**63)


def test_file_size_smaller_than_the_header() -> None:
    blob = three_entries()
    with pytest.raises(FormatError, match="truncated package"):
        pfs0.parse_pfs0(blob, file_size=0x7F)
    with pytest.raises(FormatError, match="truncated package"):
        pfs0.parse_pfs0(struct.pack("<4sIII", b"PFS0", 0, 0, 0), file_size=0x0F)
    with pytest.raises(ValueError, match="negative"):
        pfs0.parse_pfs0(blob, file_size=-1)


def test_corrupt_headers_are_rejected_or_parsed_but_never_crash() -> None:
    blob = three_entries()
    rng = random.Random(0xBEA)
    parsed = rejected = 0
    for _ in range(3000):
        mutated = bytearray(blob)
        for _ in range(rng.choice((1, 1, 1, 2, 4))):
            mutated[rng.randrange(0x80)] = rng.randrange(256)
        length = len(mutated) if rng.random() < 0.8 else rng.randrange(len(mutated))
        candidate = bytes(mutated[:length])
        try:
            archive = pfs0.parse_pfs0(candidate, file_size=len(candidate))
        except FormatError:
            rejected += 1
            continue
        parsed += 1
        # Whatever is accepted agrees with the bytes it came from.
        _magic, count, table_size, _reserved = struct.unpack_from("<4sIII", candidate)
        assert archive.data_offset == 0x10 + count * 0x18 + table_size <= len(candidate)
        assert len(archive.entries) == count
        assert len({entry.name for entry in archive.entries}) == count
        for entry in archive.entries:
            assert entry.name not in ("", ".", "..")
            assert not set(entry.name) & {"/", "\\", "\x00"}
            assert archive.data_offset <= entry.offset
            assert entry.offset + entry.size <= len(candidate)
    # Both outcomes happened often enough for the loop to mean something.
    assert parsed > 100
    assert rejected > 100


# ------------------------------------------------------------------- reading files


class _LoggingFile(io.FileIO):
    """A raw file that logs what is really read from it and can misbehave on request.

    Each log record is ``(position, bytes asked for, bytes returned)``. ``chunk_limit``
    caps what one read returns (a short read that is not the end of the file);
    ``eof_at`` makes the file end early (it shrank after its size was taken).
    """

    def __init__(
        self,
        path: str,
        log: list[tuple[int, int, int]],
        chunk_limit: int | None,
        eof_at: int | None,
    ) -> None:
        super().__init__(path, "rb")
        self._log = log
        self._chunk_limit = chunk_limit
        self._eof_at = eof_at

    def read(self, size: int | None = -1, /) -> bytes:
        assert size is not None and size >= 0, "asked for everything that is left"
        position = self.tell()
        allowed = size if self._chunk_limit is None else min(size, self._chunk_limit)
        if self._eof_at is not None:
            allowed = max(0, min(allowed, self._eof_at - position))
        data = super().read(allowed)
        self._log.append((position, size, len(data)))
        return data

    def readinto(self, buffer: Any, /) -> int:
        # What a buffered reader stacked on this file calls: a buffer-sized request.
        position = self.tell()
        count = super().readinto(buffer)
        self._log.append((position, len(buffer), count))
        return count

    def readall(self) -> bytes:
        raise AssertionError("asked for everything that is left")


def intercept_open(
    monkeypatch: pytest.MonkeyPatch, *, chunk_limit: int | None = None, eof_at: int | None = None
) -> list[tuple[int, int, int]]:
    """Make the module open its files as :class:`_LoggingFile`; return the read log.

    Whatever buffering the module asks for is stacked on top of the logging file, so the
    log shows what is fetched from the file itself, not what the module asked a buffer.
    """
    log: list[tuple[int, int, int]] = []

    def logging_open(
        file: str, mode: str = "r", buffering: int = -1
    ) -> io.FileIO | io.BufferedReader:
        assert mode == "rb"
        raw = _LoggingFile(file, log, chunk_limit, eof_at)
        return raw if buffering == 0 else io.BufferedReader(raw)

    monkeypatch.setattr(pfs0, "open", logging_open, raising=False)
    return log


def write_sparse_package(path: Path) -> dict[str, bytes]:
    """Write an archive with megabytes of data and return what each entry holds.

    The header is 0x10 + 3 * 0x18 + 0x38 = 0x90 bytes. Nearly all of the large entry is
    a hole that is never written (sparse where the filesystem supports it).
    """
    string_table = b"first.bin\x00bulk.dat\x00last.txt\x00".ljust(0x38, b"\x00")
    header = assemble([(0, 5, 0), (5, BULK, 10), (5 + BULK, 4, 19)], string_table)
    assert len(header) == 0x90
    with open(path, "wb") as handle:
        handle.write(header + b"first" + b"<<")
        handle.seek(0x90 + 5 + BULK - 2)
        handle.write(b">>" + b"tail")
    return {"first.bin": b"first", "bulk.dat": b"<<" + bytes(BULK - 4) + b">>", "last.txt": b"tail"}


def test_read_pfs0_and_read_entry_with_megabytes_of_data(tmp_path: Path) -> None:
    package = tmp_path / "synthetic.pfs0"
    contents = write_sparse_package(package)
    assert package.stat().st_size == 0x90 + 5 + BULK + 4

    archive = pfs0.read_pfs0(package)

    assert archive == Pfs0Archive(
        (
            Pfs0Entry("first.bin", 0x90, 5),
            Pfs0Entry("bulk.dat", 0x95, BULK),
            Pfs0Entry("last.txt", 0x95 + BULK, 4),
        ),
        0x90,
    )
    assert pfs0.read_pfs0(str(package)) == archive  # a str path works like a Path
    for entry in archive.entries:
        assert pfs0.read_entry(package, entry) == contents[entry.name]
    assert pfs0.read_entry(str(package), archive.entries[0]) == b"first"


def test_read_pfs0_never_reads_past_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "synthetic.pfs0"
    write_sparse_package(package)
    log = intercept_open(monkeypatch)

    archive = pfs0.read_pfs0(package)

    assert archive.data_offset == 0x90
    assert log, "read_pfs0 did not open the file through open()"
    assert log[0][0] == 0
    # The header was fetched exactly once, and of the megabytes behind it not one byte
    # was even asked for (a buffered reader would ask for a whole buffer of them).
    assert sum(returned for _position, _asked, returned in log) == 0x90
    assert max(position + asked for position, asked, _returned in log) == 0x90


def test_the_header_alone_holds_the_whole_table_of_contents(tmp_path: Path) -> None:
    package = tmp_path / "synthetic.pfs0"
    write_sparse_package(package)
    complete = pfs0.read_pfs0(package)
    data = package.read_bytes()

    # A copy that keeps only the header still parses to the same table of contents, as
    # long as nobody asks whether the data is there ...
    assert pfs0.parse_pfs0(data[:0x90]) == complete
    # ... and read_pfs0 does ask: it passes the real size of the file.
    header_only = tmp_path / "header-only.pfs0"
    header_only.write_bytes(data[:0x90])
    with pytest.raises(FormatError, match=r"truncated package.*'first\.bin'"):
        pfs0.read_pfs0(header_only)


def test_read_pfs0_names_the_first_entry_a_truncated_package_lost(tmp_path: Path) -> None:
    package = tmp_path / "synthetic.pfs0"
    write_sparse_package(package)
    data = package.read_bytes()
    cut = tmp_path / "cut.pfs0"

    cut.write_bytes(data[: 0x95 + BULK // 2])  # in the middle of the large entry
    with pytest.raises(FormatError, match=r"truncated package.*'bulk\.dat'"):
        pfs0.read_pfs0(cut)

    cut.write_bytes(data[:-1])  # one byte short
    with pytest.raises(FormatError, match=r"truncated package.*'last\.txt'"):
        pfs0.read_pfs0(cut)

    cut.write_bytes(data + b"trailing bytes")  # longer than needed is not truncated
    assert pfs0.read_pfs0(cut) == pfs0.read_pfs0(package)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "truncated header"),
        (b"PFS0", "truncated header"),
        (b"PFS0" + bytes(11), "truncated header"),
        (b"NOPE" + bytes(60), "magic"),
        (b"\x7fELF", "magic"),
        # Two entries announced, one present: cut inside the entry table.
        (HEADER.pack(b"PFS0", 2, 8, 0) + bytes(0x18), "truncated header"),
        # Cut inside the string table.
        (HEADER.pack(b"PFS0", 1, 8, 0) + RECORD.pack(0, 0, 0, 0) + b"name", "truncated header"),
        (HEADER.pack(b"PFS0", 65537, 0, 0) + bytes(64), "entry count"),
        (HEADER.pack(b"PFS0", 0, 16 * 1024 * 1024 + 1, 0) + bytes(64), "string table size"),
        (assemble([(0, 0, 0)], b"a/b\x00"), "path separator"),
    ],
)
def test_read_pfs0_rejects_damaged_files(tmp_path: Path, content: bytes, message: str) -> None:
    damaged = tmp_path / "damaged.pfs0"
    damaged.write_bytes(content)
    with pytest.raises(FormatError, match=message):
        pfs0.read_pfs0(damaged)


def test_read_pfs0_does_not_take_the_header_at_its_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sixteen bytes that announce the largest header the limits allow (17.5 MiB).
    liar = tmp_path / "liar.pfs0"
    liar.write_bytes(struct.pack("<4sIII", b"PFS0", pfs0.MAX_ENTRIES, pfs0.MAX_STRING_TABLE, 0))
    log = intercept_open(monkeypatch)
    with pytest.raises(FormatError, match="truncated header"):
        pfs0.read_pfs0(liar)
    # Nothing beyond the sixteen bytes the file really has was ever asked for.
    assert log
    assert all(position + asked <= 0x10 for position, asked, _returned in log)


def test_reads_that_come_back_short_are_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "small.pfs0"
    package.write_bytes(three_entries())
    log = intercept_open(monkeypatch, chunk_limit=7)  # no read returns more than 7 bytes

    archive = pfs0.read_pfs0(package)
    assert archive == pfs0.parse_pfs0(three_entries())
    assert pfs0.read_entry(package, archive.entries[1]) == PAYLOADS[1]
    assert max(returned for _position, _asked, returned in log) == 7


def test_a_file_that_shrinks_while_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "small.pfs0"
    package.write_bytes(three_entries())
    beta = Pfs0Entry("beta", 0x90, 300)

    # The size says 0x281 bytes, but the file ends inside the header.
    intercept_open(monkeypatch, eof_at=0x30)
    with pytest.raises(FormatError, match="truncated header"):
        pfs0.read_pfs0(package)

    # The size says the entry is all there, but reading stops after 16 of its bytes.
    intercept_open(monkeypatch, eof_at=0xA0)
    with pytest.raises(FormatError, match=r"'beta'.*0x10 of its 0x12C bytes"):
        pfs0.read_entry(package, beta)


def test_read_entry_refuses_what_is_not_inside_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "small.pfs0"
    package.write_bytes(three_entries())

    # An entry is a name on a range of bytes: any range inside the file can be read.
    assert pfs0.read_entry(package, Pfs0Entry("alpha.bin", 0x80, 5)) == PAYLOADS[0]
    assert pfs0.read_entry(package, Pfs0Entry("magic", 0, 4)) == b"PFS0"
    assert pfs0.read_entry(package, Pfs0Entry("nothing", 0x281, 0)) == b""

    log = intercept_open(monkeypatch)
    with pytest.raises(FormatError, match="'beyond'"):
        pfs0.read_entry(package, Pfs0Entry("beyond", 0x280, 2))
    with pytest.raises(FormatError, match="'far'"):
        pfs0.read_entry(package, Pfs0Entry("far", 2**62, 1))
    with pytest.raises(FormatError, match="'vast'"):
        pfs0.read_entry(package, Pfs0Entry("vast", 0, 2**62))
    # Refused from the size of the file alone: nothing was read, let alone 2**62 bytes.
    assert log == []

    with pytest.raises(ValueError, match="negative"):
        pfs0.read_entry(package, Pfs0Entry("before", -1, 1))
    with pytest.raises(ValueError, match="negative"):
        pfs0.read_entry(package, Pfs0Entry("inverted", 0, -1))


def test_a_missing_file_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pfs0.read_pfs0(tmp_path / "missing.pfs0")
    with pytest.raises(FileNotFoundError):
        pfs0.read_entry(tmp_path / "missing.pfs0", Pfs0Entry("x", 0, 0))


def test_archives_behind_very_long_paths_can_be_read(tmp_path: Path) -> None:
    # Well past the 260 characters of classic Windows APIs.
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/synthetic.pfs0"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, three_entries())

    archive = pfs0.read_pfs0(target)
    assert archive == pfs0.parse_pfs0(three_entries())
    assert pfs0.read_entry(target, archive.entries[1]) == PAYLOADS[1]


# ------------------------------------------------------------------------- writing

#: The files of the layout tests. Their names take 6 + 11 + 2 = 19 bytes with the NULs,
#: so an unpadded header ends at 0x10 + 3 * 0x18 + 19 = 107.
THREE_FILES = [("a.bin", b"AAAA"), ("readme.txt", b""), ("z", b"z" * 10)]


def test_build_matches_a_hand_computed_layout() -> None:
    blob = pfs0.build_pfs0(THREE_FILES)

    # The next multiple of 0x20 after 107 is 128: the string table grows by 21 zero
    # bytes, to 40. The empty file takes no room: the third one starts where it would.
    expected = b"".join(
        [
            struct.pack("<4sIII", b"PFS0", 3, 40, 0),
            struct.pack("<QQII", 0, 4, 0, 0),
            struct.pack("<QQII", 4, 0, 6, 0),
            struct.pack("<QQII", 4, 10, 17, 0),
            b"a.bin\x00readme.txt\x00z\x00" + bytes(21),
            b"AAAA",
            b"zzzzzzzzzz",
        ]
    )
    assert len(expected) == 128 + 14
    assert blob == expected
    assert type(blob) is bytes

    assert pfs0.parse_pfs0(blob, file_size=len(blob)) == Pfs0Archive(
        (Pfs0Entry("a.bin", 128, 4), Pfs0Entry("readme.txt", 132, 0), Pfs0Entry("z", 132, 10)),
        128,
    )


@pytest.mark.parametrize(
    ("alignment", "table_size"),
    [
        (1, 19),  # no padding: the header ends at 107
        (2, 20),  # 108
        (3, 20),  # 108 = 36 * 3
        (0x10, 24),  # 112
        (0x20, 40),  # 128
        (0x40, 40),  # 128
        (107, 19),  # already a multiple: nothing is added
        (0x200, 424),  # 512
        (1000, 912),  # 1000: the alignment need not be a power of two
    ],
)
def test_build_pads_the_string_table_up_to_the_alignment(alignment: int, table_size: int) -> None:
    blob = pfs0.build_pfs0(THREE_FILES, string_table_alignment=alignment)
    header_end = 0x10 + 3 * 0x18 + table_size
    assert header_end % alignment == 0
    assert struct.unpack_from("<4sIII", blob) == (b"PFS0", 3, table_size, 0)
    # The names, NUL-terminated, then nothing but zeros up to the data.
    assert blob[0x58 : 0x58 + 19] == b"a.bin\x00readme.txt\x00z\x00"
    assert blob[0x58 + 19 : header_end] == bytes(table_size - 19)
    assert blob[header_end:] == b"AAAA" + b"z" * 10
    # The records do not depend on the padding.
    assert blob[0x10:0x58] == pfs0.build_pfs0(THREE_FILES, string_table_alignment=1)[0x10:0x58]


def test_build_without_files() -> None:
    bare = pfs0.build_pfs0([], string_table_alignment=1)
    assert bare == struct.pack("<4sIII", b"PFS0", 0, 0, 0)
    padded = pfs0.build_pfs0([])
    assert padded == struct.pack("<4sIII", b"PFS0", 0, 0x10, 0) + bytes(0x10)
    assert pfs0.parse_pfs0(bare, file_size=0x10) == Pfs0Archive((), 0x10)
    assert pfs0.parse_pfs0(padded, file_size=0x20) == Pfs0Archive((), 0x20)


@pytest.mark.parametrize("alignment", [1, 0x10, 0x20, 0x200])
def test_build_round_trips_through_the_parser_and_a_file(tmp_path: Path, alignment: int) -> None:
    rng = random.Random(alignment)
    files = [
        ("ação.bin", b""),
        ("日本語.dat", rng.randbytes(1)),
        ("smile-\U0001f600", rng.randbytes(77)),
    ]
    files += [(f"file{index}.bin", rng.randbytes(rng.randrange(400))) for index in range(40)]
    names = [name for name, _data in files]
    payloads = [data for _name, data in files]
    unpadded = 0x10 + 0x18 * len(files) + sum(len(name.encode()) + 1 for name in names)

    blob = pfs0.build_pfs0(files, string_table_alignment=alignment)
    archive = pfs0.parse_pfs0(blob, file_size=len(blob))

    # The smallest multiple of the alignment that holds the header.
    assert archive.data_offset % alignment == 0
    assert 0 <= archive.data_offset - unpadded < alignment
    assert [entry.name for entry in archive.entries] == names
    assert [entry.size for entry in archive.entries] == [len(data) for data in payloads]
    # Back to back, in the given order, right behind the header.
    assert blob[archive.data_offset :] == b"".join(payloads)
    for entry, data in zip(archive.entries, payloads, strict=True):
        assert blob[entry.offset : entry.offset + entry.size] == data

    package = tmp_path / "round-trip.pfs0"
    fsutil.atomic_write_bytes(package, blob)
    assert pfs0.read_pfs0(package) == archive
    for entry, data in zip(archive.entries, payloads, strict=True):
        assert pfs0.read_entry(package, entry) == data


def test_the_largest_archive_the_limits_allow() -> None:
    count = pfs0.MAX_ENTRIES
    names = [f"f{index:05d}" for index in range(count)]  # 6 characters and a NUL: 7 bytes
    string_table = ("\x00".join(names) + "\x00").encode()
    assert len(string_table) == 7 * count
    by_hand = b"".join(
        [
            struct.pack("<4sIII", b"PFS0", count, 7 * count, 0),
            *(struct.pack("<QQII", index, 1, 7 * index, 0) for index in range(count)),
            string_table,
            bytes(index % 251 for index in range(count)),
        ]
    )
    data_offset = 0x10 + 0x18 * count + 7 * count

    archive = pfs0.parse_pfs0(by_hand, file_size=len(by_hand))
    assert archive.data_offset == data_offset
    assert archive.entries == tuple(
        Pfs0Entry(name, data_offset + index, 1) for index, name in enumerate(names)
    )
    assert archive.entry("f65535") == Pfs0Entry("f65535", data_offset + 65535, 1)
    with pytest.raises(FormatError, match=r"truncated package.*'f65535'"):
        pfs0.parse_pfs0(by_hand, file_size=len(by_hand) - 1)

    # The writer produces the very same bytes, and refuses one file more.
    files = [(name, bytes([index % 251])) for index, name in enumerate(names)]
    assert pfs0.build_pfs0(files, string_table_alignment=1) == by_hand
    with pytest.raises(ValueError, match="at most 65536"):
        pfs0.build_pfs0([*files, ("one-too-many", b"")])


@pytest.mark.parametrize(
    ("name", "message"),
    [
        pytest.param("", "empty", id="empty"),
        pytest.param(".", "directory reference", id="dot"),
        pytest.param("..", "directory reference", id="dot-dot"),
        pytest.param("dir/file", "path separator", id="slash"),
        pytest.param("dir\\file", "path separator", id="backslash"),
        pytest.param("/rooted", "path separator", id="rooted"),
        pytest.param("nul\x00inside", "NUL", id="nul"),
        pytest.param("n" * 0x301, "longer than", id="too-long"),
        pytest.param("あ" * 257, "longer than", id="too-long-in-bytes"),
        pytest.param("lone-\ud800", "UTF-8", id="not-encodable"),
    ],
)
def test_build_rejects_names_the_parser_would_refuse(name: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        pfs0.build_pfs0([("fine.bin", b"1"), (name, b"2")])
    assert "file 1" in str(caught.value)  # the report says which file is at fault


def test_build_accepts_the_longest_name() -> None:
    name = "n" * 0x300
    blob = pfs0.build_pfs0([(name, b"x")])
    assert pfs0.parse_pfs0(blob, file_size=len(blob)).entries[0].name == name


def test_build_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        pfs0.build_pfs0([("a", b"1"), ("b", b"2"), ("a", b"3")])
    # Case matters, as it does for the parser.
    blob = pfs0.build_pfs0([("a", b"1"), ("A", b"2")])
    assert [entry.name for entry in pfs0.parse_pfs0(blob).entries] == ["a", "A"]


@pytest.mark.parametrize("alignment", [0, -1, -0x20])
def test_build_rejects_an_alignment_below_one(alignment: int) -> None:
    with pytest.raises(ValueError, match="string_table_alignment"):
        pfs0.build_pfs0(THREE_FILES, string_table_alignment=alignment)


def test_build_refuses_a_string_table_above_the_limit() -> None:
    # The padding alone would be far larger than any string table may be; it is refused
    # before it is allocated (2**62 bytes could not be).
    with pytest.raises(ValueError, match="string table"):
        pfs0.build_pfs0(THREE_FILES, string_table_alignment=2**62)
    with pytest.raises(ValueError, match="string table"):
        pfs0.build_pfs0([], string_table_alignment=pfs0.MAX_STRING_TABLE + 0x11)
