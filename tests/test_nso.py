"""Tests for bea.formats.nso: NSO0 headers, segments, memory images and rewriting.

Every input is synthetic. The two golden files and the LZ4 blocks were written out by
hand from the documented formats; every other module is assembled here with ``struct``
from the documented header layout. The reader is therefore checked against bytes it did
not produce and the writers against a layout they did not compute. Expected hashes come
from ``hashlib`` and expected decompressed bytes from the LZ4 library called directly.
"""

from __future__ import annotations

import hashlib
import random
import struct
import tracemalloc
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import lz4.block
import pytest

from bea import fsutil, hexutil
from bea.errors import BeaError, FormatError, UnsupportedInputError
from bea.formats import nso
from bea.formats.nso import NsoFile, NsoFlags, NsoHeader, build_nso

# ----------------------------------------------------------------------------------
# Golden vectors
# ----------------------------------------------------------------------------------

#: Golden file A: three raw segments in packed layout, 392 bytes.
GOLDEN_A = bytes.fromhex(
    """
    4e534f3000000000000000003800000000010000000000004000000000010000
    4001000000100000280000000000000068010000002000002000000030000000
    101112131415161718191a1b1c1d1e1f20212223000000000000000000000000
    4000000028000000200000000000000000000000000000000000000000000000
    0000000000000000000000000000000000000000000000000000000000000000
    529dde3435f88cabff28e3144f909b17e09518781226e5325351f3e1818fe514
    b4d1b06b760e6ca63a55c9731a5e03383a8e147039bec3d252c36a37a27624ec
    1d586c9adb38fbf91095434631cb0dfb8ee81ba13f49ba0f230b1532bdad8976
    00000000080000004d4f4430f81f000018200000482000000000000000000000
    182000001f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5
    000000000900000073796e74682e6e7373000000000000000000000000000000
    0000000000000000000000000000000000000000000000000001020304050607
    08090a0b0c0d0e0f
    """
)
A_SHA256 = "b472b47a79240d9df045839994ad59359c4697f4bc6c30f69020ff028f20bf5f"
A_HASHES = {
    "text": "529dde3435f88cabff28e3144f909b17e09518781226e5325351f3e1818fe514",
    "rodata": "b4d1b06b760e6ca63a55c9731a5e03383a8e147039bec3d252c36a37a27624ec",
    "data": "1d586c9adb38fbf91095434631cb0dfb8ee81ba13f49ba0f230b1532bdad8976",
}
A_IMAGE_SHA256 = "069d6f504aed2e471c7e8c7dada3a1a200c2ff6f01d7c524640c17bcae8fac35"
A_REWRITTEN_SHA256 = "dc11a53b975ad5747876d596f2659df62d27e7879cb3394ce9d90c96b1d9dbee"

#: Golden file B: the three segments LZ4-compressed, 383 bytes.
GOLDEN_B = bytes.fromhex(
    """
    4e534f3000000000000000003f00000000010000000000000010000000010000
    420100000010000028010000000000005f010000002000002001000030000000
    101112131415161718191a1b1c1d1e1f20212223000000000000000000000000
    420000001d000000200000000000000000000000000000000000000000000000
    0000000000000000000000000000000000000000000000000000000000000000
    bb279878cd70cb0d0ae84bbdbeced52bd71f3c5d62d586e3d88d695c37676397
    7664baea3d3ecae519935762745900213078dd9ae7050eadd9563a0dc3fc80f2
    e5bc21873c8296923f7f95c3970e1bc95119da4485c7f7401a95728a6bdef8fa
    ff1900000000080000004d4f4430f81f00001820000048200000000000000000
    0000182000001f2003d50400ffffffffffffffffffffffffffffffcf50d51f20
    03d5ff03000000000900000073796e74682e6e7373000100fe5000000000001b
    000100ff02000102030405060708090a0b0c0d0e0f000100e7500000000000
    """
)
B_SHA256 = "b213df3fd95567bb321b377ec6dae309eb19fd9824998c1e96e22cdf511f5e38"
B_HASHES = {
    "text": "bb279878cd70cb0d0ae84bbdbeced52bd71f3c5d62d586e3d88d695c37676397",
    "rodata": "7664baea3d3ecae519935762745900213078dd9ae7050eadd9563a0dc3fc80f2",
    "data": "e5bc21873c8296923f7f95c3970e1bc95119da4485c7f7401a95728a6bdef8fa",
}
B_IMAGE_SHA256 = "f29d8b399668b25427cbf1fa5f66269e0e374651831e50fe15d2bf2836813c09"
B_REWRITTEN_SHA256 = "97a9887012c14cc4e5639670bbc1fd0b70e76b4d89c69473e8784e65830670e5"

#: Module ID of both golden files: 20 significant bytes, then zeros.
GOLDEN_MODULE_ID = bytes(range(0x10, 0x24))
GOLDEN_BUILD_ID = "101112131415161718191A1B1C1D1E1F20212223"

SEGMENT_NAMES = ("text", "rodata", "data")
ALL_HASHES_OK = {"text": True, "rodata": True, "data": True}

# The segments of A are raw: they are the file bytes at the offsets its header gives.
A_SEGMENTS = {
    "text": GOLDEN_A[0x100:0x140],
    "rodata": GOLDEN_A[0x140:0x168],
    "data": GOLDEN_A[0x168:0x188],
}
# The segments of B, decoded with the LZ4 library itself from the documented offsets.
B_SEGMENTS = {
    "text": lz4.block.decompress(GOLDEN_B[0x100:0x142], uncompressed_size=0x1000),
    "rodata": lz4.block.decompress(GOLDEN_B[0x142:0x15F], uncompressed_size=0x128),
    "data": lz4.block.decompress(GOLDEN_B[0x15F:0x17F], uncompressed_size=0x120),
}

#: Raw LZ4 blocks written out by hand from the block format description, with what each
#: one decodes to.
LZ4_VECTORS = [
    pytest.param("00", b"", id="empty"),
    pytest.param("40 4E 53 4F 30", b"NSO0", id="literals-only"),
    pytest.param("1A 41 01 00 50 41 41 41 41 41", b"\x41" * 20, id="overlapping-match"),
    pytest.param("2F AB CD 02 00 26 50 CD AB CD AB CD", b"\xab\xcd" * 32, id="long-match"),
    pytest.param(
        "1F 00 01 00" + " FF" * 15 + " F6 50 00 00 00 00 00", bytes(4096), id="4096-zeros"
    ),
]
#: Blocks that are not a valid encoding of the number of bytes given with them.
#:
#: A fourth block that the format description calls corrupt, one whose match offset is
#: zero, is deliberately absent: the LZ4 library decodes it instead of rejecting it, so
#: BEA, which leaves that judgement to the library, cannot promise to refuse it (see
#: "Robustness" in the module under test).
LZ4_REJECTED = [
    pytest.param("10 41 05 00 50 41 41 41 41 41", 20, id="offset-beyond-the-output"),
    pytest.param("50 41", 5, id="literal-run-past-the-input"),
    pytest.param("40 4E 53 4F 30", 5, id="shorter-than-declared"),
    pytest.param("1A 41 01 00 50 41 41 41 41 41", 10, id="longer-than-declared"),
]

# ----------------------------------------------------------------------------------
# Helpers: reading and laying out headers by hand
# ----------------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def u32(blob: bytes, offset: int) -> int:
    """Read the little-endian u32 at ``offset``."""
    (value,) = struct.unpack_from("<I", blob, offset)
    return value


def poke(blob: bytes, offset: int, value: int) -> bytes:
    """Return ``blob`` with the little-endian u32 at ``offset`` replaced by ``value``."""
    out = bytearray(blob)
    struct.pack_into("<I", out, offset, value)
    return bytes(out)


def differing_offsets(left: bytes, right: bytes) -> list[int]:
    """Return every offset at which two equally long byte strings differ."""
    assert len(left) == len(right)
    return [index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]


def image_of(placed: Sequence[tuple[int, bytes]]) -> bytes:
    """Build a memory image the plain way: zeros, with each ``(offset, bytes)`` copied in."""
    image = bytearray(max(offset + len(content) for offset, content in placed))
    for offset, content in placed:
        image[offset : offset + len(content)] = content
    return bytes(image)


A_IMAGE = image_of(
    [(0x0, A_SEGMENTS["text"]), (0x1000, A_SEGMENTS["rodata"]), (0x2000, A_SEGMENTS["data"])]
)
B_IMAGE = image_of(
    [(0x0, B_SEGMENTS["text"]), (0x1000, B_SEGMENTS["rodata"]), (0x2000, B_SEGMENTS["data"])]
)


@dataclass
class Part:
    """One segment of a hand-assembled module (see :func:`assemble`).

    ``data`` is the decompressed content: it sets the size field and the hash, unless
    ``size`` or ``hash`` say otherwise. ``stored`` is what goes into the file when that is
    not ``data`` itself: an LZ4 block, or bytes that are not what the header promises.
    """

    data: bytes
    memory_offset: int
    stored: bytes | None = None
    size: int | None = None
    stored_size: int | None = None
    file_offset: int | None = None
    hash: bytes | None = None


def assemble(
    text: Part,
    rodata: Part,
    data: Part,
    *,
    flags: int = 0x38,
    version: int = 0,
    bss_size: int = 0,
    module_id: bytes = b"",
) -> bytes:
    """Lay a module out by hand: the header field by field, the stored bytes from 0x100 on."""
    header = bytearray(0x100)
    header[0x00:0x04] = b"NSO0"
    struct.pack_into("<I", header, 0x04, version)
    struct.pack_into("<I", header, 0x0C, flags)
    struct.pack_into("<I", header, 0x3C, bss_size)
    header[0x40 : 0x40 + len(module_id)] = module_id
    body = bytearray()
    for index, part in enumerate((text, rodata, data)):
        stored = part.data if part.stored is None else part.stored
        file_offset = 0x100 + len(body) if part.file_offset is None else part.file_offset
        size = len(part.data) if part.size is None else part.size
        stored_size = len(stored) if part.stored_size is None else part.stored_size
        digest = hashlib.sha256(part.data).digest() if part.hash is None else part.hash
        struct.pack_into("<3I", header, 0x10 + 0x10 * index, file_offset, part.memory_offset, size)
        struct.pack_into("<I", header, 0x60 + 0x04 * index, stored_size)
        header[0xA0 + 0x20 * index : 0xC0 + 0x20 * index] = digest
        body += stored
    return bytes(header + body)


#: Contents of the plain three-segment module of :func:`sample`.
TEXT = bytes(range(0x40))
RODATA = b"read-only bytes\x00" * 2 + b"tail"
DATA = bytes(range(0xC0, 0xD8))


def sample(
    *, memory: tuple[int, int, int] = (0x0, 0x1000, 0x2000), flags: int = 0x38, version: int = 0
) -> bytes:
    """Return a module of three raw segments (0x40, 0x24 and 0x18 bytes) at ``memory``."""
    return assemble(
        Part(TEXT, memory[0]),
        Part(RODATA, memory[1]),
        Part(DATA, memory[2]),
        flags=flags,
        version=version,
    )


def segment_rows(header: NsoHeader) -> list[tuple[str, int, int, int, int, bool, bool]]:
    """Return the per-segment header fields as plain tuples, for one-glance comparisons."""
    return [
        (s.name, s.file_offset, s.memory_offset, s.size, s.stored_size, s.compressed, s.check_hash)
        for s in header.segments
    ]


@contextmanager
def traced_peak() -> Iterator[list[int]]:
    """Measure the peak of the allocations made inside the block; the result is in ``[0]``.

    This is how "does not allocate what the header announces" is checked: both Python
    objects and the output buffer of the LZ4 library are counted.
    """
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    baseline = tracemalloc.get_traced_memory()[0]
    peak = [0]
    try:
        yield peak
    finally:
        peak[0] = tracemalloc.get_traced_memory()[1] - baseline
        if not was_tracing:
            tracemalloc.stop()


#: Far below anything a hostile header announces, far above what a small file needs.
SMALL = 1 << 20

# ----------------------------------------------------------------------------------
# The golden files
# ----------------------------------------------------------------------------------


def test_golden_vectors_are_intact() -> None:
    # Guards the hex above against a slip of the keyboard: everything else builds on it.
    assert len(GOLDEN_A) == 392
    assert sha256_hex(GOLDEN_A) == A_SHA256
    assert len(GOLDEN_B) == 383
    assert sha256_hex(GOLDEN_B) == B_SHA256


def test_public_constants() -> None:
    assert nso.NSO_MAGIC == b"NSO0"
    assert nso.HEADER_SIZE == 0x100
    assert nso.MODULE_ID_SIZE == 0x20 == hexutil.BUILD_ID_SIZE
    assert nso.MAX_IMAGE_SIZE == 1 << 30
    assert nso.SEGMENT_NAMES == SEGMENT_NAMES


def test_flag_bits_follow_the_documented_layout() -> None:
    assert {flag.name: flag.value for flag in NsoFlags} == {
        "TEXT_COMPRESSED": 0x01,
        "RODATA_COMPRESSED": 0x02,
        "DATA_COMPRESSED": 0x04,
        "TEXT_HASH": 0x08,
        "RODATA_HASH": 0x10,
        "DATA_HASH": 0x20,
        "EXECUTE_ONLY": 0x40,
        "ZBIC": 0x80,
    }
    header = NsoHeader.parse(GOLDEN_B)
    assert NsoFlags(header.flags) == (
        NsoFlags.TEXT_COMPRESSED
        | NsoFlags.RODATA_COMPRESSED
        | NsoFlags.DATA_COMPRESSED
        | NsoFlags.TEXT_HASH
        | NsoFlags.RODATA_HASH
        | NsoFlags.DATA_HASH
    )


def test_golden_a_header() -> None:
    header = NsoHeader.parse(GOLDEN_A)

    assert header.raw == GOLDEN_A[:0x100]
    assert header.version == 0
    assert header.flags == 0x38
    assert segment_rows(header) == [
        ("text", 0x100, 0x0, 0x40, 0x40, False, True),
        ("rodata", 0x140, 0x1000, 0x28, 0x28, False, True),
        ("data", 0x168, 0x2000, 0x20, 0x20, False, True),
    ]
    assert [segment.memory_end for segment in header.segments] == [0x40, 0x1028, 0x2020]
    assert (header.module_name_offset, header.module_name_size) == (0x100, 0)
    assert header.bss_size == 0x30
    assert header.module_id.hex() == (
        "101112131415161718191a1b1c1d1e1f20212223000000000000000000000000"
    )
    assert header.build_id == GOLDEN_BUILD_ID
    assert header.build_id_full == GOLDEN_BUILD_ID + "0" * 24
    assert {segment.name: segment.hash.hex() for segment in header.segments} == A_HASHES
    assert header.image_size == 0x2020
    assert not header.uses_zbic
    assert header.warnings() == ()


def test_golden_a_segments_image_and_addressing() -> None:
    module = NsoFile.parse(GOLDEN_A)

    assert module.header == NsoHeader.parse(GOLDEN_A)
    assert module.hash_ok == ALL_HASHES_OK
    for name in SEGMENT_NAMES:
        assert module.segment_data(name) == A_SEGMENTS[name]
        assert sha256_hex(module.segment_data(name)) == A_HASHES[name]

    image = module.image()
    assert len(image) == 0x2020  # the 0x30 bytes of bss are not part of it
    assert sha256_hex(image) == A_IMAGE_SHA256
    assert image == A_IMAGE
    # Memory offset -> byte: the first NOP of text, then rodata, then data up to its end.
    for offset, value in [(0x0024, 0x1F), (0x1008, 0x73), (0x2010, 0x00), (0x201F, 0x0F)]:
        assert image[offset] == value
        assert module.read_memory(offset, 1) == bytes([value])
    assert module.read_memory(0x24, 4) == bytes.fromhex("1F2003D5")


def test_golden_a_rewritten_in_image_layout() -> None:
    rewritten = NsoFile.parse(GOLDEN_A).to_bytes()

    assert len(rewritten) == 0x2120 == 8480
    assert sha256_hex(rewritten) == A_REWRITTEN_SHA256
    # A was already uncompressed with text at 0x100: only the two other file offsets move.
    assert differing_offsets(rewritten[:0x100], GOLDEN_A[:0x100]) == [0x20, 0x21, 0x30, 0x31]
    assert (u32(rewritten, 0x10), u32(rewritten, 0x20), u32(rewritten, 0x30)) == (
        0x100,
        0x1100,
        0x2100,
    )
    assert rewritten[0x100:] == A_IMAGE


def test_golden_b_header() -> None:
    header = NsoHeader.parse(GOLDEN_B)

    assert header.version == 0
    assert header.flags == 0x3F
    assert segment_rows(header) == [
        ("text", 0x100, 0x0, 0x1000, 0x42, True, True),
        ("rodata", 0x142, 0x1000, 0x128, 0x1D, True, True),
        ("data", 0x15F, 0x2000, 0x120, 0x20, True, True),
    ]
    assert header.bss_size == 0x30
    assert header.module_id == GOLDEN_MODULE_ID + bytes(12)
    assert header.build_id == GOLDEN_BUILD_ID
    assert {segment.name: segment.hash.hex() for segment in header.segments} == B_HASHES
    assert header.image_size == 0x2120
    assert not header.uses_zbic
    # A compressed segment is expected to have a stored size that differs from its size.
    assert header.warnings() == ()


def test_golden_b_segments_and_image() -> None:
    module = NsoFile.parse(GOLDEN_B)

    assert module.hash_ok == ALL_HASHES_OK
    for name in SEGMENT_NAMES:
        # Against the LZ4 library called directly, and against the header's own hash.
        assert module.segment_data(name) == B_SEGMENTS[name]
        assert sha256_hex(module.segment_data(name)) == B_HASHES[name]
    image = module.image()
    assert len(image) == 0x2120
    assert sha256_hex(image) == B_IMAGE_SHA256
    assert image == B_IMAGE


def test_golden_b_rewritten_in_image_layout() -> None:
    rewritten = NsoFile.parse(GOLDEN_B).to_bytes()

    assert len(rewritten) == 0x2220
    assert sha256_hex(rewritten) == B_REWRITTEN_SHA256
    assert u32(rewritten, 0x0C) == 0x38  # the three compression bits are gone
    # Flags, two file offsets (text was at 0x100 already) and the three stored sizes.
    assert differing_offsets(rewritten[:0x100], GOLDEN_B[:0x100]) == [
        0x0C,
        0x20,
        0x21,
        0x30,
        0x31,
        0x60,
        0x61,
        0x64,
        0x65,
        0x69,
    ]
    # The bytes of the segments did not change, so neither did their hashes.
    assert rewritten[0xA0:0x100] == GOLDEN_B[0xA0:0x100]
    assert rewritten[0x100:] == B_IMAGE


@pytest.mark.parametrize("golden", [GOLDEN_A, GOLDEN_B], ids=["a", "b"])
def test_rewritten_golden_file_parses_back_to_the_same_module(golden: bytes) -> None:
    original = NsoFile.parse(golden)
    rewritten = original.to_bytes()

    again = NsoFile.parse(rewritten)  # verify_hashes is on: the hashes must hold

    assert again.hash_ok == ALL_HASHES_OK
    assert again.header.build_id == original.header.build_id
    for name in SEGMENT_NAMES:
        assert again.segment_data(name) == original.segment_data(name)
        assert (
            again.header.segment(name).memory_offset == original.header.segment(name).memory_offset
        )
        assert not again.header.segment(name).compressed
    assert again.image() == original.image()
    # The image layout is a fixed point: writing it again changes nothing.
    assert again.to_bytes() == rewritten


# ----------------------------------------------------------------------------------
# LZ4 blocks
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("block_hex", "decoded"), LZ4_VECTORS)
def test_lz4_vector_is_confirmed_by_the_lz4_library(block_hex: str, decoded: bytes) -> None:
    block = bytes.fromhex(block_hex)
    assert lz4.block.decompress(block, uncompressed_size=len(decoded)) == decoded


@pytest.mark.parametrize(("block_hex", "decoded"), LZ4_VECTORS)
def test_lz4_vector_is_decoded_inside_a_module(block_hex: str, decoded: bytes) -> None:
    block = bytes.fromhex(block_hex)
    blob = assemble(
        Part(decoded, 0x0, stored=block),
        Part(RODATA, 0x2000),
        Part(DATA, 0x3000),
        flags=0x38 | 0x01,
    )

    module = NsoFile.parse(blob)

    assert module.segment_data("text") == decoded
    assert module.header.segment("text").compressed
    assert module.header.segment("text").stored_size == len(block)
    assert module.hash_ok == ALL_HASHES_OK
    # The neighbours, stored raw right behind the block, are found where the header says.
    assert module.segment_data("rodata") == RODATA
    assert module.segment_data("data") == DATA


@pytest.mark.parametrize(("block_hex", "decoded"), LZ4_VECTORS)
def test_lz4_vector_payload_round_trips_through_build_nso(block_hex: str, decoded: bytes) -> None:
    blob = build_nso(decoded, decoded, decoded)

    module = NsoFile.parse(blob)

    for segment in module.header.segments:
        assert module.segment_data(segment.name) == decoded
        stored = blob[segment.file_offset : segment.file_offset + segment.stored_size]
        # A block is at least one byte longer than its literals, so 4 bytes and less stay
        # raw; the repetitive payloads are what any LZ4 compressor shrinks.
        assert segment.compressed == (len(decoded) >= 20)
        if segment.compressed:
            assert segment.stored_size < segment.size
            # What the writer stored is a raw block that the library reads on its own.
            assert lz4.block.decompress(stored, uncompressed_size=segment.size) == decoded
        else:
            assert stored == decoded


def test_lz4_library_reports_corrupt_blocks_and_returns_short_ones() -> None:
    # Confirms the rejected vectors with the library itself. Two are refused outright ...
    for block_hex, declared in [("10 41 05 00 50 41 41 41 41 41", 20), ("50 41", 5)]:
        with pytest.raises(lz4.block.LZ4BlockError):
            lz4.block.decompress(bytes.fromhex(block_hex), uncompressed_size=declared)
    # ... a block that is longer than the buffer it is given is refused as well ...
    with pytest.raises(lz4.block.LZ4BlockError):
        lz4.block.decompress(bytes.fromhex("1A 41 01 00 50 41 41 41 41 41"), uncompressed_size=10)
    # ... but the size is only a maximum: a shorter result comes back without complaint,
    # which is why the module has to compare lengths itself.
    assert lz4.block.decompress(bytes.fromhex("40 4E 53 4F 30"), uncompressed_size=5) == b"NSO0"


@pytest.mark.parametrize(("block_hex", "declared"), LZ4_REJECTED)
@pytest.mark.parametrize("verify_hashes", [True, False])
def test_rejected_lz4_block_is_a_format_error(
    block_hex: str, declared: int, verify_hashes: bool
) -> None:
    # No hash-check bit is set: the refusal has to come from the block, not from a hash.
    blob = assemble(
        Part(bytes(declared), 0x0, stored=bytes.fromhex(block_hex)),
        Part(RODATA, 0x1000),
        Part(DATA, 0x2000),
        flags=0x01,
    )
    with pytest.raises(FormatError, match="LZ4 block of the text segment"):
        NsoFile.parse(blob, verify_hashes=verify_hashes)


def test_decoded_length_must_equal_the_declared_size() -> None:
    # The text block of B decodes to exactly 0x1000 bytes: one more or one less is wrong.
    for declared in (0xFFF, 0x1001):
        blob = poke(GOLDEN_B, 0x18, declared)
        with pytest.raises(FormatError, match="text"):
            NsoFile.parse(blob, verify_hashes=False)


def test_every_damaged_byte_of_a_compressed_body_is_caught() -> None:
    # Whatever a flipped byte does to a block (corrupt, wrong length, or wrong content
    # that only the hash can tell), the module is refused, and always as a FormatError.
    for offset in range(0x100, len(GOLDEN_B)):
        damaged = bytearray(GOLDEN_B)
        damaged[offset] ^= 0xFF
        with pytest.raises(FormatError):
            NsoFile.parse(bytes(damaged))


def test_highly_compressible_segment_parses() -> None:
    # A megabyte of zeros is about as far as LZ4 expands (close to 255 times): the guard
    # against impossible sizes must not get in the way of what a block can really do.
    zeros = bytes(1 << 20)
    blob = build_nso(zeros, RODATA, DATA)
    stored_size = u32(blob, 0x60)
    assert (1 << 20) // 255 < stored_size < (1 << 20) // 250

    module = NsoFile.parse(blob)

    assert module.segment_data("text") == zeros
    assert module.hash_ok == ALL_HASHES_OK


# ----------------------------------------------------------------------------------
# Header-only parsing and files
# ----------------------------------------------------------------------------------


def test_header_parse_needs_only_the_first_0x100_bytes() -> None:
    expected = NsoHeader.parse(GOLDEN_B)

    assert expected.raw == GOLDEN_B[:0x100]
    assert NsoHeader.parse(GOLDEN_B[:0x100]) == expected
    assert NsoHeader.parse(bytearray(GOLDEN_B)) == expected
    assert NsoHeader.parse(memoryview(GOLDEN_B)) == expected
    assert NsoHeader.parse(GOLDEN_B + b"whatever follows") == expected
    # A view whose items are not bytes is still read byte by byte.
    padded = GOLDEN_B + bytes(-len(GOLDEN_B) % 4)
    assert NsoHeader.parse(memoryview(padded).cast("I")) == expected


def test_parsed_values_are_immutable() -> None:
    header = NsoHeader.parse(GOLDEN_A)
    with pytest.raises(AttributeError):
        header.flags = 0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        header.segments[0].size = 0  # type: ignore[misc]
    module = NsoFile.parse(GOLDEN_A)
    with pytest.raises(TypeError):
        module.hash_ok["text"] = False  # type: ignore[index]
    assert hash(header) == hash(NsoHeader.parse(GOLDEN_A))


def test_parsing_keeps_no_view_of_the_callers_buffer() -> None:
    # A bytearray with a live memoryview cannot be resized. Parsing must not leave one
    # behind, not even while the exception of a failed parse is still being handled.
    buffer = bytearray(GOLDEN_B)
    NsoHeader.parse(buffer)
    module = NsoFile.parse(buffer)
    buffer += b"more"
    buffer[0x100] ^= 0xFF  # and the parsed module does not alias the buffer
    assert module.segment_data("text") == B_SEGMENTS["text"]

    truncated = bytearray(GOLDEN_B[:0x120])
    try:
        NsoFile.parse(truncated)
    except FormatError:
        truncated += b"more"
    else:
        pytest.fail("a truncated module was accepted")


def test_is_nso_looks_at_the_magic_only() -> None:
    assert nso.is_nso(GOLDEN_A)
    assert nso.is_nso(GOLDEN_A[:0x100])
    assert nso.is_nso(b"NSO0")
    assert nso.is_nso(bytearray(b"NSO0 and anything else"))
    assert not nso.is_nso(b"")
    assert not nso.is_nso(b"NSO")
    assert not nso.is_nso(b"NSO1" + GOLDEN_A[4:])
    assert not nso.is_nso(b"nso0")
    assert not nso.is_nso(b"\x00NSO0")


def test_read_header_works_on_a_file_whose_body_is_missing(tmp_path: Path) -> None:
    # Only the header is on disk, so nothing beyond 0x100 can possibly be read.
    path = tmp_path / "main"
    path.write_bytes(GOLDEN_B[:0x100])

    header = nso.read_header(path)

    assert header == NsoHeader.parse(GOLDEN_B)
    assert header.build_id == GOLDEN_BUILD_ID
    assert nso.read_header(str(path)) == header
    # The same file is not a readable module: its segments are not there.
    with pytest.raises(FormatError, match="text"):
        NsoFile.read(path)


def test_read_header_and_read_on_a_complete_file(tmp_path: Path) -> None:
    path = tmp_path / "sdk"
    path.write_bytes(GOLDEN_B + bytes(0x4000))  # trailing bytes belong to nobody

    assert nso.read_header(path) == NsoHeader.parse(GOLDEN_B)
    module = NsoFile.read(path)
    assert module.image() == B_IMAGE
    assert module.hash_ok == ALL_HASHES_OK
    assert NsoFile.read(str(path), verify_hashes=False).image() == B_IMAGE


def test_reading_reports_bad_files_and_missing_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    short = tmp_path / "short"
    short.write_bytes(GOLDEN_A[:0xFF])
    other = tmp_path / "other"
    other.write_bytes(b"PFS0" + bytes(0x200))

    for path in (empty, short):
        with pytest.raises(FormatError, match="truncated"):
            nso.read_header(path)
        with pytest.raises(FormatError, match="truncated"):
            NsoFile.read(path)
    with pytest.raises(FormatError, match="magic"):
        nso.read_header(other)
    with pytest.raises(FormatError, match="magic"):
        NsoFile.read(other)
    with pytest.raises(FileNotFoundError):
        nso.read_header(tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        NsoFile.read(tmp_path / "missing")


def test_reading_works_beyond_260_characters(tmp_path: Path) -> None:
    # An ExeFS inside a deep output tree: well past the classic Windows path limit.
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/main"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, GOLDEN_A)

    assert nso.read_header(target).build_id == GOLDEN_BUILD_ID
    assert NsoFile.read(target).image() == A_IMAGE


# ----------------------------------------------------------------------------------
# Build IDs
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_id", "canonical"),
    [
        pytest.param(
            bytes(range(0x10, 0x24)), "101112131415161718191A1B1C1D1E1F20212223", id="20-bytes"
        ),
        pytest.param(bytes(range(0xA0, 0xB0)), "A0A1A2A3A4A5A6A7A8A9AAABACADAEAF", id="16-bytes"),
        pytest.param(
            bytes(range(0x01, 0x21)),
            "0102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F20",
            id="32-bytes",
        ),
        pytest.param(b"", "00", id="all-zero"),
        # Trailing zero *bytes* are padding; the zero digit that ends "10" is not.
        pytest.param(bytes.fromhex("ABCD10"), "ABCD10", id="zero-low-nibble"),
        pytest.param(bytes.fromhex("00AB00CD"), "00AB00CD", id="leading-and-inner-zero-bytes"),
    ],
)
def test_build_id_forms(module_id: bytes, canonical: str) -> None:
    padded = module_id + bytes(32 - len(module_id))
    blob = bytearray(GOLDEN_A)
    blob[0x40:0x60] = padded

    header = NsoHeader.parse(blob)

    assert header.module_id == padded
    assert header.build_id == canonical
    assert header.build_id_full == "".join(f"{byte:02X}" for byte in padded)
    assert len(header.build_id_full) == 64
    # Both spellings name the same ID once zero-padded, which is how loaders compare.
    assert hexutil.build_ids_equal(header.build_id, header.build_id_full)
    # The writer pads, the reader trims, and a rewrite keeps the ID where it is.
    built = build_nso(TEXT, RODATA, DATA, module_id=module_id)
    assert built[0x40:0x60] == padded
    assert NsoHeader.parse(built).build_id == canonical
    assert NsoFile.parse(built).to_bytes()[0x40:0x60] == padded


# ----------------------------------------------------------------------------------
# Only the header fields are followed
# ----------------------------------------------------------------------------------


def test_segments_in_any_file_order_with_a_name_byte_before_text() -> None:
    text = bytes(range(0x30, 0x61))
    rodata = b"strings\x00tables\x00" * 2
    data = bytes(range(0xD0, 0xE8))
    rodata_block = lz4.block.compress(rodata, store_size=False)
    assert len(rodata_block) < len(rodata)

    # The body, written out piece by piece: a module-name byte, then text at 0x101, then
    # data *before* rodata, with filler that no header field refers to in between.
    body = bytearray(b"m")
    text_at = 0x100 + len(body)
    body += text + b"\xee" * 7
    data_at = 0x100 + len(body)
    body += data + b"\xee" * 3
    rodata_at = 0x100 + len(body)
    body += rodata_block + b"\xee" * 5
    assert text_at == 0x101

    header = bytearray(0x100)
    header[0x00:0x04] = b"NSO0"
    struct.pack_into("<I", header, 0x0C, 0x38 | 0x02)  # hashes checked, rodata compressed
    struct.pack_into("<3I", header, 0x10, text_at, 0x0, len(text))
    struct.pack_into("<3I", header, 0x20, rodata_at, 0x1000, len(rodata))
    struct.pack_into("<3I", header, 0x30, data_at, 0x2000, len(data))
    struct.pack_into("<I", header, 0x1C, 0x100)  # module-name offset
    struct.pack_into("<I", header, 0x2C, 1)  # module-name size
    struct.pack_into("<3I", header, 0x60, len(text), len(rodata_block), len(data))
    header[0xA0:0xC0] = hashlib.sha256(text).digest()
    header[0xC0:0xE0] = hashlib.sha256(rodata).digest()
    header[0xE0:0x100] = hashlib.sha256(data).digest()

    module = NsoFile.parse(bytes(header + body))

    assert module.segment_data("text") == text
    assert module.segment_data("rodata") == rodata
    assert module.segment_data("data") == data
    assert module.hash_ok == ALL_HASHES_OK
    assert (module.header.module_name_offset, module.header.module_name_size) == (0x100, 1)
    assert module.header.warnings() == ()
    assert module.image() == image_of([(0x0, text), (0x1000, rodata), (0x2000, data)])
    # Rewriting drops the name byte and the filler: the layout becomes the canonical one.
    written = module.to_bytes()
    assert (u32(written, 0x1C), u32(written, 0x2C)) == (0x100, 0)
    assert written[0x100:] == module.image()


def test_file_ranges_are_not_second_guessed() -> None:
    # Two segments read the same file bytes and the third reads the magic itself: odd,
    # but each range lies inside the file, and that is all a loader asks.
    shared = bytes(range(0x80, 0xA0))
    blob = assemble(
        Part(shared, 0x0, file_offset=0x100),
        Part(shared, 0x1000, stored=b"", file_offset=0x100),
        Part(b"NSO0", 0x2000, stored=b"", file_offset=0x0),
    )

    module = NsoFile.parse(blob)

    assert module.segment_data("text") == shared
    assert module.segment_data("rodata") == shared
    assert module.segment_data("data") == b"NSO0"
    assert module.hash_ok == ALL_HASHES_OK


@pytest.mark.parametrize("stored_size", [0, 0x3F, 0x45, 0xFFFFFFFF])
def test_uncompressed_segment_is_read_by_its_size_not_its_stored_size(stored_size: int) -> None:
    blob = assemble(
        Part(TEXT, 0x0, stored_size=stored_size), Part(RODATA, 0x1000), Part(DATA, 0x2000)
    )

    module = NsoFile.parse(blob)

    assert module.segment_data("text") == TEXT
    assert module.header.segment("text").size == 0x40
    assert module.header.segment("text").stored_size == stored_size
    assert module.hash_ok == ALL_HASHES_OK
    (warning,) = module.header.warnings()
    assert "text" in warning
    assert "stored size" in warning


def test_empty_segments_are_fine() -> None:
    blob = assemble(Part(b"", 0x0), Part(RODATA, 0x0), Part(b"", 0x1000))

    module = NsoFile.parse(blob)

    assert module.segment_data("text") == b""
    assert module.segment_data("data") == b""
    assert module.hash_ok == ALL_HASHES_OK
    # An empty segment at 0x1000 still extends the image to there.
    assert module.image() == RODATA + bytes(0x1000 - len(RODATA))
    assert module.find_segment(0x0, 1) is module.header.segment("rodata")
    assert NsoFile.parse(module.to_bytes()).image() == module.image()

    nothing = NsoFile.parse(assemble(Part(b"", 0x0), Part(b"", 0x0), Part(b"", 0x0)))
    assert nothing.image() == b""
    assert len(nothing.to_bytes()) == 0x100


# ----------------------------------------------------------------------------------
# Malformed files
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 3, 4, 0x40, 0xFF])
def test_short_header_is_a_format_error(length: int) -> None:
    with pytest.raises(FormatError, match="truncated header"):
        NsoHeader.parse(GOLDEN_A[:length])
    with pytest.raises(FormatError, match="truncated header"):
        NsoFile.parse(GOLDEN_A[:length])


@pytest.mark.parametrize("magic", [b"NSO1", b"nso0", b"NRO0", b"0OSN", b"\x00\x00\x00\x00"])
def test_wrong_magic_is_a_format_error(magic: bytes) -> None:
    blob = magic + GOLDEN_A[4:]
    with pytest.raises(FormatError, match="magic"):
        NsoHeader.parse(blob)
    with pytest.raises(FormatError, match="magic"):
        NsoFile.parse(blob)
    # Data that is both too short and not an NSO is reported as "not an NSO".
    with pytest.raises(FormatError, match="magic"):
        NsoHeader.parse(blob[:8])
    with pytest.raises(FormatError, match="magic"):
        NsoHeader.parse(b"XY")


@pytest.mark.parametrize("golden", [GOLDEN_A, GOLDEN_B], ids=["a", "b"])
def test_every_truncation_of_a_module_is_a_format_error(golden: bytes) -> None:
    # The last segment of both files ends with the file, so any missing byte is missed.
    for length in range(len(golden)):
        with pytest.raises(FormatError):
            NsoFile.parse(golden[:length])
        with pytest.raises(FormatError):
            NsoFile.parse(golden[:length], verify_hashes=False)


@pytest.mark.parametrize(
    ("golden", "offset", "value", "segment"),
    [
        pytest.param(GOLDEN_A, 0x38, 0x21, "data", id="raw-size-one-byte-too-long"),
        pytest.param(GOLDEN_A, 0x10, 0x149, "text", id="raw-file-offset-too-far"),
        pytest.param(GOLDEN_A, 0x20, 0x188, "rodata", id="raw-starts-at-the-end"),
        pytest.param(GOLDEN_A, 0x30, 0xFFFFFFFF, "data", id="raw-file-offset-u32-max"),
        pytest.param(GOLDEN_B, 0x68, 0x21, "data", id="stored-size-one-byte-too-long"),
        pytest.param(GOLDEN_B, 0x60, 0x80000000, "text", id="stored-size-huge"),
        pytest.param(GOLDEN_B, 0x20, 0x17F, "rodata", id="block-starts-at-the-end"),
    ],
)
def test_stored_range_outside_the_file_is_a_format_error(
    golden: bytes, offset: int, value: int, segment: str
) -> None:
    blob = poke(golden, offset, value)
    with pytest.raises(FormatError, match=f"the {segment} segment does not fit in the file"):
        NsoFile.parse(blob)
    # The header alone stays readable: that is what makes the build ID always available.
    assert NsoHeader.parse(blob).build_id == GOLDEN_BUILD_ID


@pytest.mark.parametrize(
    ("memory", "pair"),
    [
        pytest.param((0x0, 0x20, 0x2000), "text.*rodata", id="rodata-starts-inside-text"),
        pytest.param((0x0, 0x3F, 0x2000), "text.*rodata", id="rodata-on-the-last-byte-of-text"),
        pytest.param((0x1010, 0x1000, 0x2000), "text.*rodata", id="text-inside-rodata"),
        pytest.param((0x0, 0x1000, 0x1023), "rodata.*data", id="data-on-the-last-byte-of-rodata"),
        pytest.param((0x0, 0x1000, 0xFF0), "rodata.*data", id="data-straddles-rodata-start"),
        pytest.param((0x0, 0x1000, 0x0), "text.*data", id="data-on-top-of-text"),
        pytest.param((0x1000, 0x1000, 0x1000), "text.*rodata", id="all-at-the-same-offset"),
    ],
)
def test_overlapping_memory_ranges_are_a_format_error(
    memory: tuple[int, int, int], pair: str
) -> None:
    blob = sample(memory=memory)
    with pytest.raises(FormatError, match=f"overlap in memory: {pair}"):
        NsoFile.parse(blob)
    with pytest.raises(FormatError, match="overlap"):
        NsoFile.parse(blob, verify_hashes=False)
    # The header itself parses, and says what is wrong with the layout.
    assert NsoHeader.parse(blob).warnings() != ()


@pytest.mark.parametrize(
    "memory",
    [
        pytest.param((0x0, 0x40, 0x2000), id="rodata-right-after-text"),
        pytest.param((0x0, 0x1000, 0x1024), id="data-right-after-rodata"),
        pytest.param((0x3C, 0x18, 0x0), id="reverse-order-back-to-back"),
    ],
)
def test_segments_that_only_touch_do_not_overlap(memory: tuple[int, int, int]) -> None:
    module = NsoFile.parse(sample(memory=memory))
    expected = image_of([(memory[0], TEXT), (memory[1], RODATA), (memory[2], DATA)])
    assert module.image() == expected


def test_empty_segment_inside_another_one_is_not_an_overlap() -> None:
    # An empty segment occupies no byte, so no byte of the image is ambiguous.
    blob = assemble(Part(TEXT, 0x0), Part(b"", 0x10), Part(DATA, 0x1000))
    module = NsoFile.parse(blob)
    assert module.image() == image_of([(0x0, TEXT), (0x1000, DATA)])
    assert module.find_segment(0x10, 1) is module.header.segment("text")


def test_hostile_sizes_are_refused_before_anything_is_allocated() -> None:
    hostile = {
        "raw text of 2 GiB": poke(GOLDEN_A, 0x18, 0x7FFFFFFF),
        "raw data of 2 GiB": poke(GOLDEN_A, 0x38, 0x7FFFFFFF),
        "raw data of 4 GiB": poke(GOLDEN_A, 0x38, 0xFFFFFFFF),
        "raw data just inside the image limit": poke(GOLDEN_A, 0x38, 0x3FFFD000),
        "data at the top of the address space": poke(GOLDEN_A, 0x34, 0xFFFFF000),
        "compressed text of 2 GiB": poke(GOLDEN_B, 0x18, 0x7FFFFFFF),
        "compressed data just inside the image limit": poke(GOLDEN_B, 0x38, 0x3FFFD000),
        "stored size of 4 GiB": poke(GOLDEN_B, 0x68, 0xFFFFFFFF),
        "file offset of 4 GiB": poke(GOLDEN_B, 0x30, 0xFFFFFFFF),
    }
    for label, blob in hostile.items():
        for verify_hashes in (True, False):
            with traced_peak() as peak, pytest.raises(FormatError):
                NsoFile.parse(blob, verify_hashes=verify_hashes)
            assert peak[0] < SMALL, label


def test_a_tiny_block_cannot_claim_a_gigabyte() -> None:
    # 0x20 stored bytes are said to decode to almost 1 GiB. The layout is legal (data is
    # the last segment and the image stays inside the limit), so only the knowledge of
    # what LZ4 can do refuses this without first allocating the gigabyte.
    blob = poke(GOLDEN_B, 0x38, 0x3FFFD000)
    assert NsoHeader.parse(blob).image_size == 0x3FFFF000 < nso.MAX_IMAGE_SIZE
    with traced_peak() as peak, pytest.raises(FormatError, match="LZ4 block of the data segment"):
        NsoFile.parse(blob)
    assert peak[0] < SMALL


def test_image_size_limit_is_inclusive_and_costs_no_memory() -> None:
    limit = nso.MAX_IMAGE_SIZE
    # A sparse layout whose data segment ends exactly at the limit.
    at_limit = sample(memory=(0x0, 0x1000, limit - len(DATA)))
    with traced_peak() as peak:
        module = NsoFile.parse(at_limit)
    assert module.header.image_size == limit
    assert module.segment_data("data") == DATA
    assert peak[0] < SMALL  # parsing does not build the image

    beyond = sample(memory=(0x0, 0x1000, limit - len(DATA) + 1))
    with pytest.raises(FormatError, match="memory image too large: the data segment"):
        NsoFile.parse(beyond)
    assert NsoHeader.parse(beyond).image_size == limit + 1  # the header alone still parses


def test_image_size_limit_applies_to_reader_and_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the limit lowered to the size of A's image, the boundary is cheap to walk.
    monkeypatch.setattr(nso, "MAX_IMAGE_SIZE", 0x2020)
    assert len(NsoFile.parse(GOLDEN_A).image()) == 0x2020
    assert build_nso(*A_SEGMENTS.values(), compress=False)[0x100:] == GOLDEN_A[0x100:]

    monkeypatch.setattr(nso, "MAX_IMAGE_SIZE", 0x201F)
    with pytest.raises(FormatError, match="memory image too large"):
        NsoFile.parse(GOLDEN_A)
    with pytest.raises(FormatError, match="memory image too large"):
        NsoFile(NsoHeader.parse(GOLDEN_A), list(A_SEGMENTS.values()))
    # The writer does not produce what the reader would refuse.
    with pytest.raises(ValueError, match="data segment"):
        build_nso(*A_SEGMENTS.values())


# ----------------------------------------------------------------------------------
# Hashes
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "file_offset", "check_bit"),
    [("text", 0x100, 0x08), ("rodata", 0x150, 0x10), ("data", 0x187, 0x20)],
)
def test_hash_mismatch_on_a_raw_segment(name: str, file_offset: int, check_bit: int) -> None:
    damaged = bytearray(GOLDEN_A)
    damaged[file_offset] ^= 0x01
    blob = bytes(damaged)
    expected = {other: other != name for other in SEGMENT_NAMES}

    # The check bit is set and hashes are verified: refused, as a loader would.
    with pytest.raises(FormatError, match=f"SHA-256 mismatch: the {name} hash"):
        NsoFile.parse(blob)

    # Not verifying: the module is readable and says which segment is off.
    module = NsoFile.parse(blob, verify_hashes=False)
    assert module.hash_ok == expected
    start = module.header.segment(name).file_offset
    assert module.segment_data(name) == blob[start : start + module.header.segment(name).size]

    # Check bit clear: the loader would not look, so neither does parse.
    unchecked = poke(blob, 0x0C, 0x38 & ~check_bit)
    module = NsoFile.parse(unchecked)
    assert module.hash_ok == expected
    assert not module.header.segment(name).check_hash


def test_hash_mismatch_when_the_recorded_hash_is_wrong() -> None:
    blob = bytearray(GOLDEN_B)
    blob[0xC0] ^= 0x80  # first byte of the rodata hash; the segment itself is intact
    with pytest.raises(FormatError, match="rodata"):
        NsoFile.parse(bytes(blob))
    module = NsoFile.parse(bytes(blob), verify_hashes=False)
    assert module.hash_ok == {"text": True, "rodata": False, "data": True}
    assert module.segment_data("rodata") == B_SEGMENTS["rodata"]


def test_every_mismatching_checked_segment_is_named() -> None:
    damaged = bytearray(GOLDEN_A)
    damaged[0x100] ^= 0x01  # text
    damaged[0x187] ^= 0x01  # data
    with pytest.raises(FormatError) as caught:
        NsoFile.parse(bytes(damaged))
    message = str(caught.value)
    assert "the text hash" in message
    assert "the data hash" in message
    assert "rodata" not in message
    # Expected and found are both spelled out.
    assert A_HASHES["text"] in message
    assert sha256_hex(bytes(damaged[0x100:0x140])) in message


# ----------------------------------------------------------------------------------
# ZBIC
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [0x80 | 0x3F, 0x80 | 0x01, 0x80 | 0x02, 0x80 | 0x04, 0x80 | 0x3A])
def test_zbic_compressed_module_is_unsupported(flags: int) -> None:
    # The stored bytes of B are valid LZ4 blocks; with bit 7 set they must not be read
    # as such all the same.
    blob = poke(GOLDEN_B, 0x0C, flags)

    with pytest.raises(UnsupportedInputError, match="ZBIC"):
        NsoFile.parse(blob)
    with pytest.raises(UnsupportedInputError, match="not supported by this version of BEA"):
        NsoFile.parse(blob, verify_hashes=False)

    # The header, and with it the build ID, stays readable.
    header = NsoHeader.parse(blob)
    assert header.uses_zbic
    assert header.build_id == GOLDEN_BUILD_ID


def test_zbic_is_reported_as_unsupported_not_as_malformed() -> None:
    blob = poke(GOLDEN_B, 0x0C, 0xBF)
    with pytest.raises(BeaError) as caught:
        NsoFile.parse(blob)
    assert isinstance(caught.value, UnsupportedInputError)
    assert not isinstance(caught.value, FormatError)
    # Even a ZBIC module that is truncated right after its header is "unsupported": BEA
    # cannot judge storage it does not understand.
    with pytest.raises(UnsupportedInputError):
        NsoFile.parse(blob[:0x100])


def test_zbic_flag_with_nothing_compressed_is_readable() -> None:
    blob = poke(GOLDEN_A, 0x0C, 0x38 | 0x80)

    module = NsoFile.parse(blob)

    assert not module.header.uses_zbic
    assert module.header.flags == 0xB8
    assert module.image() == A_IMAGE
    assert module.hash_ok == ALL_HASHES_OK
    # Written back uncompressed, the flag is cleared: the result is A's own rewrite.
    assert sha256_hex(module.to_bytes()) == A_REWRITTEN_SHA256


# ----------------------------------------------------------------------------------
# Warnings
# ----------------------------------------------------------------------------------


def only_warning(blob: bytes) -> str:
    """Return the single warning of a header, checking that it reads as a sentence."""
    (warning,) = NsoHeader.parse(blob).warnings()
    assert warning[0].isupper()
    assert warning.endswith(".")
    return warning


def test_clean_headers_have_no_warnings() -> None:
    for blob in (GOLDEN_A, GOLDEN_B, sample(), build_nso(TEXT, RODATA, DATA)):
        assert NsoHeader.parse(blob).warnings() == ()


def test_warning_for_a_version_other_than_zero() -> None:
    blob = poke(GOLDEN_A, 0x04, 3)
    assert "version is 3" in only_warning(blob)
    assert NsoFile.parse(blob).image() == A_IMAGE  # a warning, not an error


@pytest.mark.parametrize("extra", [0x100, 0x80000000, 0xFFFFFF00])
def test_warning_for_undefined_flag_bits(extra: int) -> None:
    blob = poke(GOLDEN_A, 0x0C, 0x38 | extra)
    warning = only_warning(blob)
    assert "undefined" in warning
    assert f"0x{extra:08X}" in warning
    assert NsoFile.parse(blob).image() == A_IMAGE


def test_warning_for_the_execute_only_flag() -> None:
    blob = poke(GOLDEN_A, 0x0C, 0x38 | 0x40)
    assert "execute-only" in only_warning(blob)
    assert NsoFile.parse(blob).image() == A_IMAGE


@pytest.mark.parametrize(
    ("field", "value", "name"),
    [(0x24, 0x1008, "rodata"), (0x24, 0x1FD7, "rodata"), (0x34, 0x2004, "data")],
)
def test_warning_for_a_memory_offset_off_the_page_grid(field: int, value: int, name: str) -> None:
    blob = poke(GOLDEN_A, field, value)
    warning = only_warning(blob)
    assert f"The {name} memory offset 0x{value:X}" in warning
    assert "0x1000" in warning
    assert NsoFile.parse(blob).segment_data(name) == A_SEGMENTS[name]


def test_warning_for_text_away_from_offset_zero() -> None:
    blob = sample(memory=(0x1000, 0x2000, 0x3000))
    warning = only_warning(blob)
    assert "text memory offset is 0x1000" in warning
    assert NsoFile.parse(blob).image() == image_of(
        [(0x1000, TEXT), (0x2000, RODATA), (0x3000, DATA)]
    )

    # Off the grid as well: both rules are reported.
    warnings = NsoHeader.parse(poke(GOLDEN_A, 0x14, 0x40)).warnings()
    assert len(warnings) == 2
    assert all("text memory offset" in warning for warning in warnings)


def test_warning_for_segments_out_of_order() -> None:
    # data before rodata, nothing overlapping: one rule broken.
    blob = sample(memory=(0x0, 0x2000, 0x1000))
    warning = only_warning(blob)
    assert "rodata segment ends at 0x2024" in warning
    assert "start of data at 0x1000" in warning
    assert NsoFile.parse(blob).image() == image_of([(0x0, TEXT), (0x2000, RODATA), (0x1000, DATA)])

    # rodata before text: text is no longer at 0, and it ends after rodata starts.
    warnings = NsoHeader.parse(sample(memory=(0x1000, 0x0, 0x2000))).warnings()
    assert len(warnings) == 2
    assert any("text memory offset is 0x1000" in warning for warning in warnings)
    assert any(
        "text segment ends at 0x1040" in warning and "start of rodata at 0x0" in warning
        for warning in warnings
    )


def test_warnings_accumulate_and_are_sentences() -> None:
    blob = assemble(
        Part(TEXT, 0x1008, stored_size=1),
        Part(RODATA, 0x10),
        Part(DATA, 0x4),
        flags=0x38 | 0x40 | 0x200,
        version=9,
    )
    warnings = NsoHeader.parse(blob).warnings()
    # version, undefined bits, bit 6, three offsets off the grid, text not at 0, two order
    # violations, one stored size.
    assert len(warnings) == 10
    assert len(set(warnings)) == 10
    for warning in warnings:
        assert warning[0].isupper()
        assert warning.endswith(".")


# ----------------------------------------------------------------------------------
# bss
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bss_size", [0x70000000, 0xFFFFFFFF])
def test_bss_is_only_a_number(bss_size: int) -> None:
    blob = build_nso(TEXT, RODATA, DATA, bss_size=bss_size)
    assert u32(blob, 0x3C) == bss_size

    with traced_peak() as peak:
        module = NsoFile.parse(blob)
        image = module.image()
        rewritten = module.to_bytes()

    assert peak[0] < SMALL  # nowhere near the gigabytes of bss
    assert module.header.bss_size == bss_size
    assert image == image_of([(0x0, TEXT), (0x1000, RODATA), (0x2000, DATA)])
    assert module.header.image_size == len(image) == 0x2000 + len(DATA)
    assert len(rewritten) == 0x100 + len(image)
    assert u32(rewritten, 0x3C) == bss_size
    # bss is not part of any segment: it can be neither read nor patched.
    assert module.find_segment(len(image), 1) is None
    with pytest.raises(ValueError):
        module.read_memory(len(image), 1)


# ----------------------------------------------------------------------------------
# Addressing: find_segment and read_memory
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "length", "expected"),
    [
        # A: text 0x0..0x40, rodata 0x1000..0x1028, data 0x2000..0x2020.
        (0x0, 1, "text"),
        (0x0, 0x40, "text"),
        (0x24, 4, "text"),
        (0x3F, 1, "text"),
        (0x1000, 1, "rodata"),
        (0x1000, 0x28, "rodata"),
        (0x1027, 1, "rodata"),
        (0x2000, 0x20, "data"),
        (0x201F, 1, "data"),
        (0x0, 0x41, None),  # one byte past the end of text
        (0x3F, 2, None),
        (0x40, 1, None),  # the gap right after text
        (0x800, 4, None),
        (0xFFF, 1, None),  # the last byte of the gap
        (0xFFF, 2, None),  # from the gap into rodata
        (0x1027, 2, None),
        (0x1028, 1, None),
        (0x1FFF, 2, None),
        (0x201F, 2, None),  # off the end of the image
        (0x2020, 1, None),  # where bss begins
        (0x2040, 8, None),
        (0x0, 0x2020, None),  # the whole image spans three segments
        (-1, 1, None),
        (-1, 2, None),
        (0xFFFFFFFF, 1, None),
    ],
)
def test_find_segment_and_read_memory(offset: int, length: int, expected: str | None) -> None:
    module = NsoFile.parse(GOLDEN_A)
    found = module.find_segment(offset, length)
    if expected is None:
        assert found is None
        with pytest.raises(ValueError, match="inside one segment"):
            module.read_memory(offset, length)
    else:
        assert found is module.header.segment(expected)
        assert module.read_memory(offset, length) == A_IMAGE[offset : offset + length]


def test_adjacent_segments_are_still_two_segments() -> None:
    # In B rodata starts exactly where text ends: memory is contiguous there, but a range
    # across the boundary belongs to no single segment.
    module = NsoFile.parse(GOLDEN_B)
    assert module.find_segment(0xFFF, 1) is module.header.segment("text")
    assert module.find_segment(0x1000, 1) is module.header.segment("rodata")
    assert module.find_segment(0xFFF, 2) is None
    assert module.read_memory(0xFFF, 1) == B_IMAGE[0xFFF:0x1000]
    assert module.read_memory(0x1000, 0x128) == B_SEGMENTS["rodata"]
    with pytest.raises(ValueError):
        module.read_memory(0xFFF, 2)


@pytest.mark.parametrize("length", [0, -1, -0x40])
def test_memory_ranges_hold_at_least_one_byte(length: int) -> None:
    module = NsoFile.parse(GOLDEN_A)
    with pytest.raises(ValueError, match="at least 1 byte"):
        module.find_segment(0x10, length)
    with pytest.raises(ValueError, match="at least 1 byte"):
        module.read_memory(0x10, length)


def test_unknown_segment_names_are_rejected() -> None:
    module = NsoFile.parse(GOLDEN_A)
    assert module.header.segment("rodata") is module.header.segments[1]
    for name in ("bss", "TEXT", ".text", ""):
        with pytest.raises(ValueError, match="unknown segment"):
            module.header.segment(name)
        with pytest.raises(ValueError, match="unknown segment"):
            module.segment_data(name)


# ----------------------------------------------------------------------------------
# Patching and writing back
# ----------------------------------------------------------------------------------

#: Patch sites on B, as (memory offset, length): the first and the last byte of every
#: segment are among them.
B_PATCH_SITES = [
    (0x0, 1),
    (0x24, 4),
    (0xFFF, 1),
    (0x1000, 2),
    (0x1127, 1),
    (0x2000, 3),
    (0x211C, 4),
]


def inverted(image: bytes, offset: int, length: int) -> bytes:
    """Return replacement bytes that differ from the image at every single position."""
    return bytes(byte ^ 0xFF for byte in image[offset : offset + length])


def b_patches() -> list[tuple[int, bytes]]:
    return [(offset, inverted(B_IMAGE, offset, length)) for offset, length in B_PATCH_SITES]


def test_with_patches_changes_exactly_the_requested_bytes() -> None:
    original = NsoFile.parse(GOLDEN_B)
    patches = b_patches()
    expected = bytearray(B_IMAGE)
    for offset, replacement in patches:
        expected[offset : offset + len(replacement)] = replacement

    patched = original.with_patches(patches)

    assert patched is not original
    assert patched.image() == bytes(expected)
    for offset, replacement in patches:
        assert patched.read_memory(offset, len(replacement)) == replacement
    # The original object is untouched, down to its header.
    assert original.image() == B_IMAGE
    assert original.header == NsoHeader.parse(GOLDEN_B)
    for name in SEGMENT_NAMES:
        assert original.segment_data(name) == B_SEGMENTS[name]
    # Neither the order nor the kind of iterable matters.
    assert original.with_patches(reversed(patches)).image() == bytes(expected)
    assert original.with_patches(iter(patches)).image() == bytes(expected)
    assert original.with_patches((o, bytearray(r)) for o, r in patches).image() == bytes(expected)


def test_patched_module_carries_the_header_that_to_bytes_writes() -> None:
    original = NsoFile.parse(GOLDEN_B)

    patched = original.with_patches([(0x24, bytes.fromhex("AABBCCDD"))])

    # Only text changed.
    assert patched.segment_data("text") != B_SEGMENTS["text"]
    assert patched.segment_data("rodata") == B_SEGMENTS["rodata"]
    assert patched.segment_data("data") == B_SEGMENTS["data"]
    # hash_ok speaks about the patched bytes and the header of the patched module ...
    assert patched.hash_ok == ALL_HASHES_OK
    new_text_hash = hashlib.sha256(patched.segment_data("text")).digest()
    assert patched.header.segment("text").hash == new_text_hash
    assert new_text_hash != original.header.segment("text").hash
    assert patched.header.segment("rodata").hash == original.header.segment("rodata").hash
    # ... which is the header of an uncompressed file in image layout.
    assert patched.header == NsoHeader.parse(patched.to_bytes())
    assert patched.header.flags == 0x38
    assert segment_rows(patched.header) == [
        ("text", 0x100, 0x0, 0x1000, 0x1000, False, True),
        ("rodata", 0x1100, 0x1000, 0x128, 0x128, False, True),
        ("data", 0x2100, 0x2000, 0x120, 0x120, False, True),
    ]
    assert patched.header.build_id == GOLDEN_BUILD_ID
    assert patched.header.bss_size == 0x30


def test_patched_module_written_back_matches_its_image_byte_for_byte() -> None:
    patches = b_patches()
    patched_offsets = {
        offset + index for offset, replacement in patches for index in range(len(replacement))
    }

    written = NsoFile.parse(GOLDEN_B).with_patches(patches).to_bytes()

    reparsed = NsoFile.parse(written)  # the hashes were recomputed, so they verify
    after = reparsed.image()
    assert reparsed.hash_ok == ALL_HASHES_OK
    assert len(after) == len(B_IMAGE)
    # The image differs from the original one exactly at the patched offsets ...
    changed = {m for m in range(len(after)) if after[m] != B_IMAGE[m]}
    assert changed == patched_offsets
    # ... and the file is the image shifted by the header: the address space of IPS.
    assert len(written) == 0x100 + len(after)
    assert all(written[0x100 + m] == after[m] for m in range(len(after)))
    assert reparsed.header.build_id == GOLDEN_BUILD_ID


def test_with_no_patches_the_result_is_the_same_module_in_image_layout() -> None:
    original = NsoFile.parse(GOLDEN_B)
    same = original.with_patches([])
    assert same is not original
    assert same.image() == original.image()
    assert same.hash_ok == ALL_HASHES_OK
    assert same.header.flags == 0x38
    assert same.to_bytes() == original.to_bytes()


def test_a_whole_segment_can_be_replaced() -> None:
    original = NsoFile.parse(GOLDEN_A)
    replacement = bytes(reversed(A_SEGMENTS["rodata"]))
    patched = original.with_patches([(0x1000, replacement)])
    assert patched.segment_data("rodata") == replacement
    assert patched.segment_data("text") == A_SEGMENTS["text"]
    assert patched.segment_data("data") == A_SEGMENTS["data"]


@pytest.mark.parametrize(
    "patches",
    [
        pytest.param([(0x10, bytes(4)), (0x12, bytes(1))], id="one-inside-the-other"),
        pytest.param([(0x10, bytes(4)), (0x13, bytes(4))], id="tail-over-head"),
        pytest.param([(0x13, bytes(4)), (0x10, bytes(4))], id="given-out-of-order"),
        pytest.param([(0x20, b"\x01"), (0x20, b"\x01")], id="same-byte-twice"),
        pytest.param([(0x0, bytes(0x40)), (0x1000, bytes(4)), (0x3F, b"\x00")], id="three"),
    ],
)
def test_overlapping_patches_are_rejected(patches: list[tuple[int, bytes]]) -> None:
    module = NsoFile.parse(GOLDEN_A)
    with pytest.raises(ValueError, match="overlap"):
        module.with_patches(patches)
    assert module.image() == A_IMAGE


def test_patches_that_only_touch_are_applied() -> None:
    module = NsoFile.parse(GOLDEN_A)
    patched = module.with_patches([(0x14, b"\xbb" * 4), (0x10, b"\xaa" * 4)])
    assert patched.read_memory(0x10, 8) == b"\xaa" * 4 + b"\xbb" * 4
    assert patched.image()[:0x10] == A_IMAGE[:0x10]
    assert patched.image()[0x18:] == A_IMAGE[0x18:]


@pytest.mark.parametrize(
    ("offset", "length"),
    [
        (0xFFE, 4),  # from text into rodata: contiguous in memory, still two segments
        (0x1127, 2),  # off the end of rodata
        (0x1128, 1),  # in the gap
        (0x1FFF, 2),  # from the gap into data
        (0x211F, 2),  # off the end of the image
        (0x2120, 1),  # where bss begins
        (0x0, 0x1001),
        (-1, 1),
        (-4, 8),
    ],
)
def test_patch_outside_one_segment_is_rejected(offset: int, length: int) -> None:
    module = NsoFile.parse(GOLDEN_B)
    with pytest.raises(ValueError, match="inside one segment"):
        module.with_patches([(0x10, b"\x00"), (offset, bytes(length))])
    assert module.image() == B_IMAGE


def test_invalid_patches_are_rejected() -> None:
    module = NsoFile.parse(GOLDEN_A)
    with pytest.raises(ValueError, match="empty"):
        module.with_patches([(0x10, b"")])
    for bad in [(0x10, "AA"), (0x10, 4), (0x10, None), ("0x10", b"\x00"), (1.5, b"\x00")]:
        with pytest.raises(TypeError):
            module.with_patches([bad])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        module.with_patches([(True, b"\x00")])
    assert module.image() == A_IMAGE


def test_rewrite_changes_the_storage_fields_and_nothing_else() -> None:
    text_block = lz4.block.compress(B_SEGMENTS["text"], store_size=False)
    blob = bytearray(
        assemble(
            Part(B_SEGMENTS["text"], 0x0, stored=text_block),
            Part(RODATA, 0x3000),
            Part(DATA, 0x5000),
            flags=0x38 | 0x01 | 0x40 | 0x10000,
            version=7,
            bss_size=0x1234,
            module_id=bytes.fromhex("ABCD10"),
        )
    )
    # Values in every field that no loader interprets: a rewrite has to carry them over.
    struct.pack_into("<I", blob, 0x08, 0xAABBCCDD)  # reserved
    struct.pack_into("<I", blob, 0x1C, 0x11223344)  # module-name offset
    struct.pack_into("<I", blob, 0x2C, 0x55667788)  # module-name size
    blob[0x6C:0x88] = bytes(range(0x6C, 0x88))  # reserved
    blob[0x88:0xA0] = bytes(range(0x88, 0xA0))  # the rodata-relative extents

    module = NsoFile.parse(bytes(blob))
    written = module.to_bytes()

    expected = bytearray(blob[:0x100])
    struct.pack_into("<I", expected, 0x0C, 0x38 | 0x40 | 0x10000)  # bit 0 cleared, the rest kept
    struct.pack_into("<I", expected, 0x10, 0x100)
    struct.pack_into("<I", expected, 0x20, 0x3100)
    struct.pack_into("<I", expected, 0x30, 0x5100)
    struct.pack_into("<3I", expected, 0x60, 0x1000, len(RODATA), len(DATA))
    struct.pack_into("<I", expected, 0x1C, 0x100)
    struct.pack_into("<I", expected, 0x2C, 0)
    assert written[:0x100] == bytes(expected)
    assert written[0x100:] == image_of(
        [(0x0, B_SEGMENTS["text"]), (0x3000, RODATA), (0x5000, DATA)]
    )
    assert NsoFile.parse(written).to_bytes() == written


def test_rewrite_of_an_unusual_memory_layout() -> None:
    # Memory order differs from header order, text is not at 0 and data is empty.
    blob = assemble(Part(TEXT, 0x3000), Part(RODATA, 0x1000), Part(b"", 0x5000))
    module = NsoFile.parse(blob)
    expected = bytearray(0x5000)  # the empty data segment still extends the image
    expected[0x3000 : 0x3000 + len(TEXT)] = TEXT
    expected[0x1000 : 0x1000 + len(RODATA)] = RODATA
    assert module.image() == bytes(expected)

    written = module.to_bytes()

    assert len(written) == 0x5100
    assert written[0x100:] == bytes(expected)
    assert (u32(written, 0x10), u32(written, 0x20), u32(written, 0x30)) == (0x3100, 0x1100, 0x5100)
    again = NsoFile.parse(written)
    assert again.image() == bytes(expected)
    assert again.hash_ok == ALL_HASHES_OK
    assert again.to_bytes() == written


def test_rewrite_records_the_hashes_of_the_current_bytes() -> None:
    # A module whose rodata does not match its hash, read without verification ...
    damaged = bytearray(GOLDEN_A)
    damaged[0x150] ^= 0x01
    module = NsoFile.parse(bytes(damaged), verify_hashes=False)
    assert module.hash_ok == {"text": True, "rodata": False, "data": True}

    # ... is written back with the hash of what it holds now, patched or not.
    written = module.to_bytes()
    assert written[0xC0:0xE0] == hashlib.sha256(bytes(damaged[0x140:0x168])).digest()
    assert NsoFile.parse(written).hash_ok == ALL_HASHES_OK
    assert module.with_patches([(0x0, b"\xff")]).hash_ok == ALL_HASHES_OK


def test_constructor_wraps_decompressed_segments() -> None:
    header = NsoHeader.parse(GOLDEN_B)

    module = NsoFile(
        header,
        [B_SEGMENTS["text"], bytearray(B_SEGMENTS["rodata"]), memoryview(B_SEGMENTS["data"])],
    )

    assert module.header is header
    assert module.image() == B_IMAGE
    assert module.hash_ok == ALL_HASHES_OK
    assert GOLDEN_BUILD_ID in repr(module)
    # hash_ok is computed, not assumed: other bytes of the right sizes do not match.
    zeros = NsoFile(header, [bytes(0x1000), bytes(0x128), B_SEGMENTS["data"]])
    assert zeros.hash_ok == {"text": False, "rodata": False, "data": True}

    with pytest.raises(ValueError, match="3 segments"):
        NsoFile(header, [B_SEGMENTS["text"], B_SEGMENTS["rodata"]])
    with pytest.raises(ValueError, match="rodata"):
        NsoFile(header, [B_SEGMENTS["text"], B_SEGMENTS["rodata"][:-1], B_SEGMENTS["data"]])
    with pytest.raises(TypeError, match="text"):
        NsoFile(header, [0x1000, B_SEGMENTS["rodata"], B_SEGMENTS["data"]])  # type: ignore[list-item]
    overlapping = NsoHeader.parse(poke(GOLDEN_B, 0x24, 0x800))
    with pytest.raises(FormatError, match="overlap"):
        NsoFile(overlapping, list(B_SEGMENTS.values()))


# ----------------------------------------------------------------------------------
# build_nso
# ----------------------------------------------------------------------------------


def test_build_nso_reproduces_golden_a_byte_for_byte() -> None:
    built = build_nso(
        A_SEGMENTS["text"],
        A_SEGMENTS["rodata"],
        A_SEGMENTS["data"],
        module_id=GOLDEN_MODULE_ID,
        bss_size=0x30,
        compress=False,
    )
    assert built == GOLDEN_A


def test_build_nso_compressed_matches_golden_b_except_for_the_blocks() -> None:
    built = build_nso(
        B_SEGMENTS["text"],
        B_SEGMENTS["rodata"],
        B_SEGMENTS["data"],
        module_id=GOLDEN_MODULE_ID,
        bss_size=0x30,
    )

    # Which block a compressor emits for given bytes is its own business, so the stored
    # sizes and the file offsets that follow from them may differ. Nothing else may.
    storage = set(range(0x20, 0x24)) | set(range(0x30, 0x34)) | set(range(0x60, 0x6C))
    assert set(differing_offsets(built[:0x100], GOLDEN_B[:0x100])) <= storage
    assert u32(built, 0x0C) == 0x3F
    stored_sizes = struct.unpack_from("<3I", built, 0x60)
    assert (u32(built, 0x10), u32(built, 0x20), u32(built, 0x30)) == (
        0x100,
        0x100 + stored_sizes[0],
        0x100 + stored_sizes[0] + stored_sizes[1],
    )
    assert len(built) == 0x100 + sum(stored_sizes)
    position = 0x100
    for name, stored_size in zip(SEGMENT_NAMES, stored_sizes, strict=True):
        block = built[position : position + stored_size]
        expected = B_SEGMENTS[name]
        assert lz4.block.decompress(block, uncompressed_size=len(expected)) == expected
        position += stored_size
    assert NsoFile.parse(built).image() == B_IMAGE


def test_build_nso_layout_read_back_field_by_field() -> None:
    rng = random.Random(11)
    text, rodata, data = rng.randbytes(0x1001), rng.randbytes(0x1000), rng.randbytes(0x123)
    name = b"synthetic\x00"

    blob = build_nso(text, rodata, data, compress=False, module_name=name)

    assert blob[0x00:0x04] == b"NSO0"
    assert (u32(blob, 0x04), u32(blob, 0x08)) == (0, 0)  # version, reserved
    assert u32(blob, 0x0C) == 0x38
    # The name sits at 0x100 and pushes text back; in memory every segment starts on the
    # first page boundary at or after the end of the previous one.
    assert (u32(blob, 0x1C), u32(blob, 0x2C)) == (0x100, len(name))
    assert blob[0x100:0x10A] == name
    assert struct.unpack_from("<3I", blob, 0x10) == (0x10A, 0x0, 0x1001)
    assert struct.unpack_from("<3I", blob, 0x20) == (0x10A + 0x1001, 0x2000, 0x1000)
    assert struct.unpack_from("<3I", blob, 0x30) == (0x10A + 0x2001, 0x3000, 0x123)
    assert u32(blob, 0x3C) == 0
    assert blob[0x40:0x60] == bytes(32)
    assert struct.unpack_from("<3I", blob, 0x60) == (0x1001, 0x1000, 0x123)
    assert blob[0x6C:0xA0] == bytes(0x34)  # reserved bytes and the informational extents
    assert blob[0xA0:0xC0] == hashlib.sha256(text).digest()
    assert blob[0xC0:0xE0] == hashlib.sha256(rodata).digest()
    assert blob[0xE0:0x100] == hashlib.sha256(data).digest()
    assert blob[0x10A:] == text + rodata + data
    assert NsoHeader.parse(blob).warnings() == ()


def test_build_nso_memory_offsets_with_empty_and_page_sized_segments() -> None:
    blob = build_nso(b"", b"r" * 0x1000, b"d", compress=False)
    # Empty text takes no page; a segment of exactly one page is followed at once.
    assert [u32(blob, offset) for offset in (0x14, 0x24, 0x34)] == [0x0, 0x0, 0x1000]
    module = NsoFile.parse(blob)
    assert module.image() == b"r" * 0x1000 + b"d"


@pytest.mark.parametrize(
    "wanted",
    [(True, True, True), (False, False, False), (True, False, True), (False, True, False)],
)
def test_build_nso_compresses_the_requested_segments(wanted: tuple[bool, bool, bool]) -> None:
    repetitive = bytes.fromhex("1F2003D5") * 0x100

    blob = build_nso(repetitive, repetitive, repetitive, compress=wanted)

    flags = u32(blob, 0x0C)
    assert [bool(flags & bit) for bit in (0x01, 0x02, 0x04)] == list(wanted)
    assert flags & 0x38 == 0x38
    position = 0x100
    for index, compressed in enumerate(wanted):
        stored_size = u32(blob, 0x60 + 4 * index)
        assert u32(blob, 0x10 + 0x10 * index) == position
        stored = blob[position : position + stored_size]
        if compressed:
            assert stored_size < len(repetitive)
            assert lz4.block.decompress(stored, uncompressed_size=len(repetitive)) == repetitive
        else:
            assert stored == repetitive
        position += stored_size
    assert position == len(blob)
    for name in SEGMENT_NAMES:
        assert NsoFile.parse(blob).segment_data(name) == repetitive


def test_build_nso_compress_accepts_one_bool_for_all_segments() -> None:
    repetitive = b"\x00\x01" * 0x200
    args = (repetitive, repetitive, repetitive)
    assert build_nso(*args) == build_nso(*args, compress=True)
    assert build_nso(*args, compress=True) == build_nso(*args, compress=(True, True, True))
    assert build_nso(*args, compress=False) == build_nso(*args, compress=(False, False, False))
    assert len(build_nso(*args, compress=True)) < len(build_nso(*args, compress=False))


def test_build_nso_keeps_incompressible_and_empty_segments_raw() -> None:
    noise = random.Random(7).randbytes(0x400)
    # The premise, checked with the library itself: these blocks would not be smaller.
    assert len(lz4.block.compress(noise, store_size=False)) >= len(noise)
    assert len(lz4.block.compress(b"NSO0", store_size=False)) >= 4

    blob = build_nso(noise, b"NSO0", b"", compress=True)

    # A loader refuses a stored size above the size, so the flags stay clear instead.
    assert u32(blob, 0x0C) == 0x38
    assert struct.unpack_from("<3I", blob, 0x60) == (0x400, 4, 0)
    assert blob[0x100:] == noise + b"NSO0"
    module = NsoFile.parse(blob)
    assert module.segment_data("text") == noise
    assert module.segment_data("data") == b""
    assert module.header.warnings() == ()


def test_build_nso_without_hash_checks() -> None:
    blob = build_nso(TEXT, RODATA, DATA, check_hashes=False, compress=False)

    assert u32(blob, 0x0C) == 0  # no check bit, and nothing was compressed
    # The hashes are written all the same.
    assert blob[0xA0:0xC0] == hashlib.sha256(TEXT).digest()
    assert NsoFile.parse(blob).hash_ok == ALL_HASHES_OK
    # With the bits clear, a damaged segment is readable and reported, not refused.
    damaged = bytearray(blob)
    damaged[0x100] ^= 0xFF
    assert NsoFile.parse(bytes(damaged)).hash_ok == {"text": False, "rodata": True, "data": True}


def test_build_nso_extra_flags_are_ored_into_the_flags_word() -> None:
    blob = build_nso(TEXT, RODATA, DATA, compress=False, extra_flags=0x40 | 0x100)
    assert u32(blob, 0x0C) == 0x38 | 0x40 | 0x100
    assert len(NsoHeader.parse(blob).warnings()) == 2
    assert NsoFile.parse(blob).segment_data("text") == TEXT

    # Bit 7 on a module with a compressed segment: what the reader calls unsupported.
    zbic = build_nso(bytes(0x200), RODATA, DATA, extra_flags=0x80)
    assert u32(zbic, 0x0C) & 0x81 == 0x81
    with pytest.raises(UnsupportedInputError):
        NsoFile.parse(zbic)


def test_build_nso_rejects_bad_arguments() -> None:
    assert build_nso(TEXT, RODATA, DATA, module_id=bytes(range(32)))[0x40:0x60] == bytes(range(32))
    with pytest.raises(ValueError, match="module ID"):
        build_nso(TEXT, RODATA, DATA, module_id=bytes(33))
    for compress in [(True, False), (True, False, True, False), ()]:
        with pytest.raises(ValueError, match="compress"):
            build_nso(TEXT, RODATA, DATA, compress=compress)  # type: ignore[arg-type]
    for bss_size in (-1, 1 << 32):
        with pytest.raises(ValueError, match="bss"):
            build_nso(TEXT, RODATA, DATA, bss_size=bss_size)
    for extra_flags in (-1, 1 << 32):
        with pytest.raises(ValueError, match="flags"):
            build_nso(TEXT, RODATA, DATA, extra_flags=extra_flags)


def test_build_parse_and_rewrite_round_trip_on_random_modules() -> None:
    rng = random.Random(2024)
    sizes = [0, 1, 7, 0x40, 0xFFF, 0x1000, 0x1001, 0x2345]
    for _ in range(40):
        # Noise and repetition in turn, so that some segments compress and some do not.
        parts = [
            rng.randbytes(size) if rng.random() < 0.5 else bytes([rng.randrange(256)]) * size
            for size in (rng.choice(sizes), rng.choice(sizes), rng.choice(sizes))
        ]
        module_id = rng.randbytes(rng.choice([0, 16, 20, 32]))
        bss_size = rng.randrange(1 << 32)

        blob = build_nso(
            *parts,
            module_id=module_id,
            bss_size=bss_size,
            compress=(rng.random() < 0.7, rng.random() < 0.7, rng.random() < 0.7),
            check_hashes=rng.random() < 0.8,
        )
        module = NsoFile.parse(blob)

        assert [module.segment_data(name) for name in SEGMENT_NAMES] == parts
        assert module.hash_ok == ALL_HASHES_OK
        assert module.header.module_id == module_id.ljust(32, b"\x00")
        assert module.header.bss_size == bss_size
        assert module.header.warnings() == ()
        # Where the segments must sit, worked out here from the sizes alone.
        rodata_at = -(-len(parts[0]) // 0x1000) * 0x1000
        data_at = rodata_at + -(-len(parts[1]) // 0x1000) * 0x1000
        expected = image_of([(0x0, parts[0]), (rodata_at, parts[1]), (data_at, parts[2])])
        assert module.image() == expected
        written = module.to_bytes()
        assert written[0x100:] == expected
        again = NsoFile.parse(written)
        assert [again.segment_data(name) for name in SEGMENT_NAMES] == parts
        assert again.to_bytes() == written


# ----------------------------------------------------------------------------------
# Robustness: whatever the bytes, the outcome is a module or a BEA error
# ----------------------------------------------------------------------------------


def test_mutated_headers_are_parsed_or_refused_never_anything_else() -> None:
    rng = random.Random(0xBEA)
    parsed = refused = 0
    with traced_peak() as peak:
        for golden in (GOLDEN_A, GOLDEN_B):
            for _ in range(1500):
                mutated = bytearray(golden)
                for _ in range(rng.randint(1, 3)):
                    mutated[rng.randrange(4, 0x100)] = rng.randrange(256)
                try:
                    module = NsoFile.parse(bytes(mutated), verify_hashes=False)
                except (FormatError, UnsupportedInputError):
                    refused += 1
                    continue
                parsed += 1
                # What parses is self-consistent: every segment has the size it declares.
                for segment in module.header.segments:
                    assert len(module.segment_data(segment.name)) == segment.size
                module.header.warnings()
    assert parsed > 100
    assert refused > 100
    # No mutation made the parser allocate what a header announced.
    assert peak[0] < 8 * SMALL


def test_random_headers_are_parsed_or_refused_never_anything_else() -> None:
    rng = random.Random(1234)
    for _ in range(500):
        blob = b"NSO0" + rng.randbytes(0xFC) + rng.randbytes(rng.randrange(0x200))
        header = NsoHeader.parse(blob)
        header.warnings()
        assert len(header.build_id_full) == 64
        # Any other exception than these two fails the test.
        with suppress(FormatError, UnsupportedInputError):
            NsoFile.parse(blob)
