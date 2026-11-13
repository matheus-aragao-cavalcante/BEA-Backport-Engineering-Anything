"""Tests for bea.formats.npdm and bea.formats.nacp: which game an extracted folder holds.

Every input is assembled here with ``struct``, field by field, at the offsets given by
the public documentation (switchbrew wiki, pages "NPDM" and "NACP"). The parsers are
never checked against the modules' own writers, and the writers are checked against
layouts built by hand. Nothing comes from a real game: the names are invented and the
IDs are computed at run time.
"""

from __future__ import annotations

import random
import re
import string
import struct
import tracemalloc
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from bea import fsutil
from bea.errors import FormatError
from bea.formats import nacp, npdm

#: Made-up application IDs. The low 13 bits of an application ID are zero.
APP_ID = (0x0100 << 48) | 0xABCDEF012000
OTHER_ID = (0x0100 << 48) | 0xBEA0BEA0E000
THIRD_ID = (0x0100 << 48) | 0x5EED5EED4000
#: The range of application IDs, both ends included.
FIRST_APPLICATION_ID = (0x0100 << 48) | 0x10000
LAST_APPLICATION_ID = (0x0200 << 48) - 1
U64_MAX = (1 << 64) - 1
MIB = 1024 * 1024

JAPANESE_NAME = "サンプルゲーム"
JAPANESE_PUBLISHER = "サンプル出版"
POLISH_NAME = "Przykładowa Gra"
POLISH_PUBLISHER = "Przykładowy Wydawca"
THAI_NAME = "เกมตัวอย่าง"
THAI_PUBLISHER = "ผู้จัดจำหน่ายตัวอย่าง"


def peak_allocation(function: Callable[[], object]) -> int:
    """Run ``function`` and return the most memory Python held at once while it ran."""
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before, _ = tracemalloc.get_traced_memory()
        function()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()
    return peak - before


# ----------------------------------------------------------------------------------
# main.npdm: inputs built by hand
# ----------------------------------------------------------------------------------


def documented_npdm(program_id: int, id_min: int, id_max: int) -> bytearray:
    """The minimal 0x300-byte file, written at the absolute offsets of the documentation."""
    data = bytearray(0x300)
    data[0x000:0x004] = b"META"
    data[0x00C] = 0x07  # Flags0: 64-bit instructions, address space 3
    data[0x00E] = 0x2C  # main thread priority 44
    struct.pack_into("<I", data, 0x01C, 0x100000)  # main thread stack size
    data[0x020:0x02B] = b"Application"
    struct.pack_into("<I", data, 0x070, 0x2C0)  # AciOffset
    struct.pack_into("<I", data, 0x074, 0x40)  # AciSize
    struct.pack_into("<I", data, 0x078, 0x80)  # AcidOffset
    struct.pack_into("<I", data, 0x07C, 0x240)  # AcidSize
    # ACID block at 0x80: 0x200 bytes of signature and key (zero), then its header.
    data[0x280:0x284] = b"ACID"
    struct.pack_into("<I", data, 0x284, 0x140)
    struct.pack_into("<I", data, 0x28C, 1)
    struct.pack_into("<Q", data, 0x290, id_min)
    struct.pack_into("<Q", data, 0x298, id_max)
    for offset in (0x2A0, 0x2A8, 0x2B0):
        struct.pack_into("<II", data, offset, 0x240, 0)
    # ACI0 block at 0x2C0.
    data[0x2C0:0x2C4] = b"ACI0"
    struct.pack_into("<Q", data, 0x2D0, program_id)
    for offset in (0x2E0, 0x2E8, 0x2F0):
        struct.pack_into("<II", data, offset, 0x40, 0)
    return data


def valid_npdm() -> bytearray:
    """A valid file for ``APP_ID`` whose ACID block allows exactly that ID."""
    return documented_npdm(APP_ID, APP_ID, APP_ID)


def patched_npdm(*fields: tuple[int, int], size: int | None = None) -> bytes:
    """A valid file with u32 header fields overwritten (``(offset, value)`` pairs), then cut."""
    data = valid_npdm()
    for offset, value in fields:
        struct.pack_into("<I", data, offset, value)
    return bytes(data if size is None else data[:size])


def aci_first_npdm() -> bytearray:
    """META, then ACI0 at 0x80, then ACID at 0xC0: the two blocks in the other order."""
    data = bytearray(0x300)
    data[0x00:0x04] = b"META"
    data[0x0C] = 0x07
    struct.pack_into("<IIII", data, 0x70, 0x80, 0x40, 0xC0, 0x240)
    data[0x80:0x84] = b"ACI0"
    struct.pack_into("<Q", data, 0x80 + 0x10, APP_ID)
    data[0xC0 + 0x200 : 0xC0 + 0x204] = b"ACID"
    struct.pack_into("<QQ", data, 0xC0 + 0x210, APP_ID, APP_ID)
    return data


# ----------------------------------------------------------------------------------
# main.npdm: parsing
# ----------------------------------------------------------------------------------


def test_parse_npdm_reads_every_field_of_the_documented_layout() -> None:
    data = documented_npdm(APP_ID, APP_ID - 0x2000, APP_ID + 0x1FFF)
    # Fields the minimal layout leaves at zero, and noise in everything to be ignored.
    data[0x0F] = 2  # main thread core number
    struct.pack_into("<I", data, 0x14, 0x1E00000)  # system resource size
    struct.pack_into("<I", data, 0x18, 7)  # version
    data[0x30:0x40] = b"\xff" * 0x10  # product code
    data[0x80:0x280] = b"\xa5" * 0x200  # signature and public key
    parsed = npdm.parse_npdm(bytes(data))

    assert parsed.program_id == APP_ID
    assert parsed.title_id == "0100" + "ABCDEF012000"
    assert parsed.name == "Application"
    assert parsed.is_64bit is True
    assert parsed.address_space == 3
    assert parsed.system_resource_size == 0x1E00000
    assert parsed.main_thread_stack_size == 0x100000
    assert parsed.program_id_min == APP_ID - 0x2000
    assert parsed.program_id_max == APP_ID + 0x1FFF
    assert parsed.notes == ()


def test_program_id_is_stored_little_endian() -> None:
    data = documented_npdm(0, 0, U64_MAX)
    data[0x2D0:0x2D8] = bytes.fromhex("00 20 01 EF CD AB 00 01")
    assert npdm.parse_npdm(data).program_id == (0x0100 << 48) | 0xABCDEF012000


def test_parse_npdm_follows_the_header_offsets() -> None:
    # ACI0 before ACID, both at odd places, with padding between and after them.
    data = bytearray(0x400)
    data[0x00:0x04] = b"META"
    data[0x0C] = 0x07
    data[0x20:0x27] = b"Process"
    struct.pack_into("<IIII", data, 0x70, 0x94, 0x40, 0x124, 0x240)
    data[0x94:0x98] = b"ACI0"
    struct.pack_into("<Q", data, 0x94 + 0x10, APP_ID)
    data[0x124 + 0x200 : 0x124 + 0x204] = b"ACID"
    struct.pack_into("<QQ", data, 0x124 + 0x210, APP_ID - 0x2000, APP_ID + 0x2000)
    # Decoys where the minimal layout has its blocks (here: inside the public key and in
    # the padding): a reader that assumed fixed positions would pick these up.
    data[0x280:0x284] = b"ACID"
    struct.pack_into("<QQ", data, 0x290, OTHER_ID, OTHER_ID)
    data[0x2C0:0x2C4] = b"ACI0"
    struct.pack_into("<Q", data, 0x2D0, OTHER_ID)

    parsed = npdm.parse_npdm(data)
    assert parsed.program_id == APP_ID
    assert parsed.name == "Process"
    assert (parsed.program_id_min, parsed.program_id_max) == (APP_ID - 0x2000, APP_ID + 0x2000)
    assert parsed.notes == ()


def test_parse_npdm_ignores_trailing_padding_up_to_the_size_limit() -> None:
    reference = npdm.parse_npdm(valid_npdm())
    assert reference.notes == ()
    assert npdm.parse_npdm(valid_npdm() + bytes(0x100)) == reference
    assert npdm.parse_npdm(valid_npdm() + b"\xff" * (0x8000 - 0x300)) == reference

    assert npdm.MAX_NPDM_SIZE == 0x8000
    oversized = npdm.parse_npdm(valid_npdm() + bytes(0x8001 - 0x300))
    assert oversized.program_id == APP_ID
    assert len(oversized.notes) == 1
    assert "0x8001" in oversized.notes[0]
    assert "0x8000" in oversized.notes[0]


@pytest.mark.parametrize(
    ("program_id", "id_min", "id_max"),
    [
        pytest.param(APP_ID, APP_ID + 1, APP_ID + 0x1000, id="below the range"),
        pytest.param(APP_ID, APP_ID - 0x1000, APP_ID - 1, id="above the range"),
        pytest.param(APP_ID, APP_ID + 1, APP_ID - 1, id="inverted range"),
        pytest.param(APP_ID, 0, 0, id="range left at zero"),
    ],
)
def test_program_id_outside_the_acid_range_is_a_note(
    program_id: int, id_min: int, id_max: int
) -> None:
    parsed = npdm.parse_npdm(documented_npdm(program_id, id_min, id_max))
    assert parsed.program_id == program_id
    assert (parsed.program_id_min, parsed.program_id_max) == (id_min, id_max)
    assert len(parsed.notes) == 1
    assert "outside the range" in parsed.notes[0]
    assert f"{program_id:016X}" in parsed.notes[0]


@pytest.mark.parametrize(
    ("id_min", "id_max"),
    [(APP_ID, APP_ID), (APP_ID, APP_ID + 1), (APP_ID - 1, APP_ID), (0, U64_MAX)],
)
def test_program_id_inside_the_acid_range_is_fine(id_min: int, id_max: int) -> None:
    parsed = npdm.parse_npdm(documented_npdm(APP_ID, id_min, id_max))
    assert (parsed.program_id_min, parsed.program_id_max) == (id_min, id_max)
    assert parsed.notes == ()


def _npdm_with_acid_bytes(offset: int, replacement: bytes) -> bytes:
    data = valid_npdm()
    data[offset : offset + len(replacement)] = replacement
    return bytes(data)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(patched_npdm((0x78, 0), (0x7C, 0)), "no ACID block", id="missing"),
        pytest.param(_npdm_with_acid_bytes(0x280, b"ACIX"), "0x280", id="wrong magic"),
        pytest.param(_npdm_with_acid_bytes(0x280, b"acid"), "0x280", id="magic in lower case"),
        pytest.param(_npdm_with_acid_bytes(0x80, bytes(0x240)), "0x280", id="zeroed block"),
        pytest.param(_npdm_with_acid_bytes(0x80, b"\xff" * 0x240), "0x280", id="block of 0xFF"),
        pytest.param(patched_npdm((0x7C, 0x281)), "ends at 0x301", id="one byte too long"),
        pytest.param(patched_npdm((0x78, 0x300)), "ends at 0x540", id="offset at the end"),
        pytest.param(patched_npdm((0x78, 0xFFFFFFFF)), "ends at", id="offset far away"),
        pytest.param(patched_npdm((0x7C, 0xFFFFFFFF)), "ends at", id="huge size"),
        pytest.param(patched_npdm((0x7C, 0x23F)), "at least 0x240", id="too small"),
        pytest.param(patched_npdm((0x7C, 0)), "at least 0x240", id="empty"),
        pytest.param(patched_npdm((0x78, 0x7F)), "META header", id="inside the header"),
        pytest.param(patched_npdm((0x78, 0)), "META header", id="at offset zero"),
    ],
)
def test_unreadable_acid_block_is_a_note(data: bytes, reason: str) -> None:
    parsed = npdm.parse_npdm(data)
    assert parsed.program_id == APP_ID
    assert parsed.program_id_min is None
    assert parsed.program_id_max is None
    assert len(parsed.notes) == 1
    assert "ACID" in parsed.notes[0]
    assert reason in parsed.notes[0]


def test_truncating_the_acid_block_keeps_the_program_id() -> None:
    data = bytes(aci_first_npdm())
    assert npdm.parse_npdm(data).notes == ()
    # ACI0 ends at 0xC0: from there on only the ACID block is cut, which is not fatal.
    for size in range(0xC0, 0x300):
        parsed = npdm.parse_npdm(data[:size])
        assert parsed.program_id == APP_ID
        assert (parsed.program_id_min, parsed.program_id_max) == (None, None)
        assert len(parsed.notes) == 1, size
    for size in range(0xC0):
        with pytest.raises(FormatError):
            npdm.parse_npdm(data[:size])


def test_acid_block_with_3072_bit_signature_and_key() -> None:
    # The documentation gives a second layout: signature and key of 0x180 bytes each,
    # which puts the ACID header at +0x300 of its block.
    data = bytearray(0x80 + 0x340 + 0x40)
    data[0x00:0x04] = b"META"
    data[0x0C] = 0x07
    struct.pack_into("<IIII", data, 0x70, 0x3C0, 0x40, 0x80, 0x340)
    data[0x80:0x380] = b"\x5a" * 0x300  # signature and key
    data[0x380:0x384] = b"ACID"
    struct.pack_into("<QQ", data, 0x390, APP_ID, APP_ID + 0xFFF)
    data[0x3C0:0x3C4] = b"ACI0"
    struct.pack_into("<Q", data, 0x3D0, APP_ID + 1)

    parsed = npdm.parse_npdm(data)
    assert parsed.program_id == APP_ID + 1
    assert (parsed.program_id_min, parsed.program_id_max) == (APP_ID, APP_ID + 0xFFF)
    assert parsed.notes == ()

    # Without a magic at either place the note names both.
    data[0x380:0x384] = b"\x5a" * 4
    unreadable = npdm.parse_npdm(data)
    assert unreadable.program_id_min is None
    assert len(unreadable.notes) == 1
    assert "0x280" in unreadable.notes[0]
    assert "0x380" in unreadable.notes[0]


@pytest.mark.parametrize(
    ("flags0", "is_64bit", "address_space"),
    [
        (0x07, True, 3),
        (0x00, False, 0),
        (0x01, True, 0),
        (0x06, False, 3),
        (0x0B, True, 5),
        (0x0F, True, 7),
        (0xF7, True, 3),  # the upper bits are other flags
        (0xF0, False, 0),
    ],
)
def test_flags0_gives_the_architecture_and_the_address_space(
    flags0: int, is_64bit: bool, address_space: int
) -> None:
    data = valid_npdm()
    data[0x0C] = flags0
    parsed = npdm.parse_npdm(data)
    assert parsed.is_64bit is is_64bit
    assert parsed.address_space == address_space
    if is_64bit:
        assert parsed.notes == ()
    else:
        assert len(parsed.notes) == 1
        assert "32-bit" in parsed.notes[0]


def _npdm_with_wrong_aci_magic() -> bytes:
    data = valid_npdm()
    data[0x2C0:0x2C4] = b"ACI1"
    return bytes(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"", "truncated header", id="empty"),
        pytest.param(b"MET", "truncated header", id="cut inside the magic"),
        pytest.param(b"META", "truncated header", id="magic only"),
        pytest.param(patched_npdm(size=0x7F), "truncated header", id="header one byte short"),
        pytest.param(b"NOPE" + bytes(0x2FC), "wrong magic at offset 0x0", id="other magic"),
        pytest.param(b"meta" + bytes(0x2FC), "wrong magic at offset 0x0", id="lower case"),
        pytest.param(bytes(0x300), "wrong magic at offset 0x0", id="zeros"),
        pytest.param(b"XY", "wrong magic at offset 0x0", id="short and wrong"),
        pytest.param(patched_npdm((0x70, 0x300)), "ends at 0x340", id="ACI0 at the end"),
        pytest.param(patched_npdm((0x70, 0x2C1)), "ends at 0x301", id="ACI0 one byte out"),
        pytest.param(patched_npdm((0x70, 0xFFFFFFFF)), "ends at", id="ACI0 offset far away"),
        pytest.param(patched_npdm((0x74, 0xFFFFFFFF)), "ends at", id="ACI0 huge size"),
        pytest.param(patched_npdm(size=0x2FF), "ends at 0x300", id="file one byte short"),
        pytest.param(patched_npdm((0x74, 0x17)), "AciSize", id="ACI0 too small for the ID"),
        pytest.param(patched_npdm((0x74, 0)), "AciSize", id="ACI0 empty"),
        pytest.param(patched_npdm((0x70, 0), (0x74, 0)), "AciSize", id="no ACI0 at all"),
        pytest.param(_npdm_with_wrong_aci_magic(), "wrong magic at offset 0x2C0", id="ACI0 magic"),
        pytest.param(
            patched_npdm((0x70, 0x80)), "wrong magic at offset 0x80", id="ACI0 offset elsewhere"
        ),
    ],
)
def test_parse_npdm_rejects_what_hides_the_program_id(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=re.escape(message)) as caught:
        npdm.parse_npdm(data)
    assert str(caught.value).startswith("NPDM: ")


def test_every_truncation_of_the_minimal_npdm_is_a_format_error() -> None:
    data = bytes(valid_npdm())
    # ACI0 is the last block of this layout: no shorter prefix still holds it.
    for size in range(len(data)):
        with pytest.raises(FormatError):
            npdm.parse_npdm(data[:size])


def test_small_aci0_is_read_and_noted() -> None:
    # 0x18 bytes is all it takes to hold the program ID; a loader wants 0x40.
    data = bytearray(0x98)
    data[0x00:0x04] = b"META"
    data[0x0C] = 0x07
    struct.pack_into("<IIII", data, 0x70, 0x80, 0x18, 0, 0)
    data[0x80:0x84] = b"ACI0"
    struct.pack_into("<Q", data, 0x90, APP_ID)
    parsed = npdm.parse_npdm(data)
    assert parsed.program_id == APP_ID
    assert len(parsed.notes) == 2
    assert "ACI0" in parsed.notes[0]
    assert "0x18" in parsed.notes[0]
    assert "no ACID block" in parsed.notes[1]

    # One byte less than a loader wants is still noted; 0x40 bytes are not.
    struct.pack_into("<I", data, 0x74, 0x3F)
    with pytest.raises(FormatError, match="ends at 0xBF"):
        npdm.parse_npdm(data)
    almost = npdm.parse_npdm(data + bytes(0x27))
    assert almost.program_id == APP_ID
    assert len(almost.notes) == 2
    assert "0x3F" in almost.notes[0]
    struct.pack_into("<I", data, 0x74, 0x40)
    enough = npdm.parse_npdm(data + bytes(0x28))
    assert len(enough.notes) == 1
    assert "no ACID block" in enough.notes[0]


def test_aci0_inside_the_meta_header_is_read_and_noted() -> None:
    data = valid_npdm()
    # A loader refuses a block that overlaps the header; the ID is still readable.
    data[0x40:0x44] = b"ACI0"
    struct.pack_into("<Q", data, 0x50, APP_ID)
    struct.pack_into("<I", data, 0x70, 0x40)
    parsed = npdm.parse_npdm(data)
    assert parsed.program_id == APP_ID
    assert len(parsed.notes) == 1
    assert "inside the META header" in parsed.notes[0]
    assert "0x40" in parsed.notes[0]


@pytest.mark.parametrize(
    ("field", "name"),
    [
        (b"Application".ljust(16, b"\x00"), "Application"),
        (b"SixteenByteName!", "SixteenByteName!"),  # fills the field: no terminator
        (b"Short\x00leftover\x00\x00", "Short"),  # the text ends at the first NUL
        (bytes(16), ""),
        (b"caf\xc3\xa9".ljust(16, b"\x00"), "café"),
        (b"bad\xffbyte".ljust(16, b"\x00"), "bad�byte"),
    ],
)
def test_process_name_is_nul_padded_text(field: bytes, name: str) -> None:
    data = valid_npdm()
    data[0x20:0x30] = field
    parsed = npdm.parse_npdm(data)
    assert parsed.name == name
    assert parsed.notes == ()


@pytest.mark.parametrize(
    ("program_id", "title_id"),
    [
        (0, "0000000000000000"),
        (0xABC, "0000000000000ABC"),
        (0xDEADBEEF, "00000000DEADBEEF"),
        ((0x0100 << 48) | 0xABCDEF012000, "0100" + "ABCDEF012000"),
        (0x0FEDCBA987654321, "0FEDCBA987654321"),
        (U64_MAX, "FFFFFFFFFFFFFFFF"),
    ],
)
def test_title_id_is_16_uppercase_hex_digits(program_id: int, title_id: str) -> None:
    parsed = npdm.parse_npdm(documented_npdm(program_id, 0, U64_MAX))
    assert parsed.program_id == program_id
    assert parsed.title_id == title_id


def test_is_npdm_looks_at_the_magic_only() -> None:
    assert npdm.NPDM_MAGIC == b"META"
    assert npdm.is_npdm(b"META")
    assert npdm.is_npdm(bytes(valid_npdm()))
    assert npdm.is_npdm(b"META and anything else")
    assert not npdm.is_npdm(b"")
    assert not npdm.is_npdm(b"MET")
    assert not npdm.is_npdm(b"meta")
    assert not npdm.is_npdm(b"ACI0" + bytes(0x80))
    assert not npdm.is_npdm(b"\x00META")


def test_parse_npdm_accepts_bytes_bytearray_and_memoryview() -> None:
    data = valid_npdm()
    reference = npdm.parse_npdm(bytes(data))
    assert npdm.parse_npdm(data) == reference
    assert npdm.parse_npdm(memoryview(data)) == reference
    padded = b"\xee" * 5 + bytes(data) + b"\xee" * 5
    assert npdm.parse_npdm(memoryview(padded)[5:-5]) == reference
    with pytest.raises(TypeError):
        npdm.parse_npdm("META")  # type: ignore[arg-type]


def test_npdm_result_is_immutable_and_hashable() -> None:
    parsed = npdm.parse_npdm(valid_npdm())
    with pytest.raises(AttributeError):
        parsed.program_id = 1  # type: ignore[misc]
    assert hash(parsed) == hash(npdm.parse_npdm(valid_npdm()))


def test_hostile_npdm_headers_never_crash_the_parser() -> None:
    rng = random.Random(0xBEA)
    interesting = [0, 1, 0x17, 0x18, 0x3F, 0x40, 0x7F, 0x80, 0x23F, 0x240, 0x2C0, 0x2FF, 0x300]
    interesting += [0x301, 0x33F, 0x340, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF]
    parsed_some = False
    for _ in range(2000):
        data = valid_npdm() + bytes(rng.choice([0, 0, 0x40, 0x100]))
        for offset in rng.sample([0x70, 0x74, 0x78, 0x7C], rng.randint(1, 4)):
            value = rng.choice(interesting) if rng.random() < 0.8 else rng.getrandbits(32)
            struct.pack_into("<I", data, offset, value)
        cut = rng.choice([len(data), len(data), rng.randrange(len(data) + 1)])
        try:
            parsed = npdm.parse_npdm(bytes(data[:cut]))
        except FormatError:
            continue  # the only failure a file may cause
        parsed_some = True
        assert 0 <= parsed.program_id <= U64_MAX
        assert len(parsed.title_id) == 16
        assert (parsed.program_id_min is None) == (parsed.program_id_max is None)
    assert parsed_some


# ----------------------------------------------------------------------------------
# main.npdm: reading files
# ----------------------------------------------------------------------------------


def test_read_npdm_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "exefs" / "main.npdm"
    path.parent.mkdir()
    path.write_bytes(documented_npdm(APP_ID, APP_ID, APP_ID + 0x1FFF))
    parsed = npdm.read_npdm(path)
    assert parsed.program_id == APP_ID
    assert (parsed.program_id_min, parsed.program_id_max) == (APP_ID, APP_ID + 0x1FFF)
    assert parsed.notes == ()
    assert npdm.read_npdm(str(path)) == parsed


def test_read_npdm_reports_bad_and_missing_files(tmp_path: Path) -> None:
    not_npdm = tmp_path / "main.npdm"
    not_npdm.write_bytes(b"PFS0" + bytes(0x200))
    with pytest.raises(FormatError, match="wrong magic"):
        npdm.read_npdm(not_npdm)
    empty = tmp_path / "empty.npdm"
    empty.write_bytes(b"")
    with pytest.raises(FormatError, match="truncated header"):
        npdm.read_npdm(empty)
    with pytest.raises(FileNotFoundError):
        npdm.read_npdm(tmp_path / "missing.npdm")


def test_read_npdm_refuses_a_file_over_one_mebibyte(tmp_path: Path) -> None:
    valid = bytes(valid_npdm())
    at_limit = tmp_path / "limit.npdm"
    at_limit.write_bytes(valid + bytes(MIB - len(valid)))
    parsed = npdm.read_npdm(at_limit)
    assert parsed.program_id == APP_ID
    assert len(parsed.notes) == 1  # far beyond what a loader accepts, but still read
    assert "0x100000" in parsed.notes[0]

    over = tmp_path / "over.npdm"
    over.write_bytes(valid + bytes(MIB + 1 - len(valid)))
    with pytest.raises(FormatError, match="1 MiB"):
        npdm.read_npdm(over)


def test_read_npdm_copes_with_a_path_beyond_260_characters(tmp_path: Path) -> None:
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/main.npdm"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, valid_npdm())
    assert npdm.read_npdm(target).program_id == APP_ID


# ----------------------------------------------------------------------------------
# main.npdm: the writer
# ----------------------------------------------------------------------------------


def test_build_npdm_matches_the_documented_layout_byte_by_byte() -> None:
    built = npdm.build_npdm(APP_ID)
    assert isinstance(built, bytes)
    assert len(built) == 0x300
    expected = bytes(documented_npdm(APP_ID, APP_ID, APP_ID))
    differing = [offset for offset in range(0x300) if built[offset] != expected[offset]]
    assert differing == []
    assert built == expected
    # The byte order of the ID, spelled out once.
    assert built[0x2D0:0x2D8] == bytes.fromhex("00 20 01 EF CD AB 00 01")


def test_build_npdm_options_land_in_their_fields() -> None:
    built = npdm.build_npdm(
        APP_ID,
        name="Synthetic",
        is_64bit=False,
        address_space=0,
        program_id_min=APP_ID - 0x1000,
        program_id_max=APP_ID + 0xFFF,
    )
    assert built[0x0C] == 0x00
    assert built[0x20:0x30] == b"Synthetic" + bytes(7)
    assert struct.unpack_from("<Q", built, 0x290) == (APP_ID - 0x1000,)
    assert struct.unpack_from("<Q", built, 0x298) == (APP_ID + 0xFFF,)
    assert struct.unpack_from("<Q", built, 0x2D0) == (APP_ID,)

    expected = documented_npdm(APP_ID, APP_ID - 0x1000, APP_ID + 0xFFF)
    expected[0x0C] = 0x00
    expected[0x20:0x30] = b"Synthetic" + bytes(7)
    assert built == bytes(expected)


@pytest.mark.parametrize(
    ("is_64bit", "address_space", "flags0"),
    [
        (True, 3, 0x07),
        (False, 3, 0x06),
        (True, 0, 0x01),
        (False, 0, 0x00),
        (True, 1, 0x03),
        (True, 4, 0x09),
        (True, 7, 0x0F),
    ],
)
def test_build_npdm_packs_flags0(is_64bit: bool, address_space: int, flags0: int) -> None:
    built = npdm.build_npdm(APP_ID, is_64bit=is_64bit, address_space=address_space)
    assert built[0x0C] == flags0
    expected = valid_npdm()
    expected[0x0C] = flags0
    assert built == bytes(expected)


def test_build_npdm_accepts_the_extremes() -> None:
    assert npdm.build_npdm(0) == bytes(documented_npdm(0, 0, 0))
    assert npdm.build_npdm(U64_MAX) == bytes(documented_npdm(U64_MAX, U64_MAX, U64_MAX))
    # Limits that do not enclose the ID are written as given: a test may want such a file.
    assert npdm.build_npdm(5, program_id_min=9, program_id_max=7) == bytes(documented_npdm(5, 9, 7))
    longest = npdm.build_npdm(APP_ID, name="FifteenByteName")
    assert longest[0x20:0x30] == b"FifteenByteName\x00"
    assert npdm.build_npdm(APP_ID, name="")[0x20:0x30] == bytes(16)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"program_id": -1}, id="negative ID"),
        pytest.param({"program_id": 1 << 64}, id="ID of 65 bits"),
        pytest.param({"program_id": APP_ID, "program_id_min": -1}, id="negative minimum"),
        pytest.param({"program_id": APP_ID, "program_id_max": 1 << 64}, id="maximum of 65 bits"),
        pytest.param({"program_id": APP_ID, "name": "SixteenByteName!"}, id="name of 16 bytes"),
        pytest.param({"program_id": APP_ID, "name": "x" * 40}, id="long name"),
        pytest.param({"program_id": APP_ID, "name": "café"}, id="name that is not ASCII"),
        pytest.param({"program_id": APP_ID, "name": "a\x00b"}, id="name with a NUL"),
        pytest.param({"program_id": APP_ID, "address_space": -1}, id="negative address space"),
        pytest.param({"program_id": APP_ID, "address_space": 8}, id="address space of 4 bits"),
    ],
)
def test_build_npdm_rejects_bad_values(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        npdm.build_npdm(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "arguments",
    [
        {"program_id": "0"},
        {"program_id": 1.0},
        {"program_id": True},
        {"program_id": APP_ID, "program_id_min": "0"},
        {"program_id": APP_ID, "name": b"Application"},
        {"program_id": APP_ID, "address_space": "3"},
    ],
)
def test_build_npdm_rejects_wrong_types(arguments: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        npdm.build_npdm(**arguments)  # type: ignore[arg-type]


def test_built_npdm_is_read_back_by_the_parser() -> None:
    parsed = npdm.parse_npdm(
        npdm.build_npdm(
            APP_ID + 1,
            name="Synthetic",
            is_64bit=False,
            address_space=2,
            program_id_min=APP_ID,
            program_id_max=APP_ID + 0x1FFF,
        )
    )
    assert parsed.program_id == APP_ID + 1
    assert parsed.name == "Synthetic"
    assert parsed.is_64bit is False
    assert parsed.address_space == 2
    assert parsed.system_resource_size == 0
    assert parsed.main_thread_stack_size == 0x100000
    assert (parsed.program_id_min, parsed.program_id_max) == (APP_ID, APP_ID + 0x1FFF)
    assert len(parsed.notes) == 1  # 32-bit code
    assert npdm.parse_npdm(npdm.build_npdm(APP_ID)).notes == ()
    assert npdm.is_npdm(npdm.build_npdm(APP_ID))


# ----------------------------------------------------------------------------------
# control.nacp: inputs built by hand
# ----------------------------------------------------------------------------------


def blank_nacp() -> bytearray:
    """A complete file with every byte zero: no title, no version, no ID."""
    return bytearray(0x4000)


def put_title(block: bytearray, index: int, name: bytes, publisher: bytes = b"") -> None:
    """Write title entry ``index``: the name at +0x000 of the entry, the publisher at +0x200."""
    assert len(name) <= 0x200
    assert len(publisher) <= 0x100
    start = index * 0x300
    block[start : start + len(name)] = name
    block[start + 0x200 : start + 0x200 + len(publisher)] = publisher


def raw_deflate(data: bytes) -> bytes:
    """Compress with DEFLATE alone (RFC 1951): no zlib header, no checksum."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def stored_deflate(data: bytes) -> bytes:
    """A DEFLATE stream made by hand: one final "stored" block holding ``data`` as it is."""
    assert len(data) <= 0xFFFF
    return b"\x01" + struct.pack("<HH", len(data), len(data) ^ 0xFFFF) + data


def compressed_nacp(stream: bytes, *, data_size: int | None = None, flag: int = 1) -> bytearray:
    """A file whose title block is ``stream`` behind its u16 DataSize, with the format byte."""
    assert len(stream) <= 0x2FFE
    data = blank_nacp()
    struct.pack_into("<H", data, 0x0000, len(stream) if data_size is None else data_size)
    data[0x0002 : 0x0002 + len(stream)] = stream
    data[0x3215] = flag
    return data


def three_title_block() -> bytearray:
    """The 32 entries of a compressed block: American English, Polish and Thai filled."""
    block = bytearray(0x6000)
    put_title(block, 0, b"Synthetic Game", b"Example Publisher")
    put_title(block, 16, POLISH_NAME.encode(), POLISH_PUBLISHER.encode())
    put_title(block, 17, THAI_NAME.encode(), THAI_PUBLISHER.encode())
    return block


THREE_TITLES = (
    nacp.NacpTitle(0, "AmericanEnglish", "Synthetic Game", "Example Publisher"),
    nacp.NacpTitle(16, "Polish", POLISH_NAME, POLISH_PUBLISHER),
    nacp.NacpTitle(17, "Thai", THAI_NAME, THAI_PUBLISHER),
)


def nacp_with_ids(
    *,
    save_data_owner: int = 0,
    presence_group: int = 0,
    add_on_content_base: int = 0,
    local_communication: int = 0,
    seed: int = 0,
    program_index: int = 0,
) -> nacp.Nacp:
    """Parse a file that holds nothing but the fields the ID inference looks at."""
    data = blank_nacp()
    struct.pack_into("<Q", data, 0x3078, save_data_owner)
    struct.pack_into("<Q", data, 0x3038, presence_group)
    struct.pack_into("<Q", data, 0x3070, add_on_content_base)
    struct.pack_into("<Q", data, 0x30B0, local_communication)
    struct.pack_into("<Q", data, 0x30F8, seed)
    data[0x3212] = program_index
    return nacp.parse_nacp(data)


# ----------------------------------------------------------------------------------
# control.nacp: languages
# ----------------------------------------------------------------------------------


def test_languages_are_in_index_order() -> None:
    assert nacp.LANGUAGES == (
        "AmericanEnglish",
        "BritishEnglish",
        "Japanese",
        "French",
        "German",
        "LatinAmericanSpanish",
        "Spanish",
        "Italian",
        "Dutch",
        "CanadianFrench",
        "Portuguese",
        "Russian",
        "Korean",
        "TraditionalChinese",
        "SimplifiedChinese",
        "BrazilianPortuguese",
        "Polish",
        "Thai",
    )
    assert nacp.NACP_SIZE == 0x4000
    assert nacp.TITLE_ENTRY_SIZE == 0x300


def test_language_name_covers_every_index() -> None:
    assert nacp.language_name(0) == "AmericanEnglish"
    assert nacp.language_name(2) == "Japanese"
    assert nacp.language_name(15) == "BrazilianPortuguese"
    assert nacp.language_name(16) == "Polish"
    assert nacp.language_name(17) == "Thai"
    assert nacp.language_name(18) == "Language18"
    assert nacp.language_name(31) == "Language31"
    assert [nacp.language_name(index) for index in range(18)] == list(nacp.LANGUAGES)
    assert len({nacp.language_name(index) for index in range(32)}) == 32
    for index in (-1, 32, 100):
        with pytest.raises(ValueError):
            nacp.language_name(index)


# ----------------------------------------------------------------------------------
# control.nacp: parsing a plain file
# ----------------------------------------------------------------------------------


def test_parse_plain_nacp_reads_every_field() -> None:
    # A zeroed title block, then 0xFF wherever no documented field is written below:
    # anything read from the wrong offset, or one byte too far, shows.
    data = bytearray(0x3000) + bytearray(b"\xff" * 0x1000)
    put_title(data, 0, b"Synthetic Game", b"Example Publisher")
    put_title(data, 2, JAPANESE_NAME.encode(), JAPANESE_PUBLISHER.encode())
    struct.pack_into("<I", data, 0x302C, 0x8005)  # SupportedLanguageFlag: bits 0, 2, 15
    struct.pack_into("<Q", data, 0x3038, APP_ID)  # PresenceGroupId
    data[0x3060:0x3070] = b"1.2.3" + bytes(11)  # DisplayVersion
    struct.pack_into("<Q", data, 0x3070, APP_ID + 0x1000)  # AddOnContentBaseId
    struct.pack_into("<Q", data, 0x3078, OTHER_ID)  # SaveDataOwnerId
    local_communication_ids = tuple(THIRD_ID + index for index in range(8))
    struct.pack_into("<8Q", data, 0x30B0, *local_communication_ids)  # LocalCommunicationId
    struct.pack_into("<Q", data, 0x30F8, 0x0123456789ABCDEF)  # SeedForPseudoDeviceId
    data[0x3212] = 2  # ProgramIndex
    data[0x3215] = 0  # TitlesDataFormat: plain
    parsed = nacp.parse_nacp(bytes(data))

    assert parsed.titles == (
        nacp.NacpTitle(0, "AmericanEnglish", "Synthetic Game", "Example Publisher"),
        nacp.NacpTitle(2, "Japanese", JAPANESE_NAME, JAPANESE_PUBLISHER),
    )
    assert parsed.display_version == "1.2.3"
    assert parsed.supported_languages == ("AmericanEnglish", "Japanese", "BrazilianPortuguese")
    assert parsed.presence_group_id == APP_ID
    assert parsed.add_on_content_base_id == APP_ID + 0x1000
    assert parsed.save_data_owner_id == OTHER_ID
    assert parsed.local_communication_ids == local_communication_ids
    assert parsed.seed_for_pseudo_device_id == 0x0123456789ABCDEF
    assert parsed.program_index == 2
    assert parsed.titles_compressed is False
    assert parsed.notes == ()

    assert parsed.title() == parsed.titles[0]
    assert parsed.title("AmericanEnglish") == parsed.titles[0]
    assert parsed.title("Japanese") == parsed.titles[1]
    assert parsed.title("German") == parsed.titles[0]  # not in the file: the first entry


def test_title_text_is_utf8_given_byte_by_byte() -> None:
    data = blank_nacp()
    put_title(data, 3, b"Jeu Synth\xc3\xa9tique", b"\xc3\x89diteur Exemple")
    put_title(data, 12, b"\xea\xb2\x8c\xec\x9e\x84", b"\xed\x9a\x8c\xec\x82\xac")
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == (
        nacp.NacpTitle(3, "French", "Jeu Synthétique", "Éditeur Exemple"),
        nacp.NacpTitle(12, "Korean", "게임", "회사"),
    )
    assert parsed.notes == ()


def test_title_falls_back_to_the_first_entry_in_index_order() -> None:
    data = blank_nacp()
    put_title(data, 5, b"Juego Sint\xc3\xa9tico", b"Editor de Ejemplo")
    alone = nacp.parse_nacp(data)
    only = nacp.NacpTitle(5, "LatinAmericanSpanish", "Juego Sintético", "Editor de Ejemplo")
    assert alone.titles == (only,)
    assert alone.title() == only
    assert alone.title("Japanese") == only
    assert alone.title("LatinAmericanSpanish") == only

    put_title(data, 9, b"Jeu Synth\xc3\xa9tique", b"\xc3\x89diteur Exemple")
    put_title(data, 15, b"Jogo Sint\xc3\xa9tico", b"Editora Exemplo")
    several = nacp.parse_nacp(data)
    assert [entry.index for entry in several.titles] == [5, 9, 15]
    assert several.title() == only
    assert several.title(None) == only
    assert several.title("CanadianFrench") == several.titles[1]
    assert several.title("BrazilianPortuguese") == several.titles[2]
    assert several.title("Thai") == only


def test_title_rejects_a_name_that_is_not_a_language() -> None:
    data = blank_nacp()
    put_title(data, 0, b"Synthetic Game")
    parsed = nacp.parse_nacp(data)
    for name in ("", "English", "americanenglish", "en-US", "Language32"):
        with pytest.raises(ValueError, match="unknown language"):
            parsed.title(name)
    assert parsed.title("Language31") == parsed.titles[0]  # a valid name, just not in the file


def test_nacp_without_titles() -> None:
    parsed = nacp.parse_nacp(blank_nacp())
    assert parsed.titles == ()
    assert parsed.title() is None
    assert parsed.title("Japanese") is None
    assert parsed.display_version == ""
    assert parsed.supported_languages == ()
    assert parsed.presence_group_id == 0
    assert parsed.add_on_content_base_id == 0
    assert parsed.save_data_owner_id == 0
    assert parsed.local_communication_ids == (0,) * 8
    assert parsed.seed_for_pseudo_device_id == 0
    assert parsed.program_index == 0
    assert parsed.titles_compressed is False
    assert parsed.notes == ()
    assert parsed.inferred_application_id() is None
    assert parsed.inferred_program_id() is None


def test_entry_without_a_name_is_absent() -> None:
    data = blank_nacp()
    put_title(data, 0, b"", b"Publisher Without A Name")
    put_title(data, 1, b"\x00Hidden After The Terminator", b"Example Publisher")
    put_title(data, 4, b"Synthetisches Spiel", b"")
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == (nacp.NacpTitle(4, "German", "Synthetisches Spiel", ""),)
    assert parsed.notes == ()


def test_names_and_publishers_may_fill_their_fields() -> None:
    data = blank_nacp()
    # 0x1FF bytes and the terminator; a publisher that takes all 0x100 bytes.
    put_title(data, 0, b"A" * 0x1FF, b"P" * 0x100)
    # 0x200 bytes: no terminator, the publisher field starts right behind.
    put_title(data, 1, b"B" * 0x200, b"Q" * 5)
    # Only the text before the first NUL counts, whatever follows it.
    put_title(data, 2, b"Short\x00leftover", b"Pub\x00leftover")
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == (
        nacp.NacpTitle(0, "AmericanEnglish", "A" * 0x1FF, "P" * 0x100),
        nacp.NacpTitle(1, "BritishEnglish", "B" * 0x200, "Q" * 5),
        nacp.NacpTitle(2, "Japanese", "Short", "Pub"),
    )
    assert parsed.notes == ()


def test_all_sixteen_plain_entries_are_read_and_no_more() -> None:
    data = blank_nacp()
    for index in range(16):
        put_title(data, index, f"Name {index}".encode(), f"Publisher {index}".encode())
    # Bits 16 and 17 may be set in the flags, but a plain block has no such entries.
    struct.pack_into("<I", data, 0x302C, 0xFFFFFFFF)
    parsed = nacp.parse_nacp(data)
    assert [(entry.index, entry.language) for entry in parsed.titles] == list(
        enumerate(nacp.LANGUAGES[:16])
    )
    assert parsed.titles[15] == nacp.NacpTitle(15, "BrazilianPortuguese", "Name 15", "Publisher 15")
    assert parsed.supported_languages == (
        *nacp.LANGUAGES,
        *(f"Language{index}" for index in range(18, 32)),
    )


@pytest.mark.parametrize(
    ("flag", "languages"),
    [
        (0x00000000, ()),
        (0x00000001, ("AmericanEnglish",)),
        (0x00008005, ("AmericanEnglish", "Japanese", "BrazilianPortuguese")),
        (0x00030000, ("Polish", "Thai")),
        (0x80040000, ("Language18", "Language31")),
    ],
)
def test_supported_languages_follow_the_flag_bits(flag: int, languages: tuple[str, ...]) -> None:
    data = blank_nacp()
    struct.pack_into("<I", data, 0x302C, flag)
    assert nacp.parse_nacp(data).supported_languages == languages


@pytest.mark.parametrize(
    ("field", "version"),
    [
        (b"1.2.3", "1.2.3"),
        (b"1.0.1a", "1.0.1a"),
        (b"1.0.0.2", "1.0.0.2"),
        (b"1.0.0-synthetic1", "1.0.0-synthetic1"),  # all 16 bytes: no terminator
        (b" 1.0 ", " 1.0 "),  # verbatim: nothing is stripped
        (b"1.0\x00junk", "1.0"),
        (b"", ""),
    ],
)
def test_display_version_is_kept_verbatim(field: bytes, version: str) -> None:
    data = blank_nacp()
    data[0x3060 : 0x3060 + len(field)] = field
    # AddOnContentBaseId starts right behind the field: printable bytes that must not leak.
    data[0x3070:0x3078] = b"ABCDEFGH"
    parsed = nacp.parse_nacp(data)
    assert parsed.display_version == version
    assert parsed.add_on_content_base_id == int.from_bytes(b"ABCDEFGH", "little")
    assert parsed.notes == ()


def test_text_that_is_not_utf8_is_replaced_and_noted() -> None:
    data = blank_nacp()
    put_title(data, 0, b"Synth\xffGame", b"Example\xc3")  # a stray byte; a sequence cut short
    put_title(data, 1, b"Plain Name", b"Plain Publisher")
    put_title(data, 2, b"x" * 0x1FF + b"\xe3", b"Fine")  # a character cut by the field end
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == (
        nacp.NacpTitle(0, "AmericanEnglish", "Synth�Game", "Example�"),
        nacp.NacpTitle(1, "BritishEnglish", "Plain Name", "Plain Publisher"),
        nacp.NacpTitle(2, "Japanese", "x" * 0x1FF + "�", "Fine"),
    )
    assert len(parsed.notes) == 3
    assert all("UTF-8" in note for note in parsed.notes)
    assert "name of title entry 0 (AmericanEnglish)" in parsed.notes[0]
    assert "publisher of title entry 0 (AmericanEnglish)" in parsed.notes[1]
    assert "name of title entry 2 (Japanese)" in parsed.notes[2]


def test_display_version_that_is_not_ascii_is_replaced_and_noted() -> None:
    data = blank_nacp()
    data[0x3060:0x3065] = b"1.0\xc3\xa9"
    parsed = nacp.parse_nacp(data)
    assert parsed.display_version == "1.0��"
    assert len(parsed.notes) == 1
    assert "display version" in parsed.notes[0]


def test_whitespace_in_titles_is_kept() -> None:
    data = blank_nacp()
    put_title(data, 0, b"  Spaced Name \t", b" Spaced Publisher\n")
    assert nacp.parse_nacp(data).titles == (
        nacp.NacpTitle(0, "AmericanEnglish", "  Spaced Name \t", " Spaced Publisher\n"),
    )


# ----------------------------------------------------------------------------------
# control.nacp: compressed title blocks
# ----------------------------------------------------------------------------------


def test_parse_compressed_nacp() -> None:
    stream = raw_deflate(bytes(three_title_block()))
    data = compressed_nacp(stream)
    # What lies behind DataSize is not part of the stream.
    data[2 + len(stream) : 2 + len(stream) + 16] = b"\xee" * 16
    struct.pack_into("<I", data, 0x302C, (1 << 0) | (1 << 16) | (1 << 17))
    data[0x3060:0x3065] = b"2.0.0"
    struct.pack_into("<Q", data, 0x3078, APP_ID)
    parsed = nacp.parse_nacp(data)

    assert parsed.titles_compressed is True
    assert parsed.titles == THREE_TITLES
    assert parsed.supported_languages == ("AmericanEnglish", "Polish", "Thai")
    assert parsed.display_version == "2.0.0"
    assert parsed.save_data_owner_id == APP_ID
    assert parsed.notes == ()
    assert parsed.title() == THREE_TITLES[0]
    assert parsed.title("Polish") == THREE_TITLES[1]
    assert parsed.title("Thai") == THREE_TITLES[2]


def test_compressed_block_holds_all_32_entries() -> None:
    block = bytearray(0x6000)
    for index in range(32):
        put_title(block, index, f"Name {index}".encode(), f"Publisher {index}".encode())
    parsed = nacp.parse_nacp(compressed_nacp(raw_deflate(bytes(block))))
    assert [entry.index for entry in parsed.titles] == list(range(32))
    assert parsed.titles[17] == nacp.NacpTitle(17, "Thai", "Name 17", "Publisher 17")
    assert parsed.titles[18] == nacp.NacpTitle(18, "Language18", "Name 18", "Publisher 18")
    assert parsed.titles[31] == nacp.NacpTitle(31, "Language31", "Name 31", "Publisher 31")
    assert parsed.title("Language18") == parsed.titles[18]
    assert parsed.notes == ()


def test_compressed_block_without_any_title() -> None:
    parsed = nacp.parse_nacp(compressed_nacp(raw_deflate(bytes(0x6000))))
    assert parsed.titles == ()
    assert parsed.titles_compressed is True
    assert parsed.title() is None
    assert parsed.notes == ()


def test_compressed_block_is_found_when_the_format_byte_is_left_at_zero() -> None:
    data = compressed_nacp(raw_deflate(bytes(three_title_block())), flag=0)
    parsed = nacp.parse_nacp(data)
    assert parsed.titles_compressed is True
    assert parsed.titles == THREE_TITLES
    assert len(parsed.notes) == 1
    assert "TitlesDataFormat" in parsed.notes[0]
    assert "compressed" in parsed.notes[0]


def test_plain_block_is_not_mistaken_for_a_compressed_one() -> None:
    # A plain name whose first two bytes read as a small DataSize, followed by bytes that
    # are a complete DEFLATE stream: it inflates to 5 bytes, not to 32 entries.
    stream = stored_deflate(b"hello")
    data = compressed_nacp(stream, flag=0)
    assert bytes(data[:2]) == b"\x0a\x00"
    parsed = nacp.parse_nacp(data)
    assert parsed.titles_compressed is False
    assert parsed.titles == (nacp.NacpTitle(0, "AmericanEnglish", "\n", ""),)
    assert parsed.notes == ()

    # One-character names: DataSize 0x41, then the zero padding of the name.
    plain = blank_nacp()
    put_title(plain, 0, b"A", b"B")
    parsed = nacp.parse_nacp(plain)
    assert parsed.titles_compressed is False
    assert parsed.titles == (nacp.NacpTitle(0, "AmericanEnglish", "A", "B"),)
    assert parsed.notes == ()


def _valid_stream() -> bytes:
    return raw_deflate(bytes(three_title_block()))


#: Title blocks that are not valid compressed blocks: ``(stream, DataSize or None)``.
BAD_COMPRESSED_BLOCKS = [
    pytest.param(b"\x07" + bytes(8), None, id="reserved block type"),
    pytest.param(b"\x01\x10\x00\x00\x00" + bytes(16), None, id="stored block with bad length"),
    pytest.param(b"\xff" * 64, None, id="garbage"),
    pytest.param(zlib.compress(bytes(three_title_block()), 9), None, id="zlib wrapper"),
    pytest.param(_valid_stream(), 0, id="DataSize 0"),
    pytest.param(_valid_stream(), 0x2FFF, id="DataSize 0x2FFF"),
    pytest.param(_valid_stream(), 0xFFFF, id="DataSize 0xFFFF"),
    pytest.param(_valid_stream(), len(_valid_stream()) - 1, id="stream cut by one byte"),
    pytest.param(_valid_stream(), len(_valid_stream()) // 2, id="stream cut in half"),
    pytest.param(_valid_stream(), 1, id="stream cut to one byte"),
    pytest.param(_valid_stream() + b"\x00", None, id="one byte after the stream"),
    pytest.param(_valid_stream() + b"trailing garbage", None, id="garbage after the stream"),
    pytest.param(_valid_stream(), len(_valid_stream()) + 1, id="DataSize one byte too long"),
    pytest.param(raw_deflate(bytes(0x5FFF)), None, id="inflates to 0x5FFF bytes"),
    pytest.param(raw_deflate(bytes(0x6001)), None, id="inflates to 0x6001 bytes"),
    pytest.param(raw_deflate(bytes(0x3000)), None, id="inflates to 16 entries"),
    pytest.param(raw_deflate(bytes(0x300)), None, id="inflates to one entry"),
    pytest.param(raw_deflate(b""), None, id="inflates to nothing"),
    pytest.param(stored_deflate(b"hello"), None, id="inflates to five bytes"),
    pytest.param(raw_deflate(bytes(0xC000)), None, id="inflates to twice the size"),
]


@pytest.mark.parametrize(("stream", "data_size"), BAD_COMPRESSED_BLOCKS)
def test_invalid_compressed_block_is_a_format_error(stream: bytes, data_size: int | None) -> None:
    data = compressed_nacp(stream, data_size=data_size, flag=1)
    with pytest.raises(FormatError, match="compressed title block") as caught:
        nacp.parse_nacp(data)
    assert str(caught.value).startswith("NACP: ")


@pytest.mark.parametrize(("stream", "data_size"), BAD_COMPRESSED_BLOCKS)
def test_invalid_compressed_block_is_plain_data_when_the_format_byte_is_zero(
    stream: bytes, data_size: int | None
) -> None:
    # With the byte at 0 the inflation is only an attempt: its failure is not an error.
    parsed = nacp.parse_nacp(compressed_nacp(stream, data_size=data_size, flag=0))
    assert parsed.titles_compressed is False
    assert not any("compressed" in note for note in parsed.notes)
    assert all(entry.index < 16 for entry in parsed.titles)


def stream_of_exact_size(block: bytes, size: int) -> bytes:
    """A DEFLATE stream of exactly ``size`` bytes that inflates to ``block``.

    It is made of a compressed part, as many empty stored blocks as it takes (five bytes
    each, no output, not final) and a final stored block holding the last bytes of
    ``block`` as they are; the length of that last part is what makes the sum come out.
    """
    for tail in range(64):
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        head = compressor.compress(block[: len(block) - tail])
        head += compressor.flush(zlib.Z_SYNC_FLUSH)  # byte-aligned, and no final block yet
        final = stored_deflate(block[len(block) - tail :])
        room = size - len(head) - len(final)
        if room >= 0 and room % 5 == 0:
            return head + b"\x00\x00\x00\xff\xff" * (room // 5) + final
    raise AssertionError(f"no stream of {size} bytes found")


def test_data_size_may_be_as_large_as_the_block_allows() -> None:
    block = bytes(three_title_block())
    largest = stream_of_exact_size(block, 0x2FFE)
    assert len(largest) == 0x2FFE
    assert zlib.decompress(largest, wbits=-15) == block  # the stream made by hand is sound
    data = compressed_nacp(largest)
    assert struct.unpack_from("<H", data, 0) == (0x2FFE,)
    assert data[0x2FFF] == largest[-1]  # it ends with the title block
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == THREE_TITLES
    assert parsed.titles_compressed is True
    assert parsed.notes == ()

    # The same block, found without the format byte.
    data[0x3215] = 0
    unannounced = nacp.parse_nacp(data)
    assert unannounced.titles == THREE_TITLES
    assert len(unannounced.notes) == 1

    # Several blocks in one stream are nothing special either.
    smaller = stream_of_exact_size(block, 0x800)
    assert nacp.parse_nacp(compressed_nacp(smaller)).titles == THREE_TITLES


def test_deflate_bomb_fails_without_being_inflated() -> None:
    # 8 MiB of zeros deflate to about 8 KiB, which fits the title block.
    bomb = raw_deflate(bytes(8 * MIB))
    assert len(bomb) <= 0x2FFE
    announced = bytes(compressed_nacp(bomb, flag=1))
    unannounced = bytes(compressed_nacp(bomb, flag=0))

    def inflate_everything() -> None:
        assert len(zlib.decompress(bomb, wbits=-15)) == 8 * MIB

    def parse_announced() -> None:
        with pytest.raises(FormatError, match="more than"):
            nacp.parse_nacp(announced)

    def parse_unannounced() -> None:
        assert nacp.parse_nacp(unannounced).titles_compressed is False

    # The measurement sees the output of an unbounded inflation ...
    assert peak_allocation(inflate_everything) >= 8 * MIB
    # ... and the parser never comes near it, announced as compressed or not.
    assert peak_allocation(parse_announced) < MIB
    assert peak_allocation(parse_unannounced) < MIB


def test_unknown_titles_data_format_leaves_the_titles_out() -> None:
    data = blank_nacp()
    put_title(data, 0, b"Synthetic Game", b"Example Publisher")
    data[0x3060:0x3065] = b"1.2.3"
    struct.pack_into("<Q", data, 0x3078, APP_ID)
    data[0x3215] = 2
    parsed = nacp.parse_nacp(data)
    assert parsed.titles == ()  # not guessed at
    assert parsed.titles_compressed is False
    assert parsed.display_version == "1.2.3"
    assert parsed.inferred_application_id() == APP_ID
    assert len(parsed.notes) == 1
    assert "TitlesDataFormat" in parsed.notes[0]
    assert "is 2" in parsed.notes[0]


# ----------------------------------------------------------------------------------
# control.nacp: file sizes
# ----------------------------------------------------------------------------------


def _full_nacp() -> bytearray:
    data = blank_nacp()
    put_title(data, 0, b"Synthetic Game", b"Example Publisher")
    struct.pack_into("<I", data, 0x302C, 0x1)
    struct.pack_into("<Q", data, 0x3038, APP_ID)
    data[0x3060:0x3065] = b"1.2.3"
    struct.pack_into("<Q", data, 0x3070, APP_ID + 0x1000)
    struct.pack_into("<Q", data, 0x3078, APP_ID)
    struct.pack_into("<8Q", data, 0x30B0, *(APP_ID + index for index in range(8)))
    struct.pack_into("<Q", data, 0x30F8, APP_ID)
    data[0x3212] = 1
    return data


@pytest.mark.parametrize("size", [0, 1, 0x300, 0x3000, 0x3060, 0x306F])
def test_file_shorter_than_0x3070_bytes_is_not_a_nacp(size: int) -> None:
    with pytest.raises(FormatError, match="truncated file") as caught:
        nacp.parse_nacp(bytes(_full_nacp()[:size]))
    assert str(caught.value).startswith("NACP: ")
    assert f"0x{size:X} bytes" in str(caught.value)


def test_short_file_is_read_with_missing_fields_as_zero() -> None:
    full = bytes(_full_nacp())
    complete = nacp.parse_nacp(full)
    assert complete.notes == ()
    title = nacp.NacpTitle(0, "AmericanEnglish", "Synthetic Game", "Example Publisher")

    shortest = nacp.parse_nacp(full[:0x3070])
    assert shortest.titles == (title,)
    assert shortest.display_version == "1.2.3"
    assert shortest.supported_languages == ("AmericanEnglish",)
    assert shortest.presence_group_id == APP_ID
    assert shortest.add_on_content_base_id == 0
    assert shortest.save_data_owner_id == 0
    assert shortest.local_communication_ids == (0,) * 8
    assert shortest.seed_for_pseudo_device_id == 0
    assert shortest.program_index == 0
    assert shortest.titles_compressed is False
    assert len(shortest.notes) == 1
    assert "0x3070" in shortest.notes[0]
    assert "0x4000" in shortest.notes[0]

    # A field the file ends in the middle of is missing, not half a number.
    assert nacp.parse_nacp(full[:0x3077]).add_on_content_base_id == 0
    in_between = nacp.parse_nacp(full[:0x3078])
    assert in_between.add_on_content_base_id == APP_ID + 0x1000
    assert in_between.save_data_owner_id == 0
    two_ids = nacp.parse_nacp(full[:0x30C4])
    assert two_ids.save_data_owner_id == APP_ID
    assert two_ids.local_communication_ids == (APP_ID, APP_ID + 1, 0, 0, 0, 0, 0, 0)
    assert two_ids.seed_for_pseudo_device_id == 0
    assert nacp.parse_nacp(full[:0x3212]).program_index == 0
    assert nacp.parse_nacp(full[:0x3213]).program_index == 1

    one_byte_short = nacp.parse_nacp(full[:0x3FFF])
    assert len(one_byte_short.notes) == 1
    assert "0x3FFF" in one_byte_short.notes[0]
    assert (one_byte_short.titles, one_byte_short.program_index) == ((title,), 1)
    assert one_byte_short.local_communication_ids == complete.local_communication_ids


def test_short_file_cannot_announce_a_compressed_block() -> None:
    # The format byte lies beyond the end of the file and so reads as 0; a compressed
    # block is then still found by trying.
    data = bytes(compressed_nacp(_valid_stream(), flag=1))
    cut = nacp.parse_nacp(data[:0x3215])
    assert cut.titles == THREE_TITLES
    assert cut.titles_compressed is True
    assert len(cut.notes) == 2
    assert "0x3215" in cut.notes[0]  # the size
    assert "compressed" in cut.notes[1]


def test_file_longer_than_0x4000_bytes_is_read_up_to_there() -> None:
    full = bytes(_full_nacp())
    longer = nacp.parse_nacp(full + b"\xff" * 0x123)
    reference = nacp.parse_nacp(full)
    assert len(longer.notes) == 1
    assert "0x4123" in longer.notes[0]
    assert "0x4000" in longer.notes[0]
    assert longer.titles == reference.titles
    assert longer.display_version == reference.display_version
    assert longer.local_communication_ids == reference.local_communication_ids
    assert longer.program_index == reference.program_index
    assert nacp.parse_nacp(full + b"\x00").notes != ()


def test_parse_nacp_accepts_bytes_bytearray_and_memoryview() -> None:
    data = _full_nacp()
    reference = nacp.parse_nacp(bytes(data))
    assert nacp.parse_nacp(data) == reference
    assert nacp.parse_nacp(memoryview(data)) == reference
    padded = b"\xee" * 3 + bytes(data) + b"\xee" * 3
    assert nacp.parse_nacp(memoryview(padded)[3:-3]) == reference
    with pytest.raises(TypeError):
        nacp.parse_nacp("NACP")  # type: ignore[arg-type]


def test_parsers_release_the_buffer_they_were_given_even_when_they_fail() -> None:
    npdm_data = bytearray(b"JUNK" * 0x100)
    with pytest.raises(FormatError) as npdm_error:
        npdm.parse_npdm(npdm_data)
    nacp_data = compressed_nacp(b"\x07" + bytes(8))
    with pytest.raises(FormatError) as nacp_error:
        nacp.parse_nacp(nacp_data)
    # The tracebacks are still alive here. A view left exported by a parser would make
    # these resizes fail with BufferError.
    npdm_data.extend(b"more")
    nacp_data.extend(b"more")
    assert npdm_error.value is not None
    assert nacp_error.value is not None


def test_nacp_result_is_immutable_and_hashable() -> None:
    parsed = nacp.parse_nacp(_full_nacp())
    with pytest.raises(AttributeError):
        parsed.program_index = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        parsed.titles[0].name = "Other"  # type: ignore[misc]
    assert hash(parsed) == hash(nacp.parse_nacp(_full_nacp()))


def test_hostile_title_blocks_never_crash_the_parser() -> None:
    rng = random.Random(0xBEA)
    valid = _valid_stream()
    for round_number in range(300):
        data = blank_nacp()
        kind = round_number % 3
        if kind == 0:  # pure noise
            data[:0x3000] = rng.randbytes(0x3000)
        elif kind == 1:  # a valid stream with a few bytes changed
            stream = bytearray(valid)
            for _ in range(rng.randint(1, 4)):
                stream[rng.randrange(len(stream))] = rng.getrandbits(8)
            struct.pack_into("<H", data, 0, len(stream))
            data[2 : 2 + len(stream)] = stream
        else:  # a plausible DataSize in front of noise
            struct.pack_into("<H", data, 0, rng.randint(1, 0x2FFE))
            data[2:0x3000] = rng.randbytes(0x2FFE)
        data[0x3060:0x3070] = rng.randbytes(16)
        flag = rng.choice([0, 0, 1, 1, 2, 0xFF])
        data[0x3215] = flag
        try:
            parsed = nacp.parse_nacp(bytes(data))
        except FormatError:
            # Only a block announced as compressed may be refused.
            assert flag == 1, round_number
            continue
        assert len(parsed.local_communication_ids) == 8
        assert len(parsed.titles) <= (32 if parsed.titles_compressed else 16)
        assert all(entry.name for entry in parsed.titles)
        assert all(isinstance(note, str) and note for note in parsed.notes)


# ----------------------------------------------------------------------------------
# control.nacp: inferring the application ID
# ----------------------------------------------------------------------------------


def test_inference_when_every_field_agrees() -> None:
    parsed = nacp_with_ids(
        save_data_owner=APP_ID,
        presence_group=APP_ID,
        add_on_content_base=APP_ID + 0x1000,
        local_communication=APP_ID,
        seed=APP_ID,
    )
    assert parsed.inferred_application_id() == APP_ID
    assert parsed.inferred_program_id() == APP_ID


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # One field points at another title: the majority wins.
        pytest.param(
            {
                "save_data_owner": OTHER_ID,
                "presence_group": APP_ID,
                "add_on_content_base": APP_ID + 0x1000,
                "local_communication": APP_ID,
                "seed": APP_ID,
            },
            APP_ID,
            id="first field elsewhere",
        ),
        pytest.param(
            {
                "save_data_owner": APP_ID,
                "presence_group": APP_ID,
                "add_on_content_base": APP_ID + 0x1000,
                "local_communication": OTHER_ID,
                "seed": APP_ID,
            },
            APP_ID,
            id="one later field elsewhere",
        ),
        pytest.param(
            {
                "save_data_owner": APP_ID,
                "presence_group": APP_ID,
                "add_on_content_base": OTHER_ID + 0x1000,
                "local_communication": OTHER_ID,
                "seed": OTHER_ID,
            },
            OTHER_ID,
            id="three later fields outvote the first two",
        ),
        pytest.param(
            {"save_data_owner": OTHER_ID, "presence_group": APP_ID, "seed": APP_ID},
            APP_ID,
            id="two against one",
        ),
        # Ties: the candidate order decides (SaveDataOwnerId, PresenceGroupId,
        # AddOnContentBaseId, LocalCommunicationId, SeedForPseudoDeviceId).
        pytest.param(
            {"save_data_owner": OTHER_ID, "presence_group": APP_ID},
            OTHER_ID,
            id="tie: save data owner before presence group",
        ),
        pytest.param(
            {"presence_group": APP_ID, "add_on_content_base": OTHER_ID + 0x1000},
            APP_ID,
            id="tie: presence group before add-on content",
        ),
        pytest.param(
            {"add_on_content_base": OTHER_ID + 0x1000, "local_communication": APP_ID},
            OTHER_ID,
            id="tie: add-on content before local communication",
        ),
        pytest.param(
            {"local_communication": APP_ID, "seed": OTHER_ID},
            APP_ID,
            id="tie: local communication before seed",
        ),
        pytest.param(
            {
                "presence_group": APP_ID,
                "add_on_content_base": OTHER_ID + 0x1000,
                "local_communication": OTHER_ID,
                "seed": APP_ID,
            },
            APP_ID,
            id="tie of two against two",
        ),
        pytest.param(
            {"save_data_owner": THIRD_ID, "presence_group": OTHER_ID, "seed": APP_ID},
            THIRD_ID,
            id="three-way tie",
        ),
        # Values of the update, of add-on content and of other programs of the same
        # application: clearing the low 13 bits gives the application.
        pytest.param(
            {
                "save_data_owner": APP_ID + 0x800,
                "presence_group": APP_ID + 1,
                "add_on_content_base": APP_ID + 0x1000 + 0x800,
                "local_communication": APP_ID + 0x1FFF,
                "seed": APP_ID + 0x1000,
            },
            APP_ID,
            id="low bits cleared",
        ),
        pytest.param({"save_data_owner": APP_ID + 0x800}, APP_ID, id="update ID alone"),
        pytest.param(
            {"save_data_owner": APP_ID + 0x800, "presence_group": OTHER_ID},
            APP_ID,
            id="tie after clearing the low bits",
        ),
        pytest.param(
            {"save_data_owner": APP_ID + 0x2000, "presence_group": APP_ID, "seed": APP_ID},
            APP_ID,
            id="bit 13 is not cleared: a different application",
        ),
        # AddOnContentBaseId is the application ID plus 0x1000.
        pytest.param({"add_on_content_base": APP_ID + 0x1000}, APP_ID, id="add-on base alone"),
        pytest.param(
            {"add_on_content_base": APP_ID},
            APP_ID - 0x2000,
            id="0x1000 is subtracted before the low bits are cleared",
        ),
        pytest.param({"add_on_content_base": 0x1000}, None, id="add-on base of exactly 0x1000"),
        pytest.param({"add_on_content_base": 0xFFF}, None, id="add-on base below 0x1000"),
        # Values outside the application range do not vote.
        pytest.param(
            {
                "save_data_owner": (0x0100 << 48) | 0x8000,
                "presence_group": 0x0200 << 48,
                "add_on_content_base": 0xFFF,
                "local_communication": 1,
                "seed": APP_ID,
            },
            APP_ID,
            id="only the seed is an application ID",
        ),
        pytest.param(
            {"save_data_owner": (0x0100 << 48) | 0x8000, "presence_group": APP_ID},
            APP_ID,
            id="a system ID is skipped, not preferred",
        ),
        pytest.param(
            {"save_data_owner": FIRST_APPLICATION_ID}, FIRST_APPLICATION_ID, id="first ID"
        ),
        pytest.param({"save_data_owner": FIRST_APPLICATION_ID - 1}, None, id="below the first"),
        pytest.param(
            {"save_data_owner": LAST_APPLICATION_ID},
            LAST_APPLICATION_ID - 0x1FFF,
            id="last ID",
        ),
        pytest.param({"save_data_owner": LAST_APPLICATION_ID + 1}, None, id="above the last"),
        pytest.param(
            {"save_data_owner": LAST_APPLICATION_ID + 0x2000}, None, id="above after clearing"
        ),
        pytest.param({"save_data_owner": U64_MAX, "seed": U64_MAX}, None, id="all bits set"),
        pytest.param({"presence_group": 0x1FFF}, None, id="nothing left after clearing"),
        # Nothing usable.
        pytest.param({}, None, id="every field zero"),
        pytest.param(
            {
                "save_data_owner": 1,
                "presence_group": 0x0200 << 48,
                "add_on_content_base": 0x2000,
                "local_communication": 0xFFFF,
                "seed": U64_MAX,
            },
            None,
            id="no field in the application range",
        ),
    ],
)
def test_application_id_inference(fields: dict[str, int], expected: int | None) -> None:
    parsed = nacp_with_ids(**fields)
    assert parsed.inferred_application_id() == expected
    assert parsed.inferred_program_id() == expected


def test_only_the_first_local_communication_id_votes() -> None:
    data = blank_nacp()
    struct.pack_into("<8Q", data, 0x30B0, 0, *([OTHER_ID] * 7))
    struct.pack_into("<Q", data, 0x30F8, APP_ID)
    parsed = nacp.parse_nacp(data)
    assert parsed.local_communication_ids == (0, *([OTHER_ID] * 7))
    assert parsed.inferred_application_id() == APP_ID


@pytest.mark.parametrize("program_index", [0, 1, 2, 0xFF])
def test_program_index_is_added_for_the_program_id(program_index: int) -> None:
    parsed = nacp_with_ids(
        save_data_owner=APP_ID, presence_group=APP_ID, program_index=program_index
    )
    assert parsed.program_index == program_index
    assert parsed.inferred_application_id() == APP_ID
    assert parsed.inferred_program_id() == APP_ID + program_index

    nothing = nacp_with_ids(program_index=program_index)
    assert nothing.inferred_application_id() is None
    assert nothing.inferred_program_id() is None


# ----------------------------------------------------------------------------------
# control.nacp: reading files
# ----------------------------------------------------------------------------------


def test_read_nacp_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "control.nacp"
    path.write_bytes(_full_nacp())
    parsed = nacp.read_nacp(path)
    assert parsed == nacp.parse_nacp(_full_nacp())
    assert parsed.title() == nacp.NacpTitle(
        0, "AmericanEnglish", "Synthetic Game", "Example Publisher"
    )
    assert nacp.read_nacp(str(path)) == parsed


def test_read_nacp_reports_bad_and_missing_files(tmp_path: Path) -> None:
    short = tmp_path / "short.nacp"
    short.write_bytes(bytes(0x100))
    with pytest.raises(FormatError, match="truncated file"):
        nacp.read_nacp(short)
    broken = tmp_path / "broken.nacp"
    broken.write_bytes(compressed_nacp(b"\x07" + bytes(8)))
    with pytest.raises(FormatError, match="compressed title block"):
        nacp.read_nacp(broken)
    with pytest.raises(FileNotFoundError):
        nacp.read_nacp(tmp_path / "missing.nacp")


def test_read_nacp_refuses_a_file_over_one_mebibyte(tmp_path: Path) -> None:
    full = bytes(_full_nacp())
    at_limit = tmp_path / "limit.nacp"
    at_limit.write_bytes(full + bytes(MIB - len(full)))
    parsed = nacp.read_nacp(at_limit)
    assert parsed.titles == nacp.parse_nacp(full).titles
    assert len(parsed.notes) == 1
    assert "0x100000" in parsed.notes[0]

    over = tmp_path / "over.nacp"
    over.write_bytes(full + bytes(MIB + 1 - len(full)))
    with pytest.raises(FormatError, match="1 MiB"):
        nacp.read_nacp(over)


def test_read_nacp_copes_with_a_path_beyond_260_characters(tmp_path: Path) -> None:
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/control.nacp"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, _full_nacp())
    assert nacp.read_nacp(target).display_version == "1.2.3"


# ----------------------------------------------------------------------------------
# control.nacp: the writer
# ----------------------------------------------------------------------------------


def test_build_nacp_writes_a_plain_file_field_by_field() -> None:
    built = nacp.build_nacp(
        {
            0: ("Synthetic Game", "Example Publisher"),
            2: (JAPANESE_NAME, JAPANESE_PUBLISHER),
        },
        display_version="1.2.3",
        application_id=APP_ID,
        program_index=1,
    )
    assert isinstance(built, bytes)
    assert len(built) == 0x4000

    assert built[0x000:0x200] == b"Synthetic Game".ljust(0x200, b"\x00")
    assert built[0x200:0x300] == b"Example Publisher".ljust(0x100, b"\x00")
    assert built[0x300:0x600] == bytes(0x300)
    assert built[0x600:0x800] == JAPANESE_NAME.encode().ljust(0x200, b"\x00")
    assert built[0x800:0x900] == JAPANESE_PUBLISHER.encode().ljust(0x100, b"\x00")
    assert struct.unpack_from("<I", built, 0x302C) == (0b101,)
    assert struct.unpack_from("<Q", built, 0x3038) == (APP_ID,)
    assert built[0x3060:0x3070] == b"1.2.3" + bytes(11)
    assert struct.unpack_from("<Q", built, 0x3070) == (APP_ID + 0x1000,)
    assert struct.unpack_from("<Q", built, 0x3078) == (APP_ID,)
    assert struct.unpack_from("<8Q", built, 0x30B0) == (APP_ID, 0, 0, 0, 0, 0, 0, 0)
    assert struct.unpack_from("<Q", built, 0x30F8) == (APP_ID,)
    assert built[0x3212] == 1
    assert built[0x3215] == 0

    # And nothing else: the whole file, against one laid out by hand.
    expected = blank_nacp()
    put_title(expected, 0, b"Synthetic Game", b"Example Publisher")
    put_title(expected, 2, JAPANESE_NAME.encode(), JAPANESE_PUBLISHER.encode())
    struct.pack_into("<I", expected, 0x302C, 0b101)
    struct.pack_into("<Q", expected, 0x3038, APP_ID)
    expected[0x3060:0x3065] = b"1.2.3"
    struct.pack_into("<Q", expected, 0x3070, APP_ID + 0x1000)
    struct.pack_into("<Q", expected, 0x3078, APP_ID)
    struct.pack_into("<Q", expected, 0x30B0, APP_ID)
    struct.pack_into("<Q", expected, 0x30F8, APP_ID)
    expected[0x3212] = 1
    assert built == bytes(expected)

    parsed = nacp.parse_nacp(built)
    assert parsed.titles == (
        nacp.NacpTitle(0, "AmericanEnglish", "Synthetic Game", "Example Publisher"),
        nacp.NacpTitle(2, "Japanese", JAPANESE_NAME, JAPANESE_PUBLISHER),
    )
    assert parsed.display_version == "1.2.3"
    assert parsed.supported_languages == ("AmericanEnglish", "Japanese")
    assert parsed.titles_compressed is False
    assert parsed.inferred_application_id() == APP_ID
    assert parsed.inferred_program_id() == APP_ID + 1
    assert parsed.notes == ()


def test_build_nacp_writes_a_compressed_file_field_by_field() -> None:
    built = nacp.build_nacp(
        {
            0: ("Synthetic Game", "Example Publisher"),
            16: (POLISH_NAME, POLISH_PUBLISHER),
            17: (THAI_NAME, THAI_PUBLISHER),
        },
        display_version="2.0.0",
        application_id=APP_ID,
        compressed=True,
    )
    assert isinstance(built, bytes)
    assert len(built) == 0x4000

    (data_size,) = struct.unpack_from("<H", built, 0x0000)
    assert 1 <= data_size <= 0x2FFE
    inflater = zlib.decompressobj(wbits=-15)  # raw DEFLATE: a wrapper would not inflate
    inflated = inflater.decompress(built[2 : 2 + data_size])
    assert inflater.eof
    assert inflater.unused_data == b""
    assert inflated == bytes(three_title_block())
    assert built[2 + data_size : 0x3000] == bytes(0x3000 - 2 - data_size)
    assert built[0x3215] == 1

    # Everything behind the title block, against a tail laid out by hand.
    tail = bytearray(0x1000)
    struct.pack_into("<I", tail, 0x02C, (1 << 0) | (1 << 16) | (1 << 17))
    struct.pack_into("<Q", tail, 0x038, APP_ID)
    tail[0x060:0x065] = b"2.0.0"
    struct.pack_into("<Q", tail, 0x070, APP_ID + 0x1000)
    struct.pack_into("<Q", tail, 0x078, APP_ID)
    struct.pack_into("<Q", tail, 0x0B0, APP_ID)
    struct.pack_into("<Q", tail, 0x0F8, APP_ID)
    tail[0x215] = 1
    assert built[0x3000:] == bytes(tail)

    parsed = nacp.parse_nacp(built)
    assert parsed.titles == THREE_TITLES
    assert parsed.titles_compressed is True
    assert parsed.display_version == "2.0.0"
    assert parsed.supported_languages == ("AmericanEnglish", "Polish", "Thai")
    assert parsed.inferred_program_id() == APP_ID
    assert parsed.notes == ()


def test_build_nacp_defaults_write_an_empty_file() -> None:
    assert nacp.build_nacp({}) == bytes(0x4000)
    compressed = nacp.build_nacp({}, compressed=True)
    assert len(compressed) == 0x4000
    assert compressed[0x3215] == 1
    assert compressed[0x3000:0x3215] == bytes(0x215)
    assert compressed[0x3216:] == bytes(0x4000 - 0x3216)
    assert nacp.parse_nacp(compressed).titles == ()
    assert nacp.parse_nacp(compressed).titles_compressed is True


def test_build_nacp_supported_languages() -> None:
    titles = {1: ("Synthetic Game", "Example Publisher")}
    assert struct.unpack_from("<I", nacp.build_nacp(titles), 0x302C) == (1 << 1,)
    explicit = nacp.build_nacp(titles, supported_languages=[0, 17, 31, 0])
    assert struct.unpack_from("<I", explicit, 0x302C) == ((1 << 0) | (1 << 17) | (1 << 31),)
    assert struct.unpack_from("<I", nacp.build_nacp(titles, supported_languages=()), 0x302C) == (0,)
    everything = nacp.build_nacp({}, supported_languages=iter(range(32)))
    assert struct.unpack_from("<I", everything, 0x302C) == (0xFFFFFFFF,)
    assert nacp.parse_nacp(explicit).supported_languages == (
        "AmericanEnglish",
        "Thai",
        "Language31",
    )


def test_build_nacp_fills_fields_to_their_limits() -> None:
    longest_name = "é" * 0xFF + "x"  # 0x1FF bytes of UTF-8
    longest_publisher = "P" * 0xFF
    built = nacp.build_nacp(
        {15: (longest_name, longest_publisher)}, display_version="1.0.0-synthetic1"
    )
    start = 15 * 0x300
    assert built[start : start + 0x200] == b"\xc3\xa9" * 0xFF + b"x\x00"
    assert built[start + 0x200 : start + 0x300] == b"P" * 0xFF + b"\x00"
    assert built[0x3060:0x3070] == b"1.0.0-synthetic1"
    assert built[0x3070:0x3078] == bytes(8)
    parsed = nacp.parse_nacp(built)
    assert parsed.titles == (
        nacp.NacpTitle(15, "BrazilianPortuguese", longest_name, longest_publisher),
    )
    assert parsed.display_version == "1.0.0-synthetic1"

    highest = nacp.build_nacp(
        {31: ("Name 31", "Publisher 31")}, application_id=U64_MAX - 0x1000, compressed=True
    )
    assert struct.unpack_from("<Q", highest, 0x3070) == (U64_MAX,)
    assert nacp.parse_nacp(highest).titles == (
        nacp.NacpTitle(31, "Language31", "Name 31", "Publisher 31"),
    )
    assert nacp.build_nacp({}, program_index=0xFF)[0x3212] == 0xFF


def test_build_nacp_without_application_id_leaves_the_id_fields_zero() -> None:
    built = nacp.build_nacp({0: ("Synthetic Game", "Example Publisher")}, display_version="1")
    for offset in (0x3038, 0x3070, 0x3078, 0x30B0, 0x30F8):
        assert struct.unpack_from("<Q", built, offset) == (0,)
    assert nacp.parse_nacp(built).inferred_application_id() is None


def _incompressible_titles() -> dict[int, tuple[str, str]]:
    rng = random.Random(0xBEA)
    alphabet = string.ascii_letters + string.digits

    def noise(length: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(length))

    return {index: (noise(0x1FF), noise(0xFF)) for index in range(32)}


@pytest.mark.parametrize(
    ("titles", "options"),
    [
        pytest.param({16: ("Name", "Publisher")}, {}, id="index 16 in a plain block"),
        pytest.param({31: ("Name", "Publisher")}, {}, id="index 31 in a plain block"),
        pytest.param({32: ("Name", "Publisher")}, {}, id="index 32"),
        pytest.param({32: ("Name", "Publisher")}, {"compressed": True}, id="index 32 compressed"),
        pytest.param({-1: ("Name", "Publisher")}, {}, id="negative index"),
        pytest.param({0: ("x" * 0x200, "Publisher")}, {}, id="name of 0x200 bytes"),
        pytest.param({0: ("é" * 0x100, "Publisher")}, {}, id="name of 0x200 bytes of UTF-8"),
        pytest.param({0: ("Name", "x" * 0x100)}, {}, id="publisher of 0x100 bytes"),
        pytest.param(
            {0: ("Name", "é" * 0x80)}, {"compressed": True}, id="publisher of 0x100 bytes of UTF-8"
        ),
        pytest.param({0: ("Na\x00me", "Publisher")}, {}, id="NUL in a name"),
        pytest.param({0: ("Name", "Pub\x00lisher")}, {}, id="NUL in a publisher"),
        pytest.param({0: ("\ud800", "Publisher")}, {}, id="name that cannot be encoded"),
        pytest.param({}, {"display_version": "1.0.0-synthetic12"}, id="version of 17 bytes"),
        pytest.param({}, {"display_version": "1.0é"}, id="version that is not ASCII"),
        pytest.param({}, {"display_version": "1.0\x00"}, id="NUL in the version"),
        pytest.param({}, {"supported_languages": [32]}, id="flag bit 32"),
        pytest.param({}, {"supported_languages": [0, -1]}, id="negative flag bit"),
        pytest.param({}, {"application_id": -1}, id="negative application ID"),
        pytest.param({}, {"application_id": 1 << 64}, id="application ID of 65 bits"),
        pytest.param({}, {"application_id": U64_MAX - 0xFFF}, id="add-on base of 65 bits"),
        pytest.param({}, {"program_index": -1}, id="negative program index"),
        pytest.param({}, {"program_index": 0x100}, id="program index of 9 bits"),
        pytest.param(
            _incompressible_titles(), {"compressed": True}, id="titles that do not compress"
        ),
    ],
)
def test_build_nacp_rejects_bad_values(
    titles: dict[int, tuple[str, str]], options: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        nacp.build_nacp(titles, **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("titles", "options"),
    [
        ({"0": ("Name", "Publisher")}, {}),
        ({True: ("Name", "Publisher")}, {}),
        ({0: (b"Name", "Publisher")}, {}),
        ({0: ("Name", None)}, {}),
        ({}, {"display_version": 1}),
        ({}, {"application_id": "0"}),
        ({}, {"application_id": 1.5}),
        ({}, {"program_index": "0"}),
        ({}, {"supported_languages": ["0"]}),
    ],
)
def test_build_nacp_rejects_wrong_types(
    titles: dict[object, object], options: dict[str, object]
) -> None:
    with pytest.raises(TypeError):
        nacp.build_nacp(titles, **options)  # type: ignore[arg-type]


def test_incompressible_titles_fit_a_plain_block() -> None:
    # The same texts that are refused compressed are fine as the 16 plain entries.
    titles = {index: texts for index, texts in _incompressible_titles().items() if index < 16}
    parsed = nacp.parse_nacp(nacp.build_nacp(titles))
    assert [(entry.name, entry.publisher) for entry in parsed.titles] == list(titles.values())
    assert parsed.titles_compressed is False
    assert parsed.notes == ()
