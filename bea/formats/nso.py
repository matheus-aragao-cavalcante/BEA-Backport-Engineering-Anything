"""NSO0 executable modules: header, build ID, segments, memory image, rewriting.

An NSO is one executable module of an ExeFS: the files ``main``, ``rtld``, ``sdk`` and
``subsdkN``. It carries three segments (``text``, ``rodata`` and ``data``), each stored
raw or compressed, behind a fixed header that says where every segment sits in the file
and in memory. BEA reads an NSO for two things: its **build ID**, which names the patch
file a loader looks for, and its decompressed bytes, against which every declared patch
is verified before anything is written.

Format
------
Public documentation: the switchbrew wiki, pages "NSO" (the layout below) and "ExeFS"
(which files of an ExeFS are NSOs); the LZ4 block format description (the compressed
segments). The wiki page "MOD" describes what a text segment starts with; this module
does not look inside the segments. Every integer is little-endian.

Header, exactly ``0x100`` bytes::

    +0x00  4 bytes   magic "NSO0"
    +0x04  u32       version (0 in practice)
    +0x08  u32       reserved
    +0x0C  u32       flags
    +0x10  3 x u32   text:   file offset, memory offset, size
    +0x1C  u32       module-name offset
    +0x20  3 x u32   rodata: file offset, memory offset, size
    +0x2C  u32       module-name size
    +0x30  3 x u32   data:   file offset, memory offset, size
    +0x3C  u32       bss size
    +0x40  32 bytes  module ID: the build ID, left-aligned and zero-padded
    +0x60  3 x u32   stored size of text, rodata, data (bytes they occupy in the file)
    +0x6C  28 bytes  reserved
    +0x88  6 x u32   offset and size of three areas of rodata (informational)
    +0xA0  32 bytes  SHA-256 of the decompressed text
    +0xC0  32 bytes  SHA-256 of the decompressed rodata
    +0xE0  32 bytes  SHA-256 of the decompressed data

Flags::

    bits 0-2   text / rodata / data is stored compressed
    bits 3-5   the loader verifies the hash of text / rodata / data
    bit 6      newer firmware: text is mapped execute-only
    bit 7      newer firmware: compressed segments use "ZBIC", a zstd-based codec
    bits 8-31  undefined

The body follows the header: optional module-name bytes, and the stored segments.

* ``size`` is the size of a segment in memory, that is, once decompressed. File offsets
  are absolute and arbitrary: nothing guarantees alignment, ordering, or that text starts
  right after the header. Only the header fields are followed; the length of the file is
  not stored anywhere.
* A compressed segment is exactly one raw LZ4 block (no frame, no size prefix) of
  "stored size" bytes that decodes to exactly ``size`` bytes. For an uncompressed segment
  loaders read ``size`` bytes and ignore the stored size, and so does this module.
* With flag bit 7 the compressed segments are not LZ4. BEA does not decode ZBIC and says
  so (:class:`~bea.errors.UnsupportedInputError`) instead of feeding such bytes to LZ4.
* Each hash covers the decompressed bytes of one segment. Loaders verify a segment only
  when its check bit is set, and so does :meth:`NsoFile.parse`; :attr:`NsoFile.hash_ok`
  reports all three segments regardless.
* The words at ``+0x1C`` / ``+0x2C`` (module name) and ``+0x88`` to ``+0x9F`` take
  different values depending on the tool that produced the file, and no loader checks
  them: they are preserved, never validated.

Memory image
------------
The image is a zero-filled buffer in which every segment sits at its memory offset; its
length is the largest segment end. The bss area that follows it in memory is only a
number (real modules declare gigabytes of it) and is never allocated.

A module that loads obeys a few rules: the three memory offsets are multiples of
``0x1000``, text is at offset 0, text ends before rodata starts and rodata before data,
and bit 6 is clear. A file that breaks them is still readable, so each violation is a
warning (:meth:`NsoHeader.warnings`), with one exception: segments that *overlap* in
memory make the image ambiguous, which is an error.

Writing: the image layout
-------------------------
:meth:`NsoFile.to_bytes` writes the module uncompressed with every segment at file
offset ``memory offset + 0x100``. The header is the original one with only the fields
that describe storage rewritten. In that layout ``file[0x100 + m] == image[m]``, which is
the address space of IPS patches for executables: a record offset is a memory offset plus
``0x100``. Every loader and tool reads such a file the same way.

Robustness
----------
Nothing read from the file is taken at its word. Stored ranges must lie inside the data,
the image may not exceed :data:`MAX_IMAGE_SIZE`, and a compressed segment may not claim
more bytes than an LZ4 block of its stored size can produce, all of which is checked
before anything is decompressed or allocated: a truncated or hostile file ends in a
:class:`~bea.errors.FormatError`, never in a crash or an out-of-memory condition.

Whether an LZ4 block is well formed is decided by the LZ4 library. A block that the
format description calls corrupt but the library decodes anyway (a match offset of zero)
is therefore only caught by the segment hash.
"""

from __future__ import annotations

import enum
import hashlib
import os
import struct
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import lz4.block

from bea.errors import FormatError, UnsupportedInputError
from bea.fsutil import fs_path
from bea.hexutil import build_id_from_bytes

__all__ = [
    "HEADER_SIZE",
    "MAX_IMAGE_SIZE",
    "MODULE_ID_SIZE",
    "NSO_MAGIC",
    "SEGMENT_NAMES",
    "NsoFile",
    "NsoFlags",
    "NsoHeader",
    "NsoSegment",
    "build_nso",
    "is_nso",
    "read_header",
]

#: The four bytes every NSO starts with.
NSO_MAGIC = b"NSO0"
#: Size of the header. It is also the distance between a memory offset and the position
#: of that byte in an image-layout file (and in an IPS record).
HEADER_SIZE = 0x100
#: Size of the module ID field (the build ID, zero-padded).
MODULE_ID_SIZE = 0x20
#: Largest memory image accepted, in bytes (1 GiB). Sizes up to 2 GiB are legal on paper,
#: but real modules are tens of megabytes: a header that announces more than this is
#: corrupt or hostile and is refused before anything is allocated for it.
MAX_IMAGE_SIZE = 1 << 30
#: The segment names, in header order.
SEGMENT_NAMES = ("text", "rodata", "data")

_U32 = struct.Struct("<I")
_U32_MAX = 0xFFFFFFFF
_SEGMENT_ROW = struct.Struct("<3I")  # file offset, memory offset, size

# Header field offsets (see the layout in the module docstring). The reader and both
# writers share them, so they cannot disagree about where a field lives.
_VERSION_OFFSET = 0x04
_FLAGS_OFFSET = 0x0C
_SEGMENT_ROWS_OFFSET = 0x10
_SEGMENT_ROW_STRIDE = 0x10  # each row is followed by one unrelated word
_SIZE_IN_ROW = 0x08  # where the size sits inside a segment row
_MODULE_NAME_OFFSET_FIELD = 0x1C
_MODULE_NAME_SIZE_FIELD = 0x2C
_BSS_SIZE_OFFSET = 0x3C
_MODULE_ID_OFFSET = 0x40
_STORED_SIZES_OFFSET = 0x60
_HASHES_OFFSET = 0xA0
_HASH_SIZE = 0x20

# Flag masks are plain integers on purpose: inverting an IntFlag member flips the
# *defined* bits only, which would silently drop the undefined ones a rewrite must keep.
_COMPRESSED_BITS = (0x01, 0x02, 0x04)
_HASH_BITS = (0x08, 0x10, 0x20)
_DEFINED_FLAGS = 0xFF
#: What an uncompressed rewrite clears: the three compression bits and the ZBIC bit.
_STORAGE_FLAGS = 0x87

#: Loaders map segments page by page.
_PAGE_SIZE = 0x1000
#: Every byte of an LZ4 block adds at most 255 bytes of output (a match-length
#: continuation byte), so a block never decodes to more than 255 times its own size.
_LZ4_MAX_EXPANSION = 255


class NsoFlags(enum.IntFlag):
    """The bits of the flags word at header offset ``0x0C``. Bits 8-31 are undefined."""

    #: The text segment is stored compressed.
    TEXT_COMPRESSED = 0x01
    #: The rodata segment is stored compressed.
    RODATA_COMPRESSED = 0x02
    #: The data segment is stored compressed.
    DATA_COMPRESSED = 0x04
    #: The loader verifies the SHA-256 of the text segment.
    TEXT_HASH = 0x08
    #: The loader verifies the SHA-256 of the rodata segment.
    RODATA_HASH = 0x10
    #: The loader verifies the SHA-256 of the data segment.
    DATA_HASH = 0x20
    #: Newer firmware: the text segment is mapped execute-only.
    EXECUTE_ONLY = 0x40
    #: Newer firmware: compressed segments use a zstd-based codec ("ZBIC"), not LZ4.
    ZBIC = 0x80


@dataclass(frozen=True, slots=True)
class NsoSegment:
    """One segment of an NSO, as the header describes it."""

    #: ``"text"``, ``"rodata"`` or ``"data"``.
    name: str
    #: Absolute position of the stored bytes in the file.
    file_offset: int
    #: Position of the segment inside the memory image of the module.
    memory_offset: int
    #: Size in memory, that is, once decompressed.
    size: int
    #: The stored-size field: the size of the LZ4 block when the segment is compressed.
    #: For an uncompressed segment loaders ignore it and read ``size`` bytes.
    stored_size: int
    compressed: bool
    #: Whether the loader is told to verify :attr:`hash`.
    check_hash: bool
    #: SHA-256 of the decompressed bytes as recorded in the header (32 bytes).
    hash: bytes

    @property
    def memory_end(self) -> int:
        """Memory offset of the first byte after the segment."""
        return self.memory_offset + self.size


@dataclass(frozen=True, slots=True)
class NsoHeader:
    """The ``0x100``-byte header of an NSO.

    Parsing a header never judges whether the module would load, nor whether its segments
    fit in the file: that is what lets the build ID of any NSO be read from its first
    ``0x100`` bytes. Oddities are reported by :meth:`warnings`; whatever makes the content
    unreadable is reported by :meth:`NsoFile.parse`.
    """

    #: The original ``0x100`` header bytes, verbatim (reserved and informational words too).
    raw: bytes = field(repr=False)
    #: 0 in every known file.
    version: int
    #: The flags word as stored, undefined bits included (see :class:`NsoFlags`).
    flags: int
    #: The ``text``, ``rodata`` and ``data`` segments, in that order.
    segments: tuple[NsoSegment, NsoSegment, NsoSegment]
    #: The word at ``0x1C``. Its meaning depends on the producing tool; never validated.
    module_name_offset: int
    #: The word at ``0x2C``. Its meaning depends on the producing tool; never validated.
    module_name_size: int
    #: Size of the zero-initialized area that follows the image in memory. Only a number.
    bss_size: int
    #: The 32-byte module ID field: the build ID, left-aligned and zero-padded.
    module_id: bytes

    @classmethod
    def parse(cls, data: bytes | bytearray | memoryview) -> NsoHeader:
        """Parse the header held by the first ``0x100`` bytes of ``data``.

        Anything after those bytes is ignored, so a prefix of the file is enough.

        Raises:
            FormatError: ``data`` does not start with the ``NSO0`` magic, or holds fewer
                than ``0x100`` bytes.
            TypeError: ``data`` is not bytes.
        """
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError(f"expected the bytes of an NSO header, got {type(data).__name__}")
        # Cut twice: a memoryview is sliced by items, and its items need not be bytes.
        raw = bytes(data[:HEADER_SIZE])[:HEADER_SIZE]
        magic = raw[: len(NSO_MAGIC)]
        # Compared over the bytes that are there: data too short to hold the magic is
        # "truncated" only as long as what it does hold agrees with the magic.
        if magic != NSO_MAGIC[: len(magic)]:
            raise FormatError(
                f"NSO: wrong magic at offset 0x0: expected {NSO_MAGIC!r}, found {magic!r}"
            )
        if len(raw) < HEADER_SIZE:
            raise FormatError(
                f"NSO: truncated header: the header is {_hex(HEADER_SIZE)} bytes, "
                f"only {_hex(len(raw))} available"
            )
        flags = _read_u32(raw, _FLAGS_OFFSET)
        return cls(
            raw=raw,
            version=_read_u32(raw, _VERSION_OFFSET),
            flags=flags,
            segments=(
                _parse_segment(raw, flags, 0),
                _parse_segment(raw, flags, 1),
                _parse_segment(raw, flags, 2),
            ),
            module_name_offset=_read_u32(raw, _MODULE_NAME_OFFSET_FIELD),
            module_name_size=_read_u32(raw, _MODULE_NAME_SIZE_FIELD),
            bss_size=_read_u32(raw, _BSS_SIZE_OFFSET),
            module_id=raw[_MODULE_ID_OFFSET : _MODULE_ID_OFFSET + MODULE_ID_SIZE],
        )

    @property
    def build_id(self) -> str:
        """The build ID in canonical text form: uppercase hex, trailing zero bytes removed."""
        return build_id_from_bytes(self.module_id)

    @property
    def build_id_full(self) -> str:
        """All 32 bytes of the module ID, as 64 uppercase hex digits."""
        return self.module_id.hex().upper()

    @property
    def image_size(self) -> int:
        """Size of the memory image without bss: the largest segment end."""
        return max(segment.memory_end for segment in self.segments)

    @property
    def uses_zbic(self) -> bool:
        """Whether a segment is compressed with ZBIC, the codec BEA cannot decode.

        True when flag bit 7 is set *and* at least one segment is compressed. With the bit
        set but nothing compressed there is nothing to decode, and the file is readable.
        """
        return bool(self.flags & NsoFlags.ZBIC) and any(
            segment.compressed for segment in self.segments
        )

    def segment(self, name: str) -> NsoSegment:
        """Return the segment called ``name`` (``"text"``, ``"rodata"`` or ``"data"``).

        Raises:
            ValueError: there is no segment of that name.
        """
        return self.segments[_segment_index(name)]

    def warnings(self) -> tuple[str, ...]:
        """Return what is odd but not fatal about this header, one full sentence each.

        Reported: a version other than 0; undefined flag bits; the execute-only flag;
        every rule on the memory layout that loaders enforce (memory offsets that are
        multiples of ``0x1000``, text at offset 0, text before rodata before data); and
        an uncompressed segment whose stored size differs from its size. None of them
        keeps BEA from reading the file. ZBIC compression is not a warning: see
        :attr:`uses_zbic`.
        """
        found: list[str] = []
        if self.version != 0:
            found.append(f"The header version is {self.version}; every known NSO has version 0.")
        undefined = self.flags & ~_DEFINED_FLAGS
        if undefined:
            found.append(
                f"The flags word 0x{self.flags:08X} has undefined bits set (0x{undefined:08X})."
            )
        if self.flags & NsoFlags.EXECUTE_ONLY:
            found.append(
                "Flag bit 6 (execute-only text) is set; loaders that predate this flag may "
                "refuse the module."
            )
        text, rodata, data = self.segments
        for segment in self.segments:
            if segment.memory_offset % _PAGE_SIZE:
                found.append(
                    f"The {segment.name} memory offset {_hex(segment.memory_offset)} is not a "
                    f"multiple of {_hex(_PAGE_SIZE)}; loaders require page alignment."
                )
        if text.memory_offset != 0:
            found.append(
                f"The text memory offset is {_hex(text.memory_offset)}; loaders require text "
                "at offset 0x0."
            )
        for lower, upper in ((text, rodata), (rodata, data)):
            if lower.memory_end > upper.memory_offset:
                found.append(
                    f"The {lower.name} segment ends at {_hex(lower.memory_end)}, after the "
                    f"start of {upper.name} at {_hex(upper.memory_offset)}; loaders require "
                    "text, rodata and data in that order."
                )
        for segment in self.segments:
            if not segment.compressed and segment.stored_size != segment.size:
                found.append(
                    f"The {segment.name} segment is not compressed, yet its stored size "
                    f"{_hex(segment.stored_size)} differs from its size {_hex(segment.size)}; "
                    "the size is what gets read, as loaders do."
                )
        return tuple(found)


def is_nso(prefix: bytes) -> bool:
    """Tell whether a file that starts with ``prefix`` announces itself as an NSO.

    Only the magic is looked at: :meth:`NsoHeader.parse` and :meth:`NsoFile.parse` decide
    whether the file is valid.
    """
    return bytes(prefix[: len(NSO_MAGIC)]) == NSO_MAGIC


def read_header(path: str | os.PathLike[str]) -> NsoHeader:
    """Read the header of the NSO stored at ``path``.

    Only the first ``0x100`` bytes of the file are read. This is the cheap way to get a
    build ID: the segments are neither read nor decompressed, whatever their size.

    Raises:
        FormatError: the file is not an NSO, or is shorter than its header.
        OSError: the file cannot be opened or read.
    """
    prefix = bytearray()
    # Unbuffered on purpose: a buffered reader would fetch a whole buffer of segment data
    # behind the header. One read of an unbuffered file may return less than was asked
    # for, so the request is repeated until it is satisfied or the file is exhausted.
    with open(fs_path(path), "rb", buffering=0) as handle:
        while len(prefix) < HEADER_SIZE:
            chunk = handle.read(HEADER_SIZE - len(prefix))
            if not chunk:
                break
            prefix += chunk
    return NsoHeader.parse(prefix)


class NsoFile:
    """An NSO held in memory: its header and the decompressed bytes of each segment.

    Instances are immutable (:meth:`with_patches` returns a new object) and normally come
    from :meth:`parse` or :meth:`read`. The constructor wraps segment bytes that are
    already decompressed, given in header order: text, rodata, data.

    Attributes:
        header: The header that describes the segments.
        hash_ok: For each segment name, whether the SHA-256 of its bytes equals the hash
            recorded in :attr:`header`. All three segments are reported, whether or not
            their check bit is set.

    Raises:
        FormatError: the header describes segments that overlap in memory, or an image
            larger than :data:`MAX_IMAGE_SIZE`.
        TypeError: a segment is not bytes.
        ValueError: ``segments`` is not one byte string per segment, each of exactly the
            size the header declares.
    """

    __slots__ = ("_contents", "_digests", "hash_ok", "header")

    header: NsoHeader
    hash_ok: Mapping[str, bool]
    #: The decompressed bytes of text, rodata and data.
    _contents: tuple[bytes, ...]
    #: SHA-256 of each entry of ``_contents``, kept so that nothing is hashed twice.
    _digests: tuple[bytes, ...]

    def __init__(
        self, header: NsoHeader, segments: Sequence[bytes | bytearray | memoryview]
    ) -> None:
        _check_layout(header)
        if len(segments) != len(SEGMENT_NAMES):
            raise ValueError(
                f"expected {len(SEGMENT_NAMES)} segments ({', '.join(SEGMENT_NAMES)}), "
                f"got {len(segments)}"
            )
        contents: list[bytes] = []
        for segment, content in zip(header.segments, segments, strict=True):
            # Anything else would be turned into bytes of its own making by bytes(): an
            # int becomes that many zeros.
            if not isinstance(content, bytes | bytearray | memoryview):
                raise TypeError(
                    f"the {segment.name} segment must be bytes, got {type(content).__name__}"
                )
            copy = content if isinstance(content, bytes) else bytes(content)
            if len(copy) != segment.size:
                raise ValueError(
                    f"the {segment.name} segment holds {_hex(len(copy))} bytes, "
                    f"the header declares {_hex(segment.size)}"
                )
            contents.append(copy)
        self._adopt(
            header,
            tuple(contents),
            tuple(hashlib.sha256(content).digest() for content in contents),
        )

    @classmethod
    def parse(cls, data: bytes | bytearray | memoryview, *, verify_hashes: bool = True) -> NsoFile:
        """Parse a whole NSO: read the header, then extract and decompress the segments.

        With ``verify_hashes`` (the default) a segment whose check bit is set must match
        its hash, which is what a loader demands. Without it nothing is refused on
        account of a hash. :attr:`hash_ok` tells the outcome either way.

        Raises:
            FormatError: wrong magic, or data shorter than the header; a segment stored
                outside the data; segments that overlap in memory; a memory image larger
                than :data:`MAX_IMAGE_SIZE`; an LZ4 block that is corrupt or does not
                decode to exactly the declared size; with ``verify_hashes``, a segment
                whose check bit is set and whose hash does not match.
            UnsupportedInputError: the segments are compressed with ZBIC (zstd-based),
                which this version of BEA cannot decode.
            TypeError: ``data`` is not bytes.
        """
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError(f"expected the bytes of an NSO, got {type(data).__name__}")
        # A private, immutable copy unless the data already is one: no view of a caller's
        # buffer is kept, so nothing done here can lock it against resizing.
        raw = data if isinstance(data, bytes) else bytes(data)
        header = NsoHeader.parse(raw)
        if header.uses_zbic:
            raise UnsupportedInputError(
                "NSO: flag bit 7 is set: the segments use ZBIC (zstd-based) compression, "
                "which is not supported by this version of BEA"
            )
        # Everything that the header alone decides comes first, so that a hostile file is
        # refused before a single byte is decompressed or allocated.
        _check_layout(header)
        _check_stored_ranges(header, len(raw))
        contents: list[bytes] = []
        for segment in header.segments:
            stored = raw[segment.file_offset : segment.file_offset + _stored_length(segment)]
            contents.append(_decompress(segment, stored) if segment.compressed else stored)
        module = cls(header, contents)
        if verify_hashes:
            mismatches = [
                f"the {segment.name} hash at header offset "
                f"{_hex(_HASHES_OFFSET + _HASH_SIZE * index)} is {segment.hash.hex()}, "
                f"the decompressed segment hashes to {module._digests[index].hex()}"
                for index, segment in enumerate(header.segments)
                if segment.check_hash and not module.hash_ok[segment.name]
            ]
            if mismatches:
                raise FormatError("NSO: SHA-256 mismatch: " + "; ".join(mismatches))
        return module

    @classmethod
    def read(cls, path: str | os.PathLike[str], *, verify_hashes: bool = True) -> NsoFile:
        """Read and parse the NSO stored at ``path`` (see :meth:`parse`).

        Raises:
            FormatError: the file is not a readable NSO (see :meth:`parse`).
            UnsupportedInputError: the segments are compressed with ZBIC.
            OSError: the file cannot be opened or read.
        """
        with open(fs_path(path), "rb") as handle:
            data = handle.read()
        return cls.parse(data, verify_hashes=verify_hashes)

    def segment_data(self, name: str) -> bytes:
        """Return the decompressed bytes of the segment called ``name``.

        Raises:
            ValueError: there is no segment of that name.
        """
        return self._contents[_segment_index(name)]

    def image(self) -> bytes:
        """Return the memory image, without bss.

        The result is :attr:`NsoHeader.image_size` bytes long: zeros, with every segment
        at its memory offset. It is built anew on every call; keep the result when it is
        needed more than once.
        """
        return b"".join(self._image_chunks())

    def find_segment(self, offset: int, length: int) -> NsoSegment | None:
        """Return the segment that fully contains the memory range ``[offset, offset + length)``.

        ``None`` means that the range reaches into a gap between segments, into the bss,
        outside the image, or across two segments.

        Raises:
            ValueError: ``length`` is smaller than 1.
        """
        index = self._locate(offset, length)
        return None if index is None else self.header.segments[index]

    def read_memory(self, offset: int, length: int) -> bytes:
        """Return ``length`` bytes of the memory image, starting at memory offset ``offset``.

        Raises:
            ValueError: ``length`` is smaller than 1, or the range does not lie inside
                one segment (see :meth:`find_segment`).
        """
        index = self._locate(offset, length)
        if index is None:
            raise ValueError(
                f"the memory range {_hex(offset)}..{_hex(offset + length)} does not lie "
                "inside one segment"
            )
        start = offset - self.header.segments[index].memory_offset
        return self._contents[index][start : start + length]

    def with_patches(self, patches: Iterable[tuple[int, bytes]]) -> NsoFile:
        """Return a new module in which bytes of the memory image have been replaced.

        Each patch is ``(memory offset, replacement bytes)``; the order does not matter.
        This object is left untouched. The result carries the header that
        :meth:`to_bytes` writes (uncompressed image layout, hashes of the patched
        bytes), so its :attr:`hash_ok` is about its own bytes and its own header.

        Raises:
            TypeError: an offset is not an integer, or a replacement is not bytes.
            ValueError: a replacement is empty, does not lie inside one segment, or
                overlaps another replacement.
        """
        ordered: list[tuple[int, bytes]] = []
        for offset, replacement in patches:
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise TypeError(f"a patch offset must be an integer, got {type(offset).__name__}")
            if not isinstance(replacement, bytes | bytearray | memoryview):
                raise TypeError(
                    f"the replacement at memory offset {_hex(offset)} must be bytes, "
                    f"got {type(replacement).__name__}"
                )
            ordered.append((offset, bytes(replacement)))
        ordered.sort(key=lambda patch: patch[0])

        # Copies of the segments that change; nothing of this object is ever modified, so
        # a patch set that turns out to be invalid half-way leaves no trace.
        buffers: dict[int, bytearray] = {}
        previous_offset = previous_end = 0
        for position, (offset, replacement) in enumerate(ordered):
            if not replacement:
                raise ValueError(
                    f"the replacement at memory offset {_hex(offset)} is empty "
                    "(a patch changes at least one byte)"
                )
            end = offset + len(replacement)
            if position and offset < previous_end:
                raise ValueError(
                    f"replacements overlap: the one at memory offset {_hex(previous_offset)} "
                    f"ends at {_hex(previous_end)}, after the next one starts ({_hex(offset)})"
                )
            index = self._locate(offset, len(replacement))
            if index is None:
                raise ValueError(
                    f"the replacement at memory range {_hex(offset)}..{_hex(end)} does not "
                    "lie inside one segment"
                )
            if index not in buffers:
                buffers[index] = bytearray(self._contents[index])
            start = offset - self.header.segments[index].memory_offset
            buffers[index][start : start + len(replacement)] = replacement
            previous_offset, previous_end = offset, end

        contents = tuple(
            bytes(buffers[index]) if index in buffers else content
            for index, content in enumerate(self._contents)
        )
        digests = tuple(
            hashlib.sha256(content).digest() if index in buffers else self._digests[index]
            for index, content in enumerate(contents)
        )
        # The memory layout is the one this object was validated with, and the digests are
        # those of the new contents: the constructor has nothing left to check or to hash.
        patched = NsoFile.__new__(NsoFile)
        patched._adopt(_image_layout_header(self.header, digests), contents, digests)
        return patched

    def to_bytes(self) -> bytes:
        """Return the module as an uncompressed NSO in image layout.

        The header is the original one with only the storage fields rewritten: the
        compression and ZBIC flags are cleared, every file offset becomes ``memory offset
        + 0x100``, every stored size becomes the size, the module name is declared empty
        and every hash is that of the current bytes of its segment. The segments follow
        at their file offsets, zeros in between, so the result is ``0x100 + image size``
        bytes long and ``result[0x100 + m]`` is the byte at memory offset ``m``. Parsing
        the result gives the same segments and the same build ID, with hashes that
        verify.
        """
        header = _image_layout_header(self.header, self._digests)
        return b"".join((header.raw, *self._image_chunks()))

    def __repr__(self) -> str:
        return (
            f"NsoFile(build_id={self.header.build_id!r}, image_size={_hex(self.header.image_size)})"
        )

    # ------------------------------------------------------------------ internals

    def _adopt(
        self, header: NsoHeader, contents: tuple[bytes, ...], digests: tuple[bytes, ...]
    ) -> None:
        """Take over validated state: ``digests`` are the SHA-256 of ``contents``."""
        self.header = header
        self._contents = contents
        self._digests = digests
        self.hash_ok = MappingProxyType(
            {
                segment.name: digest == segment.hash
                for segment, digest in zip(header.segments, digests, strict=True)
            }
        )

    def _locate(self, offset: int, length: int) -> int | None:
        """Return the index of the segment that contains ``[offset, offset + length)``.

        Raises:
            ValueError: ``length`` is smaller than 1.
        """
        if length < 1:
            raise ValueError(f"a memory range holds at least 1 byte, got a length of {length}")
        for index, segment in enumerate(self.header.segments):
            if segment.memory_offset <= offset and offset + length <= segment.memory_end:
                return index
        return None

    def _image_chunks(self) -> Iterator[bytes]:
        """Yield the memory image piece by piece: the segments in memory order, zeros between."""
        placed = sorted(
            (
                (segment.memory_offset, content)
                for segment, content in zip(self.header.segments, self._contents, strict=True)
                if content
            ),
            key=lambda item: item[0],
        )
        position = 0
        for memory_offset, content in placed:
            if memory_offset > position:
                yield bytes(memory_offset - position)
            yield content
            position = memory_offset + len(content)
        # An empty segment placed beyond the others still extends the image.
        if self.header.image_size > position:
            yield bytes(self.header.image_size - position)


def build_nso(
    text: bytes,
    rodata: bytes,
    data: bytes,
    *,
    module_id: bytes = b"",
    bss_size: int = 0,
    compress: bool | tuple[bool, bool, bool] = True,
    check_hashes: bool = True,
    module_name: bytes = b"",
    extra_flags: int = 0,
) -> bytes:
    """Build an NSO that holds the three given segments, in packed layout.

    A writer for tests and for the synthetic-game generator. The file is: the header;
    ``module_name`` at ``0x100`` (module-name offset ``0x100``, module-name size
    ``len(module_name)``); then text, rodata and data, stored back to back. In memory,
    text sits at offset 0, rodata at the size of text rounded up to ``0x1000``, and data
    at the end of rodata rounded up to ``0x1000``.

    ``compress`` asks for LZ4 compression of every segment (a ``bool``) or of each one
    (``(text, rodata, data)``). A segment is nevertheless stored raw, with its flag
    clear, when it is empty or when its LZ4 block would not be smaller than its bytes: a
    loader refuses a stored size larger than the size. ``check_hashes`` sets the three
    hash-check bits; the hashes themselves are always written. ``module_id`` is
    zero-padded to 32 bytes. ``extra_flags`` is OR-ed into the flags word as it is.

    Raises:
        ValueError: ``module_id`` is longer than 32 bytes; ``compress`` is neither a bool
            nor three of them; ``bss_size`` or ``extra_flags`` does not fit in 32 bits;
            or the memory image would exceed :data:`MAX_IMAGE_SIZE`.
    """
    if len(module_id) > MODULE_ID_SIZE:
        raise ValueError(f"a module ID has at most {MODULE_ID_SIZE} bytes, got {len(module_id)}")
    wanted = (compress,) * len(SEGMENT_NAMES) if isinstance(compress, bool) else tuple(compress)
    if len(wanted) != len(SEGMENT_NAMES):
        raise ValueError(
            f"compress must be a bool or one bool per segment ({', '.join(SEGMENT_NAMES)}), "
            f"got {len(wanted)} values"
        )
    if not 0 <= bss_size <= _U32_MAX:
        raise ValueError(f"the bss size must fit in 32 bits, got {bss_size}")
    if not 0 <= extra_flags <= _U32_MAX:
        raise ValueError(f"the flags must fit in 32 bits, got {extra_flags}")

    header = bytearray(HEADER_SIZE)
    header[: len(NSO_MAGIC)] = NSO_MAGIC
    body = [module_name]
    flags = extra_flags
    file_offset = HEADER_SIZE + len(module_name)
    memory_offset = 0
    for index, (name, content) in enumerate(zip(SEGMENT_NAMES, (text, rodata, data), strict=True)):
        if memory_offset + len(content) > MAX_IMAGE_SIZE:
            raise ValueError(
                f"the {name} segment would end at {_hex(memory_offset + len(content))}, past "
                f"the largest memory image of {_hex(MAX_IMAGE_SIZE)} bytes"
            )
        stored = content
        if wanted[index] and content:
            # A raw block: the default of the library prepends a size, which an NSO does
            # not have.
            block: bytes = lz4.block.compress(content, store_size=False)
            if len(block) < len(content):
                stored = block
                flags |= _COMPRESSED_BITS[index]
        if check_hashes:
            flags |= _HASH_BITS[index]
        _SEGMENT_ROW.pack_into(
            header,
            _SEGMENT_ROWS_OFFSET + _SEGMENT_ROW_STRIDE * index,
            file_offset,
            memory_offset,
            len(content),
        )
        _U32.pack_into(header, _STORED_SIZES_OFFSET + _U32.size * index, len(stored))
        hash_start = _HASHES_OFFSET + _HASH_SIZE * index
        header[hash_start : hash_start + _HASH_SIZE] = hashlib.sha256(content).digest()
        body.append(stored)
        file_offset += len(stored)
        # The next segment starts on the first page boundary at or after the end of this one.
        memory_offset = _page_align(memory_offset + len(content))
    _U32.pack_into(header, _FLAGS_OFFSET, flags)
    _U32.pack_into(header, _MODULE_NAME_OFFSET_FIELD, HEADER_SIZE)
    _U32.pack_into(header, _MODULE_NAME_SIZE_FIELD, len(module_name))
    _U32.pack_into(header, _BSS_SIZE_OFFSET, bss_size)
    header[_MODULE_ID_OFFSET : _MODULE_ID_OFFSET + len(module_id)] = module_id
    return b"".join((header, *body))


# ------------------------------------------------------------------------ internals


def _hex(value: int) -> str:
    """Format an offset or a size for a message (``0x1A2B``; a negative keeps its sign)."""
    return f"-0x{-value:X}" if value < 0 else f"0x{value:X}"


def _page_align(value: int) -> int:
    """Round ``value`` up to a multiple of the page size."""
    return (value + _PAGE_SIZE - 1) // _PAGE_SIZE * _PAGE_SIZE


def _read_u32(raw: bytes, offset: int) -> int:
    """Return the little-endian u32 stored at ``offset`` of ``raw``."""
    (value,) = _U32.unpack_from(raw, offset)
    return int(value)


def _segment_index(name: str) -> int:
    """Return the position of the segment called ``name`` in header order.

    Raises:
        ValueError: there is no segment of that name.
    """
    try:
        return SEGMENT_NAMES.index(name)
    except ValueError:
        raise ValueError(
            f"unknown segment {name!r}: expected one of {', '.join(SEGMENT_NAMES)}"
        ) from None


def _parse_segment(raw: bytes, flags: int, index: int) -> NsoSegment:
    """Decode the header fields of segment number ``index`` (0 is text)."""
    file_offset, memory_offset, size = _SEGMENT_ROW.unpack_from(
        raw, _SEGMENT_ROWS_OFFSET + _SEGMENT_ROW_STRIDE * index
    )
    hash_start = _HASHES_OFFSET + _HASH_SIZE * index
    return NsoSegment(
        name=SEGMENT_NAMES[index],
        file_offset=file_offset,
        memory_offset=memory_offset,
        size=size,
        stored_size=_read_u32(raw, _STORED_SIZES_OFFSET + _U32.size * index),
        compressed=bool(flags & _COMPRESSED_BITS[index]),
        check_hash=bool(flags & _HASH_BITS[index]),
        hash=raw[hash_start : hash_start + _HASH_SIZE],
    )


def _check_layout(header: NsoHeader) -> None:
    """Refuse a memory layout that cannot become one unambiguous image of a sane size.

    Raises:
        FormatError: the image would exceed :data:`MAX_IMAGE_SIZE`, or two segments
            overlap in memory.
    """
    if header.image_size > MAX_IMAGE_SIZE:
        index, last = max(enumerate(header.segments), key=lambda item: item[1].memory_end)
        row = _SEGMENT_ROWS_OFFSET + _SEGMENT_ROW_STRIDE * index
        raise FormatError(
            f"NSO: memory image too large: the {last.name} segment (memory offset "
            f"{_hex(last.memory_offset)} and size {_hex(last.size)}, at header offset "
            f"{_hex(row + _U32.size)}) ends at {_hex(last.memory_end)}, above the limit of "
            f"{_hex(MAX_IMAGE_SIZE)} bytes"
        )
    # An empty segment occupies no byte, so it cannot make any byte ambiguous.
    occupied = [segment for segment in header.segments if segment.size]
    for position, first in enumerate(occupied):
        for second in occupied[position + 1 :]:
            if first.memory_offset < second.memory_end and second.memory_offset < first.memory_end:
                raise FormatError(
                    f"NSO: segments overlap in memory: {first.name} occupies "
                    f"{_hex(first.memory_offset)}..{_hex(first.memory_end)}, {second.name} "
                    f"occupies {_hex(second.memory_offset)}..{_hex(second.memory_end)}"
                )


def _stored_length(segment: NsoSegment) -> int:
    """Return how many bytes of the file a segment occupies, counted the way loaders do."""
    return segment.stored_size if segment.compressed else segment.size


def _check_stored_ranges(header: NsoHeader, available: int) -> None:
    """Require the stored bytes of every segment to lie inside ``available`` bytes of data.

    Raises:
        FormatError: a segment starts or ends past the end of the data.
    """
    for index, segment in enumerate(header.segments):
        length = _stored_length(segment)
        if segment.file_offset + length <= available:
            continue
        row = _SEGMENT_ROWS_OFFSET + _SEGMENT_ROW_STRIDE * index
        size_field = (
            f"stored size at header offset {_hex(_STORED_SIZES_OFFSET + _U32.size * index)}"
            if segment.compressed
            else f"size at header offset {_hex(row + _SIZE_IN_ROW)}"
        )
        raise FormatError(
            f"NSO: the {segment.name} segment does not fit in the file: file offset "
            f"{_hex(segment.file_offset)} (header offset {_hex(row)}) plus {_hex(length)} "
            f"bytes ({size_field}) ends at {_hex(segment.file_offset + length)}, the file is "
            f"only {_hex(available)} bytes long"
        )


def _decompress(segment: NsoSegment, stored: bytes) -> bytes:
    """Decode the LZ4 block of a compressed segment.

    Raises:
        FormatError: the block is corrupt, or does not decode to exactly the size the
            header declares.
    """
    where = (
        f"NSO: the LZ4 block of the {segment.name} segment (file offset "
        f"{_hex(segment.file_offset)}, {_hex(len(stored))} bytes)"
    )
    # The library allocates the declared size before it decodes anything, so a claim that
    # no block of this size can honour is turned down here: it costs nothing to make and
    # would cost up to MAX_IMAGE_SIZE of memory to disprove.
    if segment.size > _LZ4_MAX_EXPANSION * len(stored):
        raise FormatError(
            f"{where} cannot decode to the declared size of {_hex(segment.size)} bytes: "
            f"LZ4 expands at most {_LZ4_MAX_EXPANSION} times"
        )
    try:
        # The size passed is only the capacity of the output buffer: a block that decodes
        # to fewer bytes comes back shorter, hence the comparison below.
        decoded: bytes = lz4.block.decompress(stored, uncompressed_size=segment.size)
    except lz4.block.LZ4BlockError as exc:
        raise FormatError(
            f"{where} is corrupt, or decodes to more than the declared size of "
            f"{_hex(segment.size)} bytes ({exc})"
        ) from exc
    if len(decoded) != segment.size:
        raise FormatError(
            f"{where} decodes to {_hex(len(decoded))} bytes, expected {_hex(segment.size)}"
        )
    return decoded


def _image_layout_header(header: NsoHeader, digests: Sequence[bytes]) -> NsoHeader:
    """Return ``header`` rewritten for an uncompressed file in image layout.

    Only the fields that describe storage change (see :meth:`NsoFile.to_bytes`);
    ``digests`` are the SHA-256 of the current bytes of text, rodata and data. Every
    other byte of the header, reserved and informational words included, is kept.
    """
    raw = bytearray(header.raw)
    _U32.pack_into(raw, _FLAGS_OFFSET, header.flags & ~_STORAGE_FLAGS)
    _U32.pack_into(raw, _MODULE_NAME_OFFSET_FIELD, HEADER_SIZE)
    _U32.pack_into(raw, _MODULE_NAME_SIZE_FIELD, 0)
    for index, (segment, digest) in enumerate(zip(header.segments, digests, strict=True)):
        _U32.pack_into(
            raw,
            _SEGMENT_ROWS_OFFSET + _SEGMENT_ROW_STRIDE * index,
            segment.memory_offset + HEADER_SIZE,
        )
        _U32.pack_into(raw, _STORED_SIZES_OFFSET + _U32.size * index, segment.size)
        hash_start = _HASHES_OFFSET + _HASH_SIZE * index
        raw[hash_start : hash_start + _HASH_SIZE] = digest
    return NsoHeader.parse(raw)
