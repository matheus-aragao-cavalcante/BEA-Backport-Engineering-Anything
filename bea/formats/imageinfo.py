"""Image header probe: the size and pixel format of an image file, without decoding a pixel.

BEA walks tens of thousands of game files to find the textures that exceed their limits.
Handing each one to an image library would parse tables, palettes and sometimes pixels;
:func:`probe` reads the handful of header bytes that state the dimensions and the pixel
layout, and nothing else.

Detection is by signature, never by file name: a PNG called ``x.tga`` is reported as a
PNG. The one exception is TGA, which has no signature: it is considered only when the
name ends in ``.tga`` (or the file carries the TGA 2.0 footer) *and* its header is
plausible. An implausible header under a ``.tga`` name is simply not a TGA (``None``),
whereas a recognized signature followed by a malformed or truncated header is an error
(:class:`bea.errors.FormatError`).

The probe checks what it reads - the fields it reports and the structures it has to walk -
against the documents below. It does not vouch for the rest of the file: checksums, pixel
data and later chunks are the decoder's business.

PNG
    W3C, *Portable Network Graphics (PNG) Specification*: "PNG signature", "Chunk layout"
    and "IHDR Image header". Integers are big-endian::

        offset  size  field
             0     8  signature: 89 50 4E 47 0D 0A 1A 0A
             8     4  length of the first chunk's data: 13
            12     4  type of the first chunk: "IHDR"
            16     4  width  (1 to 2^31 - 1)
            20     4  height (1 to 2^31 - 1)
            24     1  bit depth
            25     1  colour type: 0 greyscale, 2 truecolour, 3 palette,
                      4 greyscale + alpha, 6 truecolour + alpha
            26     1  compression method: 0
            27     1  filter method: 0
            28     1  interlace method: 0 none, 1 Adam7

    Allowed bit depths: 1, 2, 4, 8, 16 for greyscale; 1, 2, 4, 8 for palette; 8, 16 for
    the other colour types.

TGA
    Truevision, *TGA File Format Specification, Version 2.0*. Integers are little-endian::

        offset  size  field
             0     1  ID length
             1     1  colour-map type: 0 none, 1 present
             2     1  image type: 1 colour-mapped, 2 truecolour, 3 greyscale;
                      9, 10, 11 the same, run-length encoded
             3     5  colour map: first index (2), length (2), entry size (1)
             8     4  x origin (2), y origin (2)
            12     2  width
            14     2  height
            16     1  pixel depth: 8, 15, 16, 24 or 32 bits
            17     1  descriptor: bits 0-3 alpha bits, bit 4 right-to-left,
                      bit 5 top-to-bottom

    A TGA 2.0 file ends with a 26-byte footer: extension area offset (4), developer
    directory offset (4), then ``TRUEVISION-XFILE.`` and a NUL (18).

JPEG
    ITU-T Recommendation T.81 (ISO/IEC 10918-1), Annex B, and the JFIF convention. After
    the start of image (``FF D8``) the file is a chain of marker segments. A marker is
    ``FF`` plus a code that is neither ``00`` nor ``FF``; any number of ``FF`` fill bytes
    may precede it. The markers ``01``, ``D0``-``D7``, ``D8`` and ``D9`` stand alone;
    every other one is followed by a big-endian 16-bit length that counts itself and the
    rest of the segment. The frame header is the first ``C0``-``CF`` segment other than
    ``C4`` (Huffman tables), ``C8`` (reserved) and ``CC`` (arithmetic conditioning)::

        offset  size  field (offsets from the end of the marker)
             0     2  Lf: segment length
             2     1  P:  sample precision in bits (2 to 16)
             3     2  Y:  number of lines (height); 0 defers it to a DNL segment
             5     2  X:  samples per line (width)
             7     1  Nf: number of components

    The walk seeks from segment to segment, so megabytes of metadata before the frame
    header cost a few small reads. It gives up after 65536 segments: no real file comes
    close, and a crafted one made of millions of empty segments cannot stall a scan.

BMP
    Microsoft Learn: ``BITMAPFILEHEADER``, ``BITMAPCOREHEADER``, ``BITMAPINFOHEADER``,
    ``BITMAPV4HEADER``, ``BITMAPV5HEADER``. Integers are little-endian::

        offset  size  field
             0     2  "BM"
             2    12  file size (4), reserved (4), offset of the pixel data (4)
            14     4  size of the DIB header that starts here

        DIB header of 12 bytes (BITMAPCOREHEADER):
            18     2  width
            20     2  height
            22     2  planes
            24     2  bits per pixel

        DIB header of 16 to 124 bytes (BITMAPINFOHEADER: 40, V4: 108, V5: 124, and the
        OS/2 2.x header of 16 to 64 bytes, which all start alike):
            18     4  width, signed
            22     4  height, signed; negative for a top-down bitmap
            26     2  planes
            28     2  bits per pixel: 1, 2, 4, 8 (palette), 16, 24 or 32

KTX
    Khronos, *KTX File Format Specification* 1.1. The identifier is followed by 32-bit
    fields in the byte order announced by ``endianness``: read as little-endian it is
    0x04030201 in a little-endian file and 0x01020304 in a big-endian one::

        offset  size  field
             0    12  identifier: AB 4B 54 58 20 31 31 BB 0D 0A 1A 0A
            12     4  endianness
            16    12  glType (0 for compressed data), glTypeSize, glFormat
            28     8  glInternalFormat, glBaseInternalFormat
            36    12  pixelWidth, pixelHeight (0: 1D), pixelDepth (0: not 3D)
            48     8  numberOfArrayElements (0: not an array), numberOfFaces (1 or 6)
            56     4  numberOfMipmapLevels (0: generate the chain at load time)

KTX 2
    Khronos, *KTX File Format Specification* 2.0; ``VkFormat`` values from the Khronos
    Vulkan specification. Everything is little-endian::

        offset  size  field
             0    12  identifier: AB 4B 54 58 20 32 30 BB 0D 0A 1A 0A
            12     8  vkFormat, typeSize
            20    12  pixelWidth, pixelHeight (0: 1D), pixelDepth (0: not 3D)
            32     8  layerCount (0: not an array), faceCount (1 or 6)
            40     8  levelCount (0: base level only), supercompressionScheme

    ``VK_FORMAT_UNDEFINED`` (0) leaves the description of the data to a later block of
    the file, which the probe does not read; such a file is reported as block-compressed
    only when its supercompression scheme is BasisLZ (1), which implies it.

ASTC
    Arm, *ASTC Encoder* documentation, ".astc File Format"::

        offset  size  field
             0     4  magic: 13 AB A1 5C
             4     3  block dimensions x, y, z (one byte each)
             7     9  image dimensions x, y, z (24-bit little-endian each)

DDS
    Recognized by its ``"DDS "`` magic and handed to :mod:`bea.formats.dds`, which follows
    the Microsoft Learn "DDS" programming guide.

Pixel format names
------------------
``L<n>``, ``LA<n>``, ``RGB<n>``, ``RGBA<n>``, ``CMYK<n>``
    greyscale, greyscale + alpha, colour, colour + alpha or four inks, ``<n>`` bits per
    channel (``RGB5`` covers the 15 and 16-bit packed layouts);
``P<n>``
    palette indices of ``<n>`` bits;
``GL_0x<hex>``
    the OpenGL internal format of a KTX file, ``VK_<number>`` the ``VkFormat`` of a KTX 2
    file;
``ASTC_<x>x<y>``
    ASTC blocks of that footprint (``ASTC_<x>x<y>x<z>`` for 3D blocks);
anything else
    a DDS format name, as chosen by :mod:`bea.formats.dds`.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Buffer
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from bea.errors import FormatError
from bea.fsutil import fs_path

__all__ = ["IMAGE_EXTENSIONS", "ImageInfo", "probe", "probe_bytes"]

#: Lower-case extensions of the containers :func:`probe` recognizes.
IMAGE_EXTENSIONS = frozenset(
    {".png", ".tga", ".dds", ".jpg", ".jpeg", ".bmp", ".ktx", ".ktx2", ".astc"}
)

# The longest fixed header: a DDS file with its DX10 extension. Every container except
# JPEG is decided from this one read.
_HEAD_SIZE = 148
# Buffer of the file handle. Python's default has grown to 128 KiB, which would pull that
# much from every file of a scan when 29 bytes answer the question.
_READ_BUFFER = 4096

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR = struct.Struct(">I4sIIBBBBB")
_PNG_HEADER_SIZE = len(_PNG_SIGNATURE) + _PNG_IHDR.size
_PNG_IHDR_LENGTH = 13
_PNG_MAX_DIMENSION = 2**31 - 1
#: Colour type -> (pixel format prefix, bit depths the PNG specification allows).
_PNG_COLOUR_TYPES: dict[int, tuple[str, tuple[int, ...]]] = {
    0: ("L", (1, 2, 4, 8, 16)),
    2: ("RGB", (8, 16)),
    3: ("P", (1, 2, 4, 8)),
    4: ("LA", (8, 16)),
    6: ("RGBA", (8, 16)),
}

_TGA_EXTENSION = ".tga"
_TGA_HEADER = struct.Struct("<BBBHHBHHHHBB")
_TGA_FOOTER_SIZE = 26
_TGA_FOOTER_SIGNATURE = b"TRUEVISION-XFILE.\x00"
_TGA_RLE_BIT = 0x08
_TGA_TOP_DOWN_BIT = 0x20
_TGA_COLOUR_MAPPED = 1
_TGA_IMAGE_TYPES = frozenset({1, 2, 3, 9, 10, 11})
_TGA_PIXEL_DEPTHS = frozenset({8, 15, 16, 24, 32})
_TGA_MAP_ENTRY_SIZES = frozenset({15, 16, 24, 32})
#: (image type without the run-length bit, pixel depth) -> pixel format.
_TGA_FORMATS: dict[tuple[int, int], str] = {
    (1, 8): "P8",
    (2, 15): "RGB5",
    (2, 16): "RGB5",
    (2, 24): "RGB8",
    (2, 32): "RGBA8",
    (3, 8): "L8",
    (3, 16): "LA8",
}

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA
#: Markers that are not followed by a length (TEM, RST0-RST7, SOI); EOI is handled apart.
_JPEG_STANDALONE = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})
#: SOF0-SOF15: every C0-CF code except DHT (C4), JPG (C8) and DAC (CC).
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_PROGRESSIVE = frozenset({0xC2, 0xC6, 0xCA, 0xCE})
_JPEG_FRAME_HEADER = struct.Struct(">HBHHB")
_JPEG_COMPONENTS = {1: "L", 3: "RGB", 4: "CMYK"}
_JPEG_MAX_SEGMENTS = 65_536
_JPEG_FILL_BLOCK = 4096

_BMP_MAGIC = b"BM"
_BMP_DIB_OFFSET = 14
_BMP_CORE_HEADER_SIZE = 12
# Smallest and largest DIB headers that share the BITMAPINFOHEADER layout: the shortest
# OS/2 2.x form and BITMAPV5HEADER.
_BMP_INFO_HEADER_SIZES = range(16, 125)
_BMP_CORE_FIELDS = struct.Struct("<HHHH")
_BMP_INFO_FIELDS = struct.Struct("<iiHH")
_BMP_FORMATS = {1: "P1", 2: "P2", 4: "P4", 8: "P8", 16: "RGB5", 24: "RGB8", 32: "RGBA8"}

_KTX1_IDENTIFIER = b"\xabKTX 11\xbb\r\n\x1a\n"
_KTX1_HEADER_SIZE = 60
_KTX1_LITTLE_ENDIAN = 0x04030201
_KTX1_BIG_ENDIAN = 0x01020304

_KTX2_IDENTIFIER = b"\xabKTX 20\xbb\r\n\x1a\n"
_KTX2_FIELDS = struct.Struct("<9I")
_KTX2_HEADER_SIZE = len(_KTX2_IDENTIFIER) + _KTX2_FIELDS.size
_KTX2_BASIS_LZ = 1
#: ``VkFormat`` values whose names end in ``_BLOCK``: BC1-BC7, ETC2, EAC and ASTC (LDR);
#: PVRTC; ASTC HDR; ASTC with 3D blocks.
_VK_BLOCK_FORMATS = (
    range(131, 185),
    range(1000054000, 1000054008),
    range(1000066000, 1000066014),
    range(1000288000, 1000288030),
)
_CUBE_FACES = 6

_ASTC_MAGIC = b"\x13\xab\xa1\x5c"
_ASTC_HEADER_SIZE = 16

_DDS_MAGIC = b"DDS "


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """What the header of an image file says about the image."""

    #: ``"png"``, ``"tga"``, ``"dds"``, ``"jpeg"``, ``"bmp"``, ``"ktx"``, ``"ktx2"`` or
    #: ``"astc"``.
    container: str
    #: Size of the base image (mip level 0), in pixels.
    width: int
    height: int
    #: Number of slices of a volume texture; 1 for a flat image.
    depth: int = 1
    #: Number of mip levels stored in the file, the base image included.
    mip_count: int = 1
    #: Array elements multiplied by cube faces.
    layers: int = 1
    #: Pixel layout (see the module docstring), or ``None`` when the header describes a
    #: layout this module has no name for.
    pixel_format: str | None = None
    #: Bits per channel (per palette index for palette images), when the container states it.
    bit_depth: int | None = None
    #: True when the pixel data is block-compressed (BCn, ETC, ASTC ...).
    compressed: bool = False
    #: Facts worth surfacing: ``"interlaced"``, ``"palette"``, ``"rle"``, ``"top-down"``,
    #: ``"bottom-up"``, ``"progressive"``, ``"big-endian"``, ``"supercompressed"``,
    #: ``"cubemap"``, ``"volume"``, ``"array"``.
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in reports)."""
        return {
            "container": self.container,
            "width": self.width,
            "height": self.height,
            "depth": self.depth,
            "mip_count": self.mip_count,
            "layers": self.layers,
            "pixel_format": self.pixel_format,
            "bit_depth": self.bit_depth,
            "compressed": self.compressed,
            "notes": list(self.notes),
        }


class _Source(Protocol):
    """Random access to the bytes being probed, shared by files and in-memory data."""

    def read(self, offset: int, size: int) -> bytes:
        """Return the ``size`` bytes at ``offset``, or fewer when the data ends first."""

    def size(self) -> int:
        """Return the total number of bytes."""


class _BytesSource:
    """In-memory data."""

    __slots__ = ("_view",)

    def __init__(self, view: memoryview) -> None:
        self._view = view

    def read(self, offset: int, size: int) -> bytes:
        return bytes(self._view[offset : offset + size])

    def size(self) -> int:
        return len(self._view)


class _FileSource:
    """An open file. Its small buffer serves neighbouring reads without touching the disk."""

    __slots__ = ("_handle",)

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle

    def read(self, offset: int, size: int) -> bytes:
        self._handle.seek(offset)
        return self._handle.read(size)

    def size(self) -> int:
        return self._handle.seek(0, os.SEEK_END)


def probe(path: str | os.PathLike[str]) -> ImageInfo | None:
    """Read the dimensions and pixel format of an image file from its header.

    Only the bytes that are needed are read: one small block for most containers, plus a
    hop per marker segment for a JPEG whose frame header lies behind large metadata.

    Returns:
        The description of the image, or ``None`` when the file is none of the known
        containers (no known signature, and not a plausible TGA).

    Raises:
        FormatError: the signature is recognized but the header is malformed or truncated.
        OSError: the file cannot be opened or read.
    """
    name = os.fspath(path)
    extension = os.path.splitext(name)[1].lower()
    with open(fs_path(name), "rb", buffering=_READ_BUFFER) as handle:
        return _probe(_FileSource(handle), extension)


def probe_bytes(data: Buffer, *, extension: str = "") -> ImageInfo | None:
    """Do what :func:`probe` does, on bytes that are already in memory.

    ``data`` is the whole file or a prefix that is long enough to hold the header.
    ``extension`` (with its dot, such as ``".tga"``) matters only for TGA, which has no
    signature; the TGA 2.0 footer, the other way to recognize one, can only be seen when
    ``data`` is the whole file.

    Raises:
        FormatError: the signature is recognized but the header is malformed or truncated.
        TypeError: ``data`` is not bytes-like or ``extension`` is not a string.
    """
    if not isinstance(extension, str):
        raise TypeError(f"expected an extension such as '.tga', got {type(extension).__name__}")
    with memoryview(data) as raw, raw.cast("B") as view:
        return _probe(_BytesSource(view), extension.lower())


def _probe(source: _Source, extension: str) -> ImageInfo | None:
    """Identify the container by its signature and parse its header."""
    head = source.read(0, _HEAD_SIZE)
    if head.startswith(_PNG_SIGNATURE):
        return _parse_png(head)
    if head.startswith(_JPEG_SOI):
        return _parse_jpeg(source)
    if head.startswith(_DDS_MAGIC):
        return _parse_dds(head)
    if head.startswith(_KTX1_IDENTIFIER):
        return _parse_ktx1(head)
    if head.startswith(_KTX2_IDENTIFIER):
        return _parse_ktx2(head)
    if head.startswith(_ASTC_MAGIC):
        return _parse_astc(head)
    if head.startswith(_BMP_MAGIC):
        return _parse_bmp(head)
    info = _parse_tga(head)
    # The header test is free (it uses bytes that are already here), so it goes first:
    # the footer costs a seek to the end of the file and is rarely needed.
    if info is not None and (extension == _TGA_EXTENSION or _has_tga_footer(source)):
        return info
    return None


def _require(head: bytes, size: int, container: str) -> None:
    """Raise unless ``head`` holds the ``size`` header bytes a parser is about to unpack."""
    if len(head) < size:
        raise FormatError(
            f"{container}: header truncated: {size} bytes needed, only {len(head)} available"
        )


def _parse_png(head: bytes) -> ImageInfo:
    _require(head, _PNG_HEADER_SIZE, "PNG")
    (
        length,
        chunk_type,
        width,
        height,
        bit_depth,
        colour_type,
        compression,
        filter_method,
        interlace,
    ) = _PNG_IHDR.unpack_from(head, len(_PNG_SIGNATURE))
    if chunk_type != b"IHDR":
        raise FormatError(
            f"PNG: the first chunk (type at offset 12) is {chunk_type!r}, expected b'IHDR'"
        )
    if length != _PNG_IHDR_LENGTH:
        raise FormatError(
            f"PNG: IHDR chunk length (offset 8) is {length}, expected {_PNG_IHDR_LENGTH}"
        )
    for name, offset, value in (("width", 16, width), ("height", 20, height)):
        if not 1 <= value <= _PNG_MAX_DIMENSION:
            raise FormatError(
                f"PNG: IHDR {name} (offset {offset}) is {value}, expected 1 to {_PNG_MAX_DIMENSION}"
            )
    if colour_type not in _PNG_COLOUR_TYPES:
        raise FormatError(
            f"PNG: IHDR colour type (offset 25) is {colour_type}, expected 0, 2, 3, 4 or 6"
        )
    prefix, allowed_depths = _PNG_COLOUR_TYPES[colour_type]
    if bit_depth not in allowed_depths:
        allowed = ", ".join(str(depth) for depth in allowed_depths)
        raise FormatError(
            f"PNG: IHDR bit depth (offset 24) is {bit_depth}, "
            f"but colour type {colour_type} allows only {allowed}"
        )
    if compression != 0:
        raise FormatError(f"PNG: IHDR compression method (offset 26) is {compression}, expected 0")
    if filter_method != 0:
        raise FormatError(f"PNG: IHDR filter method (offset 27) is {filter_method}, expected 0")
    if interlace not in (0, 1):
        raise FormatError(f"PNG: IHDR interlace method (offset 28) is {interlace}, expected 0 or 1")
    notes = (("interlaced",) if interlace else ()) + (("palette",) if colour_type == 3 else ())
    return ImageInfo(
        container="png",
        width=width,
        height=height,
        pixel_format=f"{prefix}{bit_depth}",
        bit_depth=bit_depth,
        notes=notes,
    )


def _parse_tga(head: bytes) -> ImageInfo | None:
    """Return the image a TGA header describes, or ``None`` when the header is not plausible.

    TGA has no signature, so this is a judgement, not a validation: bytes that do not
    look like a TGA header are simply not a TGA. A colour map, when present, must declare
    an entry size the specification defines (15, 16, 24 or 32 bits); its length may be 0,
    which some writers use for an empty palette.
    """
    if len(head) < _TGA_HEADER.size:
        return None
    (
        _id_length,
        colour_map_type,
        image_type,
        _map_first,
        _map_length,
        map_entry_size,
        _x_origin,
        _y_origin,
        width,
        height,
        pixel_depth,
        descriptor,
    ) = _TGA_HEADER.unpack_from(head)
    base_type = image_type & ~_TGA_RLE_BIT
    plausible = (
        colour_map_type in (0, 1)
        and image_type in _TGA_IMAGE_TYPES
        and pixel_depth in _TGA_PIXEL_DEPTHS
        and width >= 1
        and height >= 1
        and (base_type != _TGA_COLOUR_MAPPED or colour_map_type == 1)
        and (colour_map_type == 0 or map_entry_size in _TGA_MAP_ENTRY_SIZES)
    )
    if not plausible:
        return None
    rle = ("rle",) if image_type & _TGA_RLE_BIT else ()
    orientation = "top-down" if descriptor & _TGA_TOP_DOWN_BIT else "bottom-up"
    return ImageInfo(
        container="tga",
        width=width,
        height=height,
        pixel_format=_TGA_FORMATS.get((base_type, pixel_depth)),
        notes=(*rle, orientation),
    )


def _has_tga_footer(source: _Source) -> bool:
    """Tell whether the data ends with the footer of a TGA 2.0 file."""
    total = source.size()
    if total < _TGA_HEADER.size + _TGA_FOOTER_SIZE:
        return False
    signature_size = len(_TGA_FOOTER_SIGNATURE)
    return source.read(total - signature_size, signature_size) == _TGA_FOOTER_SIGNATURE


def _parse_jpeg(source: _Source) -> ImageInfo:
    """Walk the marker segments that follow the start of image, up to the frame header."""
    position = len(_JPEG_SOI)
    for _ in range(_JPEG_MAX_SEGMENTS):
        start = position
        code, position = _jpeg_marker(source, position)
        if code in _JPEG_STANDALONE:
            continue
        if code in (_JPEG_EOI, _JPEG_SOS):
            what = "end of image (EOI)" if code == _JPEG_EOI else "start of scan (SOS)"
            raise FormatError(f"JPEG: {what} at offset {start} before any frame header (SOFn)")
        # One read serves both cases: the length of any segment, and the whole fixed
        # part of a frame header.
        fields = source.read(position, _JPEG_FRAME_HEADER.size)
        if len(fields) < 2:
            raise _jpeg_truncated("a segment length", position)
        length = int.from_bytes(fields[:2], "big")
        if length < 2:
            raise FormatError(
                f"JPEG: segment FF {code:02X} at offset {start} has length {length}, "
                "expected at least 2 (the length counts its own two bytes)"
            )
        if code in _JPEG_FRAME_MARKERS:
            return _jpeg_frame(code, start, fields)
        position += length
    raise FormatError(
        f"JPEG: no frame header (SOFn) within the first {_JPEG_MAX_SEGMENTS} marker segments"
    )


def _jpeg_marker(source: _Source, position: int) -> tuple[int, int]:
    """Read the marker that starts at ``position``; return its code and the offset after it."""
    marker = source.read(position, 2)
    if len(marker) < 2:
        raise _jpeg_truncated("a marker", position)
    if marker[0] != 0xFF:
        raise FormatError(
            f"JPEG: expected a marker (FF) at offset {position}, found {marker[0]:02X}"
        )
    code = marker[1]
    after = position + 2
    while code == 0xFF:
        # Fill bytes: skip them a block at a time, so that a long run costs few reads.
        block = source.read(after, _JPEG_FILL_BLOCK)
        if not block:
            raise _jpeg_truncated("a marker", position)
        rest = block.lstrip(b"\xff")
        after += len(block) - len(rest)
        if rest:
            code = rest[0]
            after += 1
    if code == 0x00:
        raise FormatError(f"JPEG: FF 00 at offset {after - 2} is not a marker")
    return code, after


def _jpeg_truncated(what: str, offset: int) -> FormatError:
    return FormatError(
        f"JPEG: the data ends before any frame header (SOFn): expected {what} at offset {offset}"
    )


def _jpeg_frame(code: int, start: int, fields: bytes) -> ImageInfo:
    """Interpret the fixed part of the frame header ``FF <code>`` found at ``start``."""
    where = f"JPEG: frame header FF {code:02X} at offset {start}"
    if len(fields) < _JPEG_FRAME_HEADER.size:
        raise FormatError(
            f"{where} is truncated: {_JPEG_FRAME_HEADER.size} bytes needed after the "
            f"marker, only {len(fields)} available"
        )
    length, precision, height, width, components = _JPEG_FRAME_HEADER.unpack(fields)
    if length < _JPEG_FRAME_HEADER.size:
        raise FormatError(
            f"{where} has length {length}, expected at least {_JPEG_FRAME_HEADER.size}"
        )
    if not 2 <= precision <= 16:
        raise FormatError(f"{where} has a sample precision of {precision} bits, expected 2 to 16")
    if width == 0:
        raise FormatError(f"{where} declares a width of 0")
    if height == 0:
        raise FormatError(
            f"{where} declares 0 lines: the height is deferred to a DNL segment, "
            "which a header probe cannot resolve"
        )
    if components == 0:
        raise FormatError(f"{where} declares 0 components")
    name = _JPEG_COMPONENTS.get(components)
    return ImageInfo(
        container="jpeg",
        width=width,
        height=height,
        pixel_format=f"{name}{precision}" if name is not None else None,
        bit_depth=precision,
        notes=("progressive",) if code in _JPEG_PROGRESSIVE else (),
    )


def _parse_bmp(head: bytes) -> ImageInfo:
    _require(head, _BMP_DIB_OFFSET + 4, "BMP")
    dib_size = int.from_bytes(head[_BMP_DIB_OFFSET : _BMP_DIB_OFFSET + 4], "little")
    fields_offset = _BMP_DIB_OFFSET + 4
    if dib_size == _BMP_CORE_HEADER_SIZE:
        fields = _BMP_CORE_FIELDS
        height_offset = fields_offset + 2
    elif dib_size in _BMP_INFO_HEADER_SIZES:
        fields = _BMP_INFO_FIELDS
        height_offset = fields_offset + 4
    else:
        raise FormatError(
            f"BMP: DIB header size (offset {_BMP_DIB_OFFSET}) is {dib_size}, expected "
            f"{_BMP_CORE_HEADER_SIZE} or {_BMP_INFO_HEADER_SIZES[0]} to "
            f"{_BMP_INFO_HEADER_SIZES[-1]}"
        )
    _require(head, fields_offset + fields.size, "BMP")
    width, height, _planes, bits = fields.unpack_from(head, fields_offset)
    if width < 1:
        raise FormatError(f"BMP: width (offset {fields_offset}) is {width}, expected 1 or more")
    if height == 0:
        raise FormatError(f"BMP: height (offset {height_offset}) is 0")
    if bits not in _BMP_FORMATS:
        raise FormatError(
            f"BMP: bits per pixel (offset {fields_offset + fields.size - 2}) is {bits}, "
            "expected 1, 2, 4, 8, 16, 24 or 32"
        )
    return ImageInfo(
        container="bmp",
        width=width,
        height=abs(height),
        pixel_format=_BMP_FORMATS[bits],
        notes=("top-down",) if height < 0 else (),
    )


def _parse_ktx1(head: bytes) -> ImageInfo:
    _require(head, _KTX1_HEADER_SIZE, "KTX")
    endianness = int.from_bytes(head[12:16], "little")
    if endianness == _KTX1_LITTLE_ENDIAN:
        order = "<"
    elif endianness == _KTX1_BIG_ENDIAN:
        order = ">"
    else:
        raise FormatError(
            f"KTX: endianness (offset 12) is 0x{endianness:08X}, "
            f"expected 0x{_KTX1_LITTLE_ENDIAN:08X} or 0x{_KTX1_BIG_ENDIAN:08X}"
        )
    (
        gl_type,
        _gl_type_size,
        _gl_format,
        gl_internal_format,
        _gl_base_internal_format,
        width,
        height,
        depth,
        array_elements,
        faces,
        mip_levels,
    ) = struct.unpack_from(f"{order}11I", head, 16)
    if width == 0:
        raise FormatError("KTX: pixelWidth (offset 36) is 0")
    if faces not in (1, _CUBE_FACES):
        raise FormatError(
            f"KTX: numberOfFaces (offset 52) is {faces}, expected 1, or 6 for a cube map"
        )
    return ImageInfo(
        container="ktx",
        width=width,
        # A zero says "this dimension does not exist" (1D, not 3D, not an array) or, for
        # the mip levels, "only the base level is stored".
        height=height or 1,
        depth=depth or 1,
        mip_count=mip_levels or 1,
        layers=(array_elements or 1) * faces,
        pixel_format=f"GL_0x{gl_internal_format:04X}",
        compressed=gl_type == 0,
        notes=("big-endian",) if order == ">" else (),
    )


def _parse_ktx2(head: bytes) -> ImageInfo:
    _require(head, _KTX2_HEADER_SIZE, "KTX2")
    (
        vk_format,
        _type_size,
        width,
        height,
        depth,
        layer_count,
        face_count,
        level_count,
        supercompression,
    ) = _KTX2_FIELDS.unpack_from(head, len(_KTX2_IDENTIFIER))
    if width == 0:
        raise FormatError("KTX2: pixelWidth (offset 20) is 0")
    if face_count not in (1, _CUBE_FACES):
        raise FormatError(
            f"KTX2: faceCount (offset 36) is {face_count}, expected 1, or 6 for a cube map"
        )
    return ImageInfo(
        container="ktx2",
        width=width,
        height=height or 1,
        depth=depth or 1,
        mip_count=level_count or 1,
        layers=(layer_count or 1) * face_count,
        pixel_format=f"VK_{vk_format}",
        compressed=(
            any(vk_format in formats for formats in _VK_BLOCK_FORMATS)
            or supercompression == _KTX2_BASIS_LZ
        ),
        notes=("supercompressed",) if supercompression else (),
    )


def _parse_astc(head: bytes) -> ImageInfo:
    _require(head, _ASTC_HEADER_SIZE, "ASTC")
    block_x, block_y, block_z = head[4:7]
    width, height, depth = (
        int.from_bytes(head[offset : offset + 3], "little") for offset in (7, 10, 13)
    )
    for name, offset, value in (
        ("block dimension x", 4, block_x),
        ("block dimension y", 5, block_y),
        ("block dimension z", 6, block_z),
        ("image dimension x", 7, width),
        ("image dimension y", 10, height),
        ("image dimension z", 13, depth),
    ):
        if value == 0:
            raise FormatError(f"ASTC: {name} (offset {offset}) is 0")
    footprint = f"{block_x}x{block_y}" + (f"x{block_z}" if block_z > 1 else "")
    return ImageInfo(
        container="astc",
        width=width,
        height=height,
        depth=depth,
        pixel_format=f"ASTC_{footprint}",
        compressed=True,
    )


def _parse_dds(head: bytes) -> ImageInfo:
    # Imported here, not at module level: probing the other containers must not depend
    # on the DDS module (which pulls in Pillow) being importable.
    from bea.formats.dds import parse_header

    header = parse_header(head)
    notes = [
        note
        for note, applies in (
            ("cubemap", header.is_cubemap),
            ("volume", header.is_volume),
            ("array", header.array_size > 1),
        )
        if applies
    ]
    return ImageInfo(
        container="dds",
        width=header.width,
        height=header.height,
        depth=header.depth,
        mip_count=header.mip_count,
        layers=header.layers,
        pixel_format=header.format.name,
        compressed=header.format.compressed,
        notes=tuple(notes),
    )
