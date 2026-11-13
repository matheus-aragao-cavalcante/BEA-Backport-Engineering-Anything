"""Tests for bea.formats.imageinfo: image headers probed without decoding a pixel.

Every input is synthetic. Headers are laid out by hand with ``struct`` from the public
format documents, so no expectation comes from the code under test; wherever Pillow can
write or read a container it serves as an independent oracle.
"""

from __future__ import annotations

import dataclasses
import io
import json
import random
import struct
import sys
import types
import zlib
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from PIL import Image

from bea import fsutil
from bea.errors import FormatError
from bea.formats import imageinfo
from bea.formats.imageinfo import ImageInfo

# Odd, different and not powers of two: a swapped or rounded field cannot go unnoticed.
WIDTH = 301
HEIGHT = 127

# ----------------------------------------------------------------------------------
# Hand-made files (one builder per container, straight from the format documents)
# ----------------------------------------------------------------------------------

PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
#: Samples per pixel of each PNG colour type.
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
#: Bit depths the PNG specification allows for each colour type.
PNG_ALLOWED_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
#: Adam7: first column, first row, column step and row step of the seven passes.
ADAM7 = [
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
]


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    """One PNG chunk: data length, type, data, CRC of type and data."""
    crc = zlib.crc32(kind + payload)
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_header(
    width: int,
    height: int,
    bit_depth: int,
    colour_type: int,
    *,
    compression: int = 0,
    filter_method: int = 0,
    interlace: int = 0,
) -> bytes:
    """The signature and the IHDR chunk: the first 33 bytes of a PNG file."""
    ihdr = struct.pack(
        ">IIBBBBB", width, height, bit_depth, colour_type, compression, filter_method, interlace
    )
    return PNG_SIGNATURE + png_chunk(b"IHDR", ihdr)


def png_file(
    width: int, height: int, bit_depth: int, colour_type: int, *, interlace: int = 0
) -> bytes:
    """A complete PNG whose samples are all zero."""
    bits_per_pixel = PNG_CHANNELS[colour_type] * bit_depth

    def scanlines(columns: int, rows: int) -> bytes:
        # Each row: a filter-type byte (0: none), then the packed samples.
        return (b"\x00" + bytes((columns * bits_per_pixel + 7) // 8)) * rows

    if interlace:
        raw = b""
        for first_column, first_row, column_step, row_step in ADAM7:
            columns = (width - first_column + column_step - 1) // column_step
            rows = (height - first_row + row_step - 1) // row_step
            if columns > 0 and rows > 0:
                raw += scanlines(columns, rows)
    else:
        raw = scanlines(width, height)
    palette = png_chunk(b"PLTE", bytes(3 << bit_depth)) if colour_type == 3 else b""
    return (
        png_header(width, height, bit_depth, colour_type, interlace=interlace)
        + palette
        + png_chunk(b"IDAT", zlib.compress(raw))
        + png_chunk(b"IEND", b"")
    )


#: The 26-byte footer of a TGA 2.0 file: no extension area, no developer directory.
TGA_FOOTER = bytes(8) + b"TRUEVISION-XFILE.\x00"


def tga_header(
    width: int,
    height: int,
    image_type: int,
    pixel_depth: int,
    *,
    descriptor: int = 0,
    colour_map_type: int = 0,
    colour_map_length: int = 0,
    colour_map_entry_size: int = 0,
    id_length: int = 0,
) -> bytes:
    """The 18-byte TGA header."""
    return struct.pack(
        "<BBBHHBHHHHBB",
        id_length,
        colour_map_type,
        image_type,
        0,  # colour map: index of the first entry
        colour_map_length,
        colour_map_entry_size,
        0,  # x origin
        0,  # y origin
        width,
        height,
        pixel_depth,
        descriptor,
    )


def tga_file(
    width: int,
    height: int,
    image_type: int,
    pixel_depth: int,
    *,
    descriptor: int = 0,
    footer: bool = False,
) -> bytes:
    """A complete TGA with black pixels; types 1 and 9 get a 256-entry colour map."""
    colour_mapped = image_type in (1, 9)
    header = tga_header(
        width,
        height,
        image_type,
        pixel_depth,
        descriptor=descriptor,
        colour_map_type=1 if colour_mapped else 0,
        colour_map_length=256 if colour_mapped else 0,
        colour_map_entry_size=24 if colour_mapped else 0,
    )
    colour_map = bytes(256 * 3) if colour_mapped else b""
    pixel = bytes((pixel_depth + 7) // 8)
    if image_type >= 9:
        # Run-length packets never cross a row: 0x80 | (count - 1), then the pixel.
        row = b""
        remaining = width
        while remaining:
            count = min(128, remaining)
            row += bytes([0x80 | (count - 1)]) + pixel
            remaining -= count
        pixels = row * height
    else:
        pixels = pixel * (width * height)
    return header + colour_map + pixels + (TGA_FOOTER if footer else b"")


SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def jpeg_segment(code: int, payload: bytes = b"") -> bytes:
    """A marker segment: FF, the code, a length that counts itself, the payload."""
    return bytes([0xFF, code]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_frame(
    code: int, width: int, height: int, *, components: int = 3, precision: int = 8
) -> bytes:
    """A frame header (SOFn) with one three-byte specification per component."""
    specifications = b"".join(bytes([index + 1, 0x11, 0]) for index in range(components))
    return jpeg_segment(
        code, struct.pack(">BHHB", precision, height, width, components) + specifications
    )


#: The APP0 segment of a JFIF file: 18 bytes.
JFIF = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
#: A scan header; no entropy-coded data follows it in these files.
SOS = jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
#: Every start-of-frame code of ITU-T T.81, and the progressive ones among them.
FRAME_MARKERS = [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF]
PROGRESSIVE_MARKERS = {0xC2, 0xC6, 0xCA, 0xCE}


def jpeg_file(*segments: bytes) -> bytes:
    """Start of image, the given segments, a scan header and the end of image."""
    return SOI + b"".join(segments) + SOS + EOI


def _bmp(dib_header: bytes, palette: bytes, pixels: bytes) -> bytes:
    offset = 14 + len(dib_header) + len(palette)
    file_header = b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset)
    return file_header + dib_header + palette + pixels


def _bmp_pixels(width: int, height: int, bits: int) -> bytes:
    stride = (width * bits + 31) // 32 * 4  # rows are padded to a multiple of four bytes
    return bytes(stride * abs(height))


def bmp_info_file(width: int, height: int, bits: int, *, dib_size: int = 40) -> bytes:
    """A complete BMP with a BITMAPINFOHEADER, or with a header that starts like one."""
    pixels = _bmp_pixels(width, height, bits)
    fields = struct.pack(
        "<IiiHHIIiiII", dib_size, width, height, 1, bits, 0, len(pixels), 2835, 2835, 0, 0
    )
    dib_header = fields[:dib_size].ljust(dib_size, b"\x00")
    palette = bytes(4 << bits) if bits <= 8 else b""
    return _bmp(dib_header, palette, pixels)


def bmp_core_file(width: int, height: int, bits: int) -> bytes:
    """A complete BMP with the 12-byte BITMAPCOREHEADER."""
    dib_header = struct.pack("<IHHHH", 12, width, height, 1, bits)
    palette = bytes(3 << bits) if bits <= 8 else b""
    return _bmp(dib_header, palette, _bmp_pixels(width, height, bits))


KTX1_IDENTIFIER = bytes([0xAB, 0x4B, 0x54, 0x58, 0x20, 0x31, 0x31, 0xBB, 0x0D, 0x0A, 0x1A, 0x0A])
KTX2_IDENTIFIER = bytes([0xAB, 0x4B, 0x54, 0x58, 0x20, 0x32, 0x30, 0xBB, 0x0D, 0x0A, 0x1A, 0x0A])


def ktx1_header(
    width: int,
    height: int,
    *,
    order: str = "<",
    endianness: int = 0x04030201,
    gl_type: int = 0x1401,  # GL_UNSIGNED_BYTE; 0 for compressed data
    gl_internal_format: int = 0x8058,  # GL_RGBA8
    depth: int = 0,
    array_elements: int = 0,
    faces: int = 1,
    mip_levels: int = 1,
) -> bytes:
    """The 64-byte KTX 1 header, in the byte order ``order`` (``<`` or ``>``)."""
    fields = (
        endianness,
        gl_type,
        1,  # glTypeSize
        0x1908 if gl_type else 0,  # glFormat: GL_RGBA, or 0 for compressed data
        gl_internal_format,
        0x1908,  # glBaseInternalFormat: GL_RGBA
        width,
        height,
        depth,
        array_elements,
        faces,
        mip_levels,
        0,  # bytesOfKeyValueData
    )
    return KTX1_IDENTIFIER + struct.pack(f"{order}13I", *fields)


def ktx2_header(
    width: int,
    height: int,
    *,
    vk_format: int = 37,  # VK_FORMAT_R8G8B8A8_UNORM
    depth: int = 0,
    layer_count: int = 0,
    face_count: int = 1,
    level_count: int = 1,
    supercompression: int = 0,
) -> bytes:
    """The KTX 2 header and its (empty) index: 80 bytes."""
    fields = struct.pack(
        "<9I",
        vk_format,
        1,  # typeSize
        width,
        height,
        depth,
        layer_count,
        face_count,
        level_count,
        supercompression,
    )
    return KTX2_IDENTIFIER + fields + bytes(32)


def astc_header(block: tuple[int, int, int], size: tuple[int, int, int]) -> bytes:
    """The 16-byte header of an .astc file."""
    dimensions = b"".join(struct.pack("<I", dimension)[:3] for dimension in size)
    return bytes([0x13, 0xAB, 0xA1, 0x5C, *block]) + dimensions


#: DDS pixel format: the FourCC "DXT1" (flag 0x4), which is BC1.
DDS_DXT1 = struct.pack("<II4sI4I", 32, 0x4, b"DXT1", 0, 0, 0, 0, 0)
#: DDS pixel format: the FourCC "DX10", which announces a DDS_HEADER_DXT10.
DDS_DX10 = struct.pack("<II4sI4I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
#: DDS pixel format: 32 bits, RGB (0x40) with alpha (0x1), blue in the low byte.
DDS_BGRA = struct.pack("<II4sI4I", 32, 0x41, bytes(4), 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)


def dds_header(
    width: int,
    height: int,
    *,
    mip_count: int = 1,
    depth: int = 0,
    caps2: int = 0,
    pixel_format: bytes = DDS_DXT1,
    dx10: tuple[int, int, int, int, int] | None = None,
) -> bytes:
    """A DDS header, field by field; ``dx10`` appends a DDS_HEADER_DXT10."""
    flags = 0x1 | 0x2 | 0x4 | 0x1000  # CAPS | HEIGHT | WIDTH | PIXELFORMAT
    caps = 0x1000  # TEXTURE
    if mip_count > 1:
        flags |= 0x20000  # MIPMAPCOUNT
        caps |= 0x8 | 0x400000  # COMPLEX | MIPMAP
    if depth:
        flags |= 0x800000  # DEPTH
    header = struct.pack("<4s7I", b"DDS ", 124, flags, height, width, 0, depth, mip_count)
    header += bytes(4 * 11)  # dwReserved1
    header += pixel_format
    header += struct.pack("<5I", caps, caps2, 0, 0, 0)  # dwCaps .. dwCaps4, dwReserved2
    assert len(header) == 128
    if dx10 is not None:
        # dxgiFormat, resourceDimension, miscFlag, arraySize, miscFlags2
        header += struct.pack("<5I", *dx10)
    return header


def pillow_reads(
    data: bytes, *, decode: bool = True
) -> tuple[str | None, tuple[int, int], dict[str, Any]]:
    """Let Pillow identify (and decode) a hand-made file: format, size and info."""
    with Image.open(io.BytesIO(data)) as image:
        if decode:
            image.load()
        return image.format, image.size, dict(image.info)


class Sample(NamedTuple):
    """A well-formed file and what its header says, both written down by hand."""

    extension: str
    data: bytes
    expected: ImageInfo
    #: Length of the signature (0 for TGA, which has none).
    signature_size: int
    #: Number of leading bytes that hold everything the probe reports.
    header_size: int


SAMPLES = {
    "png": Sample(
        ".png",
        png_file(WIDTH, HEIGHT, 8, 6),
        ImageInfo("png", WIDTH, HEIGHT, pixel_format="RGBA8", bit_depth=8),
        8,
        29,
    ),
    "png-interlaced-palette": Sample(
        ".png",
        png_file(64, 48, 4, 3, interlace=1),
        ImageInfo("png", 64, 48, pixel_format="P4", bit_depth=4, notes=("interlaced", "palette")),
        8,
        29,
    ),
    # SOI (2) + JFIF (18): the frame marker is at 20, its fields end with Nf at 29.
    "jpeg": Sample(
        ".jpg",
        jpeg_file(JFIF, jpeg_frame(0xC0, 640, 360)),
        ImageInfo("jpeg", 640, 360, pixel_format="RGB8", bit_depth=8),
        2,
        30,
    ),
    # SOI (2) + comment (4 + 7): the frame marker is at 13, Nf at 22.
    "jpeg-progressive": Sample(
        ".jpeg",
        jpeg_file(jpeg_segment(0xFE, b"comment"), jpeg_frame(0xC2, 4096, 2048, components=1)),
        ImageInfo("jpeg", 4096, 2048, pixel_format="L8", bit_depth=8, notes=("progressive",)),
        2,
        23,
    ),
    "bmp": Sample(
        ".bmp",
        bmp_info_file(WIDTH, HEIGHT, 24),
        ImageInfo("bmp", WIDTH, HEIGHT, pixel_format="RGB8"),
        2,
        30,
    ),
    "bmp-core": Sample(
        ".bmp",
        bmp_core_file(WIDTH, HEIGHT, 8),
        ImageInfo("bmp", WIDTH, HEIGHT, pixel_format="P8"),
        2,
        26,
    ),
    "bmp-top-down": Sample(
        ".bmp",
        bmp_info_file(WIDTH, -HEIGHT, 32, dib_size=124),
        ImageInfo("bmp", WIDTH, HEIGHT, pixel_format="RGBA8", notes=("top-down",)),
        2,
        30,
    ),
    "ktx": Sample(
        ".ktx",
        ktx1_header(256, 128, mip_levels=9) + bytes(64),
        ImageInfo("ktx", 256, 128, mip_count=9, pixel_format="GL_0x8058"),
        12,
        60,
    ),
    "ktx-big-endian-cube-array": Sample(
        ".ktx",
        ktx1_header(
            512,
            512,
            order=">",
            gl_type=0,
            gl_internal_format=0x93B0,  # GL_COMPRESSED_RGBA_ASTC_4x4_KHR
            array_elements=2,
            faces=6,
            mip_levels=10,
        )
        + bytes(64),
        ImageInfo(
            "ktx",
            512,
            512,
            mip_count=10,
            layers=12,
            pixel_format="GL_0x93B0",
            compressed=True,
            notes=("big-endian",),
        ),
        12,
        60,
    ),
    "ktx2": Sample(
        ".ktx2",
        ktx2_header(1024, 512, vk_format=145, level_count=11, supercompression=2) + bytes(64),
        ImageInfo(
            "ktx2",
            1024,
            512,
            mip_count=11,
            pixel_format="VK_145",
            compressed=True,
            notes=("supercompressed",),
        ),
        12,
        48,
    ),
    "astc": Sample(
        ".astc",
        astc_header((8, 8, 1), (2048, 1024, 1)) + bytes(64),
        ImageInfo("astc", 2048, 1024, pixel_format="ASTC_8x8", compressed=True),
        4,
        16,
    ),
    "tga": Sample(
        ".tga",
        tga_file(WIDTH, HEIGHT, 2, 32, descriptor=0x28),
        ImageInfo("tga", WIDTH, HEIGHT, pixel_format="RGBA8", notes=("top-down",)),
        0,
        18,
    ),
    "tga-rle-palette": Sample(
        ".tga",
        tga_file(WIDTH, HEIGHT, 9, 8),
        ImageInfo("tga", WIDTH, HEIGHT, pixel_format="P8", notes=("rle", "bottom-up")),
        0,
        18,
    ),
}


class CountingFile(io.FileIO):
    """An unbuffered file that records how many bytes were really pulled from the OS."""

    def __init__(self, path: str) -> None:
        super().__init__(path, "rb")
        self.bytes_read = 0

    def readinto(self, buffer: Any) -> int | None:
        count = super().readinto(buffer)
        self.bytes_read += count or 0
        return count

    def read(self, size: int | None = -1) -> bytes | None:
        data = super().read(size)
        self.bytes_read += len(data or b"")
        return data

    def readall(self) -> bytes:
        data = super().readall()
        self.bytes_read += len(data)
        return data


@pytest.fixture
def opened_files(monkeypatch: pytest.MonkeyPatch) -> list[CountingFile]:
    """Make the module open its files as :class:`CountingFile`; return those it opened."""
    opened: list[CountingFile] = []

    def counting_open(path: str, mode: str = "r", buffering: int = -1) -> Any:
        assert mode == "rb"
        raw = CountingFile(path)
        opened.append(raw)
        if buffering == 0:
            return raw
        size = buffering if buffering > 0 else io.DEFAULT_BUFFER_SIZE
        return io.BufferedReader(raw, buffer_size=size)

    # A module-level name shadows the builtin for that module only.
    monkeypatch.setattr(imageinfo, "open", counting_open, raising=False)
    return opened


# ----------------------------------------------------------------------------------
# The public surface
# ----------------------------------------------------------------------------------


def test_image_extensions() -> None:
    assert (
        frozenset({".png", ".tga", ".dds", ".jpg", ".jpeg", ".bmp", ".ktx", ".ktx2", ".astc"})
        == imageinfo.IMAGE_EXTENSIONS
    )
    assert isinstance(imageinfo.IMAGE_EXTENSIONS, frozenset)
    assert {sample.extension for sample in SAMPLES.values()} <= imageinfo.IMAGE_EXTENSIONS


def test_image_info_defaults() -> None:
    info = ImageInfo("png", 4, 2)
    assert (info.container, info.width, info.height) == ("png", 4, 2)
    assert (info.depth, info.mip_count, info.layers) == (1, 1, 1)
    assert info.pixel_format is None
    assert info.bit_depth is None
    assert info.compressed is False
    assert info.notes == ()


def test_image_info_is_a_frozen_hashable_value() -> None:
    info = ImageInfo("png", 4, 2, notes=("palette",))
    assert info == ImageInfo("png", 4, 2, notes=("palette",))
    assert info != ImageInfo("png", 4, 2)
    assert len({info, ImageInfo("png", 4, 2, notes=("palette",))}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.width = 8  # type: ignore[misc]
    assert not hasattr(info, "__dict__")  # slots: cheap to keep tens of thousands of them


def test_as_dict_is_json_serializable() -> None:
    info = ImageInfo(
        container="ktx2",
        width=64,
        height=32,
        depth=4,
        mip_count=7,
        layers=6,
        pixel_format="VK_145",
        bit_depth=None,
        compressed=True,
        notes=("supercompressed",),
    )
    expected = {
        "container": "ktx2",
        "width": 64,
        "height": 32,
        "depth": 4,
        "mip_count": 7,
        "layers": 6,
        "pixel_format": "VK_145",
        "bit_depth": None,
        "compressed": True,
        "notes": ["supercompressed"],
    }
    assert info.as_dict() == expected
    assert json.loads(json.dumps(info.as_dict())) == expected
    assert ImageInfo("png", 1, 1).as_dict()["notes"] == []
    assert set(info.as_dict()) == {field.name for field in dataclasses.fields(ImageInfo)}


# ----------------------------------------------------------------------------------
# PNG
# ----------------------------------------------------------------------------------

PNG_CASES = [
    # colour type, bit depth, interlace -> pixel format, notes
    (0, 1, 0, "L1", ()),
    (0, 2, 0, "L2", ()),
    (0, 4, 0, "L4", ()),
    (0, 8, 0, "L8", ()),
    (0, 16, 0, "L16", ()),
    (2, 8, 0, "RGB8", ()),
    (2, 16, 0, "RGB16", ()),
    (3, 1, 0, "P1", ("palette",)),
    (3, 2, 0, "P2", ("palette",)),
    (3, 4, 0, "P4", ("palette",)),
    (3, 8, 0, "P8", ("palette",)),
    (4, 8, 0, "LA8", ()),
    (4, 16, 0, "LA16", ()),
    (6, 8, 0, "RGBA8", ()),
    (6, 16, 0, "RGBA16", ()),
    (0, 8, 1, "L8", ("interlaced",)),
    (3, 8, 1, "P8", ("interlaced", "palette")),
    (6, 16, 1, "RGBA16", ("interlaced",)),
]


@pytest.mark.parametrize(
    ("colour_type", "bit_depth", "interlace", "pixel_format", "notes"), PNG_CASES
)
def test_png_reports_what_ihdr_says(
    colour_type: int, bit_depth: int, interlace: int, pixel_format: str, notes: tuple[str, ...]
) -> None:
    data = png_file(WIDTH, HEIGHT, bit_depth, colour_type, interlace=interlace)
    # Pillow decodes the hand-made file: it is a real PNG of that size and interlacing.
    image_format, size, reported = pillow_reads(data)
    assert (image_format, size) == ("PNG", (WIDTH, HEIGHT))
    assert reported.get("interlace", 0) == interlace

    expected = ImageInfo(
        "png", WIDTH, HEIGHT, pixel_format=pixel_format, bit_depth=bit_depth, notes=notes
    )
    assert imageinfo.probe_bytes(data) == expected


def test_png_fields_are_big_endian_and_29_bytes_are_enough() -> None:
    header = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR"
        b"\x00\x00\x12\x34"  # width 0x1234
        b"\x00\x01\x00\x02"  # height 0x10002
        b"\x10\x02\x00\x00\x00"  # 16 bits, truecolour, deflate, adaptive filter, no interlace
    )
    assert len(header) == 29  # the chunk's CRC is not needed
    expected = ImageInfo("png", 0x1234, 0x10002, pixel_format="RGB16", bit_depth=16)
    assert imageinfo.probe_bytes(header) == expected


def test_png_accepts_the_largest_dimensions_the_format_allows() -> None:
    largest = 2**31 - 1
    info = imageinfo.probe_bytes(png_header(largest, largest, 8, 0))
    assert info == ImageInfo("png", largest, largest, pixel_format="L8", bit_depth=8)


@pytest.mark.parametrize("colour_type", sorted(PNG_ALLOWED_DEPTHS))
@pytest.mark.parametrize("bit_depth", [0, 1, 2, 3, 4, 8, 16, 32])
def test_png_bit_depth_must_suit_the_colour_type(colour_type: int, bit_depth: int) -> None:
    header = png_header(WIDTH, HEIGHT, bit_depth, colour_type)
    if bit_depth in PNG_ALLOWED_DEPTHS[colour_type]:
        info = imageinfo.probe_bytes(header)
        assert info is not None
        assert info.bit_depth == bit_depth
    else:
        with pytest.raises(FormatError, match=rf"bit depth \(offset 24\) is {bit_depth}\b"):
            imageinfo.probe_bytes(header)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (png_header(0, HEIGHT, 8, 6), r"width \(offset 16\) is 0"),
        (png_header(WIDTH, 0, 8, 6), r"height \(offset 20\) is 0"),
        (png_header(2**31, HEIGHT, 8, 6), r"width \(offset 16\) is 2147483648"),
        (png_header(WIDTH, 2**32 - 1, 8, 6), r"height \(offset 20\) is 4294967295"),
        (png_header(WIDTH, HEIGHT, 8, 1), r"colour type \(offset 25\) is 1"),
        (png_header(WIDTH, HEIGHT, 8, 5), r"colour type \(offset 25\) is 5"),
        (png_header(WIDTH, HEIGHT, 8, 7), r"colour type \(offset 25\) is 7"),
        (png_header(WIDTH, HEIGHT, 8, 255), r"colour type \(offset 25\) is 255"),
        (png_header(WIDTH, HEIGHT, 8, 6, compression=1), r"compression method \(offset 26\) is 1"),
        (png_header(WIDTH, HEIGHT, 8, 6, filter_method=1), r"filter method \(offset 27\) is 1"),
        (png_header(WIDTH, HEIGHT, 8, 6, interlace=2), r"interlace method \(offset 28\) is 2"),
        # Something else where IHDR must be.
        (
            PNG_SIGNATURE + png_chunk(b"IDAT", bytes(13)),
            r"first chunk .* b'IDAT', expected b'IHDR'",
        ),
        (
            PNG_SIGNATURE + png_chunk(b"CgBI", bytes(13)) + png_header(4, 4, 8, 6)[8:],
            r"first chunk .* b'CgBI'",
        ),
        # An IHDR chunk of the wrong size.
        (
            PNG_SIGNATURE + png_chunk(b"IHDR", bytes(12)) + bytes(8),
            r"IHDR chunk length \(offset 8\) is 12",
        ),
        (PNG_SIGNATURE + png_chunk(b"IHDR", bytes(14)), r"IHDR chunk length \(offset 8\) is 14"),
        # Nothing, or not enough, after the signature.
        (PNG_SIGNATURE, r"truncated: 29 bytes needed, only 8 available"),
        (png_header(WIDTH, HEIGHT, 8, 6)[:28], r"truncated: 29 bytes needed, only 28 available"),
    ],
)
def test_png_malformed_header(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("PNG: ")


#: Pillow mode -> what the PNG it writes for that mode holds.
PNG_MODES = {
    "1": "L1",
    "L": "L8",
    "LA": "LA8",
    "P": "P8",
    "RGB": "RGB8",
    "RGBA": "RGBA8",
    "I;16": "L16",
}


@pytest.mark.parametrize("mode", sorted(PNG_MODES))
def test_png_written_by_pillow(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "image.png"
    image = Image.new(mode, (WIDTH, HEIGHT))
    if mode == "P":
        # 256 distinct colours, so that Pillow keeps eight bits per index.
        image.putpalette([value for index in range(256) for value in (index, 255 - index, 7)])
    image.save(path)
    with Image.open(path) as reopened:
        size, reported_mode = reopened.size, reopened.mode

    info = imageinfo.probe(path)
    assert info is not None
    assert (info.container, info.width, info.height) == ("png", *size)
    assert info.pixel_format == PNG_MODES[reported_mode]
    assert info.bit_depth == int(PNG_MODES[reported_mode].lstrip("LAPRGB"))
    assert info.notes == (("palette",) if reported_mode == "P" else ())
    assert imageinfo.probe_bytes(path.read_bytes()) == info


@pytest.mark.parametrize("bits", [1, 2, 4, 8])
def test_png_palette_depths_written_by_pillow(tmp_path: Path, bits: int) -> None:
    # Pillow writes the smallest depth that holds the palette in use, so the image uses
    # exactly 2**bits colours; the depth byte of IHDR (offset 24) confirms what it wrote.
    path = tmp_path / "palette.png"
    colours = 1 << bits
    image = Image.new("P", (WIDTH, HEIGHT))
    image.putpalette(bytes(value % 256 for value in range(colours * 3)))
    for index in range(colours):
        image.putpixel((index % WIDTH, index // WIDTH), index)
    image.save(path, bits=bits)
    assert path.read_bytes()[24] == bits
    expected = ImageInfo(
        "png", WIDTH, HEIGHT, pixel_format=f"P{bits}", bit_depth=bits, notes=("palette",)
    )
    assert imageinfo.probe(path) == expected


# ----------------------------------------------------------------------------------
# TGA
# ----------------------------------------------------------------------------------

TGA_CASES = [
    # image type, pixel depth, descriptor -> pixel format, notes, can Pillow read it
    (1, 8, 0x00, "P8", ("bottom-up",), True),
    (9, 8, 0x20, "P8", ("rle", "top-down"), True),
    (2, 15, 0x00, "RGB5", ("bottom-up",), False),
    (2, 16, 0x01, "RGB5", ("bottom-up",), True),
    (2, 24, 0x20, "RGB8", ("top-down",), True),
    (2, 32, 0x28, "RGBA8", ("top-down",), True),
    (10, 16, 0x21, "RGB5", ("rle", "top-down"), True),
    (10, 24, 0x00, "RGB8", ("rle", "bottom-up"), True),
    (10, 32, 0x08, "RGBA8", ("rle", "bottom-up"), True),
    (3, 8, 0x00, "L8", ("bottom-up",), True),
    (3, 16, 0x08, "LA8", ("bottom-up",), True),
    (11, 8, 0x20, "L8", ("rle", "top-down"), True),
    (11, 16, 0x28, "LA8", ("rle", "top-down"), True),
    # Right-to-left (bit 4) and the two reserved bits say nothing about rows.
    (2, 24, 0x10, "RGB8", ("bottom-up",), True),
    (2, 24, 0xF0, "RGB8", ("top-down",), False),
]


@pytest.mark.parametrize(
    ("image_type", "pixel_depth", "descriptor", "pixel_format", "notes", "pillow"), TGA_CASES
)
def test_tga_reports_what_the_header_says(
    image_type: int,
    pixel_depth: int,
    descriptor: int,
    pixel_format: str,
    notes: tuple[str, ...],
    pillow: bool,
) -> None:
    data = tga_file(WIDTH, HEIGHT, image_type, pixel_depth, descriptor=descriptor)
    if pillow:
        image_format, size, reported = pillow_reads(data)
        assert (image_format, size) == ("TGA", (WIDTH, HEIGHT))
        assert ("top-down" in notes) == (reported["orientation"] > 0)
        assert ("rle" in notes) == (reported.get("compression") == "tga_rle")

    expected = ImageInfo("tga", WIDTH, HEIGHT, pixel_format=pixel_format, notes=notes)
    assert imageinfo.probe_bytes(data, extension=".tga") == expected


def test_tga_fields_are_little_endian_and_18_bytes_are_enough() -> None:
    header = bytes(
        [
            0x00,  # no image ID
            0x00,  # no colour map
            0x0A,  # run-length encoded truecolour
            *(0, 0, 0, 0, 0),  # colour map specification
            *(0, 0, 0, 0),  # origin
            *(0x00, 0x08),  # width 0x0800
            *(0x00, 0x04),  # height 0x0400
            0x20,  # 32 bits per pixel
            0x28,  # 8 alpha bits, top-to-bottom
        ]
    )
    assert len(header) == 18
    expected = ImageInfo("tga", 0x0800, 0x0400, pixel_format="RGBA8", notes=("rle", "top-down"))
    assert imageinfo.probe_bytes(header, extension=".tga") == expected


def test_tga_largest_dimensions_and_ignored_fields() -> None:
    # An image ID, a colour map in a truecolour file and an origin change nothing.
    header = struct.pack("<BBBHHBHHHHBB", 255, 1, 2, 7, 256, 32, 1000, 2000, 0xFFFF, 0xFFFF, 24, 0)
    expected = ImageInfo("tga", 0xFFFF, 0xFFFF, pixel_format="RGB8", notes=("bottom-up",))
    assert imageinfo.probe_bytes(header, extension=".tga") == expected


@pytest.mark.parametrize(
    ("image_type", "pixel_depth"),
    [(1, 16), (1, 24), (9, 32), (2, 8), (10, 8), (3, 15), (3, 24), (11, 32)],
)
def test_tga_plausible_header_with_an_unusual_layout_has_no_format_name(
    image_type: int, pixel_depth: int
) -> None:
    mapped = image_type in (1, 9)
    header = tga_header(
        WIDTH,
        HEIGHT,
        image_type,
        pixel_depth,
        colour_map_type=1 if mapped else 0,
        colour_map_entry_size=24 if mapped else 0,
    )
    info = imageinfo.probe_bytes(header, extension=".tga")
    assert info is not None
    assert (info.container, info.width, info.height) == ("tga", WIDTH, HEIGHT)
    assert info.pixel_format is None  # reported, but not given a name it does not have


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 24, colour_map_type=2), id="colour-map-type-2"),
        pytest.param(
            tga_header(WIDTH, HEIGHT, 2, 24, colour_map_type=255), id="colour-map-type-255"
        ),
        pytest.param(tga_header(WIDTH, HEIGHT, 0, 24), id="image-type-0-no-image"),
        pytest.param(tga_header(WIDTH, HEIGHT, 4, 24), id="image-type-4"),
        pytest.param(tga_header(WIDTH, HEIGHT, 8, 24), id="image-type-8"),
        pytest.param(tga_header(WIDTH, HEIGHT, 12, 24), id="image-type-12"),
        pytest.param(tga_header(WIDTH, HEIGHT, 32, 8, colour_map_type=1), id="image-type-32"),
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 0), id="pixel-depth-0"),
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 1), id="pixel-depth-1"),
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 12), id="pixel-depth-12"),
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 48), id="pixel-depth-48"),
        pytest.param(tga_header(0, HEIGHT, 2, 24), id="width-0"),
        pytest.param(tga_header(WIDTH, 0, 2, 24), id="height-0"),
        pytest.param(
            tga_header(WIDTH, HEIGHT, 1, 8, colour_map_type=0), id="colour-mapped-without-map"
        ),
        pytest.param(
            tga_header(WIDTH, HEIGHT, 9, 8, colour_map_type=0), id="rle-colour-mapped-without-map"
        ),
        pytest.param(tga_header(WIDTH, HEIGHT, 2, 24)[:17], id="one-byte-short"),
        pytest.param(b"this is a text file, not an image\n" * 4, id="text"),
        pytest.param(bytes(64), id="zeros"),
        pytest.param(b"", id="empty"),
    ],
)
def test_implausible_tga_header_is_not_a_tga(tmp_path: Path, data: bytes) -> None:
    assert imageinfo.probe_bytes(data, extension=".tga") is None
    # Not even a footer makes up for it. (Bytes appended to a header cut short would
    # complete it, so that case only checks the shortened header itself.)
    if len(data) >= 18:
        assert imageinfo.probe_bytes(data + bytes(64) + TGA_FOOTER, extension=".tga") is None
    path = tmp_path / "image.tga"
    path.write_bytes(data)
    assert imageinfo.probe(path) is None


def test_tga_needs_its_extension_or_its_footer(tmp_path: Path) -> None:
    plain = tga_file(WIDTH, HEIGHT, 2, 24)
    with_footer = tga_file(WIDTH, HEIGHT, 2, 24, footer=True)
    expected = ImageInfo("tga", WIDTH, HEIGHT, pixel_format="RGB8", notes=("bottom-up",))

    # The extension is enough, whatever its case.
    assert imageinfo.probe_bytes(plain, extension=".tga") == expected
    assert imageinfo.probe_bytes(plain, extension=".TGA") == expected
    # Without it the bytes could be anything: no signature, no footer, no TGA.
    assert imageinfo.probe_bytes(plain) is None
    assert imageinfo.probe_bytes(plain, extension=".bin") is None
    assert imageinfo.probe_bytes(plain, extension="tga") is None  # the dot is part of it
    # The TGA 2.0 footer identifies the file under any name.
    assert imageinfo.probe_bytes(with_footer) == expected
    assert imageinfo.probe_bytes(with_footer, extension=".bin") == expected

    for name, data, result in [
        ("plain.tga", plain, expected),
        ("UPPER.TGA", plain, expected),
        ("plain.bin", plain, None),
        ("plain", plain, None),
        ("footer.bin", with_footer, expected),
        ("footer", with_footer, expected),
    ]:
        path = tmp_path / name
        path.write_bytes(data)
        assert imageinfo.probe(path) == result, name


def test_tga_footer_must_be_a_whole_footer_at_the_very_end() -> None:
    header = tga_header(WIDTH, HEIGHT, 2, 24)
    signature = b"TRUEVISION-XFILE.\x00"
    # Header (18) + footer (26) is the smallest file that can end with one.
    assert imageinfo.probe_bytes(header + TGA_FOOTER) is not None
    assert imageinfo.probe_bytes(header + signature) is None  # 36 bytes: no room for a footer
    assert imageinfo.probe_bytes(header + TGA_FOOTER + b"\x00") is None  # not at the end
    assert imageinfo.probe_bytes(header + bytes(8) + signature[:-1] + b"\x01") is None
    assert imageinfo.probe_bytes(header + bytes(8) + signature.lower()) is None


#: Pillow mode -> what the TGA it writes for that mode holds.
TGA_MODES = {"L": "L8", "LA": "LA8", "P": "P8", "RGB": "RGB8", "RGBA": "RGBA8"}


@pytest.mark.parametrize("mode", sorted(TGA_MODES))
@pytest.mark.parametrize("rle", [False, True])
@pytest.mark.parametrize("orientation", [1, -1])
def test_tga_written_by_pillow(tmp_path: Path, mode: str, rle: bool, orientation: int) -> None:
    path = tmp_path / "image.tga"
    options: dict[str, Any] = {"orientation": orientation}
    if rle:
        options["compression"] = "tga_rle"
    Image.new(mode, (WIDTH, HEIGHT)).save(path, **options)
    with Image.open(path) as reopened:
        size, reported_mode, reported = reopened.size, reopened.mode, dict(reopened.info)
    assert (reported.get("compression") == "tga_rle") is rle
    assert reported["orientation"] == orientation

    notes = (("rle",) if rle else ()) + (("top-down",) if orientation > 0 else ("bottom-up",))
    expected = ImageInfo("tga", *size, pixel_format=TGA_MODES[reported_mode], notes=notes)
    assert imageinfo.probe(path) == expected
    # Pillow writes TGA 2.0: the footer identifies the file without its extension.
    assert imageinfo.probe_bytes(path.read_bytes()) == expected


# ----------------------------------------------------------------------------------
# JPEG
# ----------------------------------------------------------------------------------

JPEG_CASES = [
    # marker code, components, precision -> pixel format, notes, can Pillow read it
    (0xC0, 1, 8, "L8", (), True),
    (0xC0, 3, 8, "RGB8", (), True),
    (0xC0, 4, 8, "CMYK8", (), True),
    (0xC2, 1, 8, "L8", ("progressive",), True),
    (0xC2, 3, 8, "RGB8", ("progressive",), True),
    (0xC1, 3, 12, "RGB12", (), False),
    (0xC2, 1, 12, "L12", ("progressive",), False),
    (0xC3, 1, 16, "L16", (), False),
    (0xC3, 3, 2, "RGB2", (), False),
    # Component counts that have no conventional meaning: reported, not named.
    (0xC0, 2, 8, None, (), False),
    (0xC1, 5, 8, None, (), False),
    (0xC0, 255, 8, None, (), False),
]


@pytest.mark.parametrize(
    ("code", "components", "precision", "pixel_format", "notes", "pillow"), JPEG_CASES
)
def test_jpeg_reports_what_the_frame_header_says(
    code: int,
    components: int,
    precision: int,
    pixel_format: str | None,
    notes: tuple[str, ...],
    pillow: bool,
) -> None:
    frame = jpeg_frame(code, WIDTH, HEIGHT, components=components, precision=precision)
    data = jpeg_file(JFIF, frame)
    if pillow:
        # Pillow stops at the scan header too: the hand-made file is a JPEG to it.
        image_format, size, reported = pillow_reads(data, decode=False)
        assert (image_format, size) == ("JPEG", (WIDTH, HEIGHT))
        assert bool(reported.get("progressive")) == ("progressive" in notes)

    expected = ImageInfo(
        "jpeg", WIDTH, HEIGHT, pixel_format=pixel_format, bit_depth=precision, notes=notes
    )
    assert imageinfo.probe_bytes(data) == expected


def test_jpeg_fields_are_big_endian() -> None:
    data = (
        b"\xff\xd8"  # start of image
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0: 16 bytes
        b"\xff\xdb\x00\x43" + bytes(65) + b"\xff\xc0\x00\x11"  # DQT: 67 bytes; SOF0: 17 bytes
        b"\x08"  # 8 bits per sample
        b"\x02\xd0"  # 0x2D0 lines
        b"\x05\x00"  # 0x500 samples per line
        b"\x03"  # three components
        b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    expected = ImageInfo("jpeg", 0x500, 0x2D0, pixel_format="RGB8", bit_depth=8)
    assert imageinfo.probe_bytes(data) == expected
    assert imageinfo.probe_bytes(data[:-9]) == expected  # the component list is not needed


@pytest.mark.parametrize("code", FRAME_MARKERS)
def test_jpeg_every_start_of_frame_marker_is_a_frame_header(code: int) -> None:
    data = jpeg_file(JFIF, jpeg_frame(code, 0xFFFF, 1))
    notes = ("progressive",) if code in PROGRESSIVE_MARKERS else ()
    expected = ImageInfo("jpeg", 0xFFFF, 1, pixel_format="RGB8", bit_depth=8, notes=notes)
    assert imageinfo.probe_bytes(data) == expected


def test_jpeg_segments_are_skipped_by_length_not_by_content() -> None:
    # A payload that looks like a frame header, a scan and an end of image.
    decoy = jpeg_frame(0xC0, 11, 22) + SOS + EOI
    data = jpeg_file(
        JFIF,
        jpeg_segment(0xE1, b"Exif\x00\x00" + decoy),
        jpeg_segment(0xFE, decoy),  # comment
        jpeg_segment(0xDB, bytes(65)),  # quantization table
        jpeg_segment(0xC4, decoy),  # Huffman tables: C4 is not a frame header
        jpeg_segment(0xCC, decoy),  # arithmetic conditioning: neither is CC
        jpeg_segment(0xC8, decoy),  # reserved: nor C8
        jpeg_segment(0xDD, b"\x00\x08"),  # restart interval
        jpeg_segment(0x4F, decoy),  # a reserved code is still a segment with a length
        jpeg_frame(0xC1, WIDTH, HEIGHT, components=1, precision=12),
    )
    expected = ImageInfo("jpeg", WIDTH, HEIGHT, pixel_format="L12", bit_depth=12)
    assert imageinfo.probe_bytes(data) == expected


def test_jpeg_markers_without_a_length() -> None:
    data = (
        SOI
        + b"\xff\x01"  # TEM
        + b"\xff\xd0\xff\xd7"  # RST0, RST7
        + b"\xff\xd8"  # another SOI
        + JFIF
        + jpeg_frame(0xC0, WIDTH, HEIGHT)
        + SOS
        + EOI
    )
    expected = ImageInfo("jpeg", WIDTH, HEIGHT, pixel_format="RGB8", bit_depth=8)
    assert imageinfo.probe_bytes(data) == expected


@pytest.mark.parametrize("fill", [1, 2, 3, 4093, 4094, 4095, 4096, 4097, 8192, 20_000])
def test_jpeg_any_number_of_fill_bytes_may_precede_a_marker(fill: int) -> None:
    frame = jpeg_frame(0xC2, WIDTH, HEIGHT, components=1)
    expected = ImageInfo(
        "jpeg", WIDTH, HEIGHT, pixel_format="L8", bit_depth=8, notes=("progressive",)
    )
    assert imageinfo.probe_bytes(SOI + b"\xff" * fill + frame) == expected
    assert imageinfo.probe_bytes(SOI + JFIF + b"\xff" * fill + frame + SOS + EOI) == expected


def test_jpeg_with_fill_bytes_is_a_jpeg_to_pillow_too() -> None:
    data = SOI + JFIF + b"\xff\xff\xff" + jpeg_frame(0xC0, WIDTH, HEIGHT) + SOS + EOI
    assert pillow_reads(data, decode=False)[:2] == ("JPEG", (WIDTH, HEIGHT))
    expected = ImageInfo("jpeg", WIDTH, HEIGHT, pixel_format="RGB8", bit_depth=8)
    assert imageinfo.probe_bytes(data) == expected


def test_jpeg_walk_is_bounded() -> None:
    empty = jpeg_segment(0xE0)  # FF E0 00 02: the shortest segment there is
    frame = jpeg_frame(0xC0, WIDTH, HEIGHT)
    expected = ImageInfo("jpeg", WIDTH, HEIGHT, pixel_format="RGB8", bit_depth=8)
    # The frame header is the 65536th segment: still found.
    assert imageinfo.probe_bytes(SOI + empty * 65_535 + frame) == expected
    # One more in front of it and the walk gives up instead of crawling through the file.
    with pytest.raises(FormatError, match=r"no frame header .* first 65536 marker segments"):
        imageinfo.probe_bytes(SOI + empty * 65_536 + frame)


def _frame_fields(precision: int, height: int, width: int, components: int) -> bytes:
    return jpeg_segment(0xC0, struct.pack(">BHHB", precision, height, width, components))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (SOI, r"ends before any frame header .* expected a marker at offset 2"),
        (SOI + b"\xff", r"ends before any frame header .* expected a marker at offset 2"),
        (SOI + JFIF, r"ends before any frame header .* expected a marker at offset 20"),
        (SOI + b"\xff" * 5000, r"ends before any frame header .* expected a marker at offset 2"),
        (
            SOI + b"\xff\xe0",
            r"ends before any frame header .* expected a segment length at offset 4",
        ),
        (
            SOI + b"\xff\xe0\x00",
            r"ends before any frame header .* expected a segment length at offset 4",
        ),
        # A segment that claims more bytes than the data holds.
        (SOI + b"\xff\xe0\xff\xff" + bytes(10), r"ends before any frame header .* offset 65539"),
        (SOI + b"\x00\x10JFIF", r"expected a marker \(FF\) at offset 2, found 00"),
        (SOI + JFIF + b"\x12\x34", r"expected a marker \(FF\) at offset 20, found 12"),
        (SOI + b"\xff\x00" + JFIF, r"FF 00 at offset 2 is not a marker"),
        (SOI + JFIF + b"\xff\xff\xff\x00", r"FF 00 at offset 22 is not a marker"),
        (SOI + b"\xff" * 5000 + b"\x00", r"FF 00 at offset 5001 is not a marker"),
        (SOI + SOS, r"start of scan \(SOS\) at offset 2 before any frame header"),
        (
            SOI + JFIF + jpeg_segment(0xDB, bytes(65)) + SOS,
            r"start of scan \(SOS\) at offset 89 before any frame header",
        ),
        (SOI + EOI, r"end of image \(EOI\) at offset 2 before any frame header"),
        (SOI + JFIF + EOI, r"end of image \(EOI\) at offset 20 before any frame header"),
        (SOI + b"\xff\xe0\x00\x00", r"segment FF E0 at offset 2 has length 0"),
        (SOI + b"\xff\xfe\x00\x01", r"segment FF FE at offset 2 has length 1"),
        (SOI + b"\xff\xc0\x00\x00" + bytes(6), r"segment FF C0 at offset 2 has length 0"),
        (SOI + b"\xff\xc0\x00\x11\x08", r"frame header FF C0 at offset 2 is truncated"),
        (
            SOI + b"\xff\xc2\x00\x11\x08\x00\x10\x00\x10",
            r"frame header FF C2 at offset 2 is truncated",
        ),
        (SOI + b"\xff\xc0\x00\x07" + bytes(6), r"frame header FF C0 at offset 2 has length 7"),
        (SOI + _frame_fields(0, 16, 16, 3), r"sample precision of 0 bits"),
        (SOI + _frame_fields(1, 16, 16, 3), r"sample precision of 1 bits"),
        (SOI + _frame_fields(17, 16, 16, 3), r"sample precision of 17 bits"),
        (SOI + _frame_fields(8, 16, 0, 3), r"declares a width of 0"),
        (SOI + _frame_fields(8, 0, 16, 3), r"declares 0 lines"),
        (SOI + _frame_fields(8, 16, 16, 0), r"declares 0 components"),
    ],
)
def test_jpeg_malformed(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("JPEG: ")


#: Pillow mode -> what the JPEG it writes for that mode holds.
JPEG_MODES = {"L": "L8", "RGB": "RGB8", "CMYK": "CMYK8"}


@pytest.mark.parametrize("mode", sorted(JPEG_MODES))
@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_written_by_pillow(tmp_path: Path, mode: str, progressive: bool) -> None:
    path = tmp_path / "image.jpg"
    Image.new(mode, (WIDTH, HEIGHT)).save(path, progressive=progressive)
    with Image.open(path) as reopened:
        size, reported_mode, reported = reopened.size, reopened.mode, dict(reopened.info)
    assert bool(reported.get("progressive")) is progressive

    expected = ImageInfo(
        "jpeg",
        *size,
        pixel_format=JPEG_MODES[reported_mode],
        bit_depth=8,
        notes=("progressive",) if progressive else (),
    )
    assert imageinfo.probe(path) == expected
    assert imageinfo.probe_bytes(path.read_bytes()) == expected


def test_jpeg_written_by_pillow_with_a_large_colour_profile(tmp_path: Path) -> None:
    path = tmp_path / "profile.jpg"
    # Pillow splits the profile over several 64 KiB APP2 segments, ahead of the frame.
    Image.new("RGB", (WIDTH, HEIGHT)).save(path, icc_profile=bytes(300_000), comment=b"x" * 999)
    data = path.read_bytes()
    assert data.index(b"\xff\xc0\x00\x11\x08") > 300_000  # the frame header is far inside
    with Image.open(path) as reopened:
        assert reopened.size == (WIDTH, HEIGHT)

    expected = ImageInfo("jpeg", WIDTH, HEIGHT, pixel_format="RGB8", bit_depth=8)
    assert imageinfo.probe(path) == expected
    assert imageinfo.probe_bytes(data) == expected


def test_jpeg_behind_megabytes_of_metadata_costs_a_few_small_reads(
    tmp_path: Path, opened_files: list[CountingFile]
) -> None:
    largest_segment = jpeg_segment(0xE1, bytes(65_533))  # a length of 65535: the maximum
    metadata = largest_segment * 40  # 2.6 MB ahead of the frame header
    garbage = bytes(range(256)) * 8192  # 2 MB behind it that nothing should look at
    data = SOI + metadata + jpeg_frame(0xC0, 4096, 2160) + garbage
    assert len(data) > 4_500_000
    path = tmp_path / "big.jpg"
    path.write_bytes(data)

    expected = ImageInfo("jpeg", 4096, 2160, pixel_format="RGB8", bit_depth=8)
    assert imageinfo.probe(path) == expected
    (raw,) = opened_files
    assert raw.closed
    # 41 hops of a few bytes each: a small fraction of the file, however it is buffered.
    assert 0 < raw.bytes_read < len(data) // 8
    assert imageinfo.probe_bytes(data) == expected


# ----------------------------------------------------------------------------------
# BMP
# ----------------------------------------------------------------------------------

BMP_FORMATS = {1: "P1", 2: "P2", 4: "P4", 8: "P8", 16: "RGB5", 24: "RGB8", 32: "RGBA8"}


@pytest.mark.parametrize("bits", sorted(BMP_FORMATS))
@pytest.mark.parametrize("dib_size", [16, 40, 52, 56, 64, 108, 124])
def test_bmp_info_header(bits: int, dib_size: int) -> None:
    data = bmp_info_file(WIDTH, HEIGHT, bits, dib_size=dib_size)
    # Pillow knows these header sizes and depths; the others are documented all the same.
    if dib_size != 16 and bits != 2:
        assert pillow_reads(data)[:2] == ("BMP", (WIDTH, HEIGHT))

    expected = ImageInfo("bmp", WIDTH, HEIGHT, pixel_format=BMP_FORMATS[bits])
    assert imageinfo.probe_bytes(data) == expected


@pytest.mark.parametrize("bits", [1, 4, 8, 24])
def test_bmp_core_header(bits: int) -> None:
    data = bmp_core_file(WIDTH, HEIGHT, bits)
    assert pillow_reads(data)[:2] == ("BMP", (WIDTH, HEIGHT))
    expected = ImageInfo("bmp", WIDTH, HEIGHT, pixel_format=BMP_FORMATS[bits])
    assert imageinfo.probe_bytes(data) == expected


def test_bmp_core_header_dimensions_are_unsigned_16_bit() -> None:
    # 0xFFFF would be -1 if the fields were read as signed.
    data = b"BM" + bytes(12) + struct.pack("<IHHHH", 12, 0xFFFF, 0x8000, 1, 24)
    assert len(data) == 26
    expected = ImageInfo("bmp", 0xFFFF, 0x8000, pixel_format="RGB8")
    assert imageinfo.probe_bytes(data) == expected


def test_bmp_negative_height_is_a_top_down_bitmap() -> None:
    header = (
        b"BM"
        + bytes(12)  # file size, reserved, offset of the pixels: not needed
        + b"\x28\x00\x00\x00"  # BITMAPINFOHEADER: 40 bytes
        + b"\x00\x05\x00\x00"  # width 0x500
        + b"\x30\xfd\xff\xff"  # height -0x2D0
        + b"\x01\x00"  # one plane
        + b"\x20\x00"  # 32 bits per pixel
    )
    assert len(header) == 30
    expected = ImageInfo("bmp", 0x500, 0x2D0, pixel_format="RGBA8", notes=("top-down",))
    assert imageinfo.probe_bytes(header) == expected

    data = bmp_info_file(WIDTH, -HEIGHT, 24)
    assert pillow_reads(data)[:2] == ("BMP", (WIDTH, HEIGHT))
    expected = ImageInfo("bmp", WIDTH, HEIGHT, pixel_format="RGB8", notes=("top-down",))
    assert imageinfo.probe_bytes(data) == expected


def test_bmp_extreme_dimensions() -> None:
    def header(width: int, height: int) -> bytes:
        return b"BM" + bytes(12) + struct.pack("<IiiHH", 40, width, height, 1, 24)

    largest = 2**31 - 1
    assert imageinfo.probe_bytes(header(largest, largest)) == ImageInfo(
        "bmp", largest, largest, pixel_format="RGB8"
    )
    assert imageinfo.probe_bytes(header(1, -(2**31))) == ImageInfo(
        "bmp", 1, 2**31, pixel_format="RGB8", notes=("top-down",)
    )


def _bmp_header(dib_size: int, width: int, height: int, bits: int) -> bytes:
    return b"BM" + bytes(12) + struct.pack("<IiiHH", dib_size, width, height, 1, bits)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"BM", r"truncated: 18 bytes needed, only 2 available"),
        (b"BM" + bytes(15), r"truncated: 18 bytes needed, only 17 available"),
        (_bmp_header(40, WIDTH, HEIGHT, 24)[:29], r"truncated: 30 bytes needed, only 29 available"),
        (bmp_core_file(WIDTH, HEIGHT, 24)[:25], r"truncated: 26 bytes needed, only 25 available"),
        (
            _bmp_header(0, WIDTH, HEIGHT, 24),
            r"DIB header size \(offset 14\) is 0, expected 12 or 16 to 124",
        ),
        (_bmp_header(11, WIDTH, HEIGHT, 24), r"DIB header size \(offset 14\) is 11"),
        (_bmp_header(13, WIDTH, HEIGHT, 24), r"DIB header size \(offset 14\) is 13"),
        (_bmp_header(15, WIDTH, HEIGHT, 24), r"DIB header size \(offset 14\) is 15"),
        (_bmp_header(125, WIDTH, HEIGHT, 24), r"DIB header size \(offset 14\) is 125"),
        (_bmp_header(2**32 - 1, WIDTH, HEIGHT, 24), r"DIB header size \(offset 14\) is 4294967295"),
        (b"BMW and other car makers: a text that merely starts like a bitmap", r"DIB header size"),
        (_bmp_header(40, 0, HEIGHT, 24), r"width \(offset 18\) is 0"),
        (_bmp_header(40, -WIDTH, HEIGHT, 24), r"width \(offset 18\) is -301"),
        (_bmp_header(40, WIDTH, 0, 24), r"height \(offset 22\) is 0"),
        (
            b"BM" + bytes(12) + struct.pack("<IHHHH", 12, 0, HEIGHT, 1, 24),
            r"width \(offset 18\) is 0",
        ),
        (
            b"BM" + bytes(12) + struct.pack("<IHHHH", 12, WIDTH, 0, 1, 24),
            r"height \(offset 20\) is 0",
        ),
        (_bmp_header(40, WIDTH, HEIGHT, 0), r"bits per pixel \(offset 28\) is 0"),
        (_bmp_header(40, WIDTH, HEIGHT, 3), r"bits per pixel \(offset 28\) is 3"),
        (_bmp_header(40, WIDTH, HEIGHT, 15), r"bits per pixel \(offset 28\) is 15"),
        (_bmp_header(40, WIDTH, HEIGHT, 48), r"bits per pixel \(offset 28\) is 48"),
        (_bmp_header(124, WIDTH, HEIGHT, 64), r"bits per pixel \(offset 28\) is 64"),
        (
            b"BM" + bytes(12) + struct.pack("<IHHHH", 12, WIDTH, HEIGHT, 1, 12),
            r"bits per pixel \(offset 24\) is 12",
        ),
    ],
)
def test_bmp_malformed(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("BMP: ")


#: Pillow mode -> what the BMP it writes for that mode holds, and the modes Pillow may
#: report when it reads that file back (it tells neither palettes nor alpha apart).
BMP_MODES = {
    "1": ("P1", {"1", "P"}),
    "L": ("P8", {"L", "P"}),
    "P": ("P8", {"L", "P"}),
    "RGB": ("RGB8", {"RGB"}),
    "RGBA": ("RGBA8", {"RGB", "RGBA"}),
}


@pytest.mark.parametrize("mode", sorted(BMP_MODES))
def test_bmp_written_by_pillow(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "image.bmp"
    image = Image.new(mode, (WIDTH, HEIGHT))
    if mode == "P":
        image.putpalette([value for index in range(256) for value in (index, 255 - index, 7)])
    image.save(path)
    with Image.open(path) as reopened:
        size, reported_mode = reopened.size, reopened.mode

    pixel_format, compatible_modes = BMP_MODES[mode]
    assert reported_mode in compatible_modes
    expected = ImageInfo("bmp", *size, pixel_format=pixel_format)
    assert imageinfo.probe(path) == expected
    assert imageinfo.probe_bytes(path.read_bytes()) == expected


# ----------------------------------------------------------------------------------
# KTX 1
# ----------------------------------------------------------------------------------

KTX1_CASES: list[tuple[dict[str, int], ImageInfo]] = [
    (
        {"width": 256, "height": 128, "mip_levels": 9},
        ImageInfo("ktx", 256, 128, mip_count=9, pixel_format="GL_0x8058"),
    ),
    # A 1D texture whose mip chain is generated at load time: zeros mean "one".
    (
        {"width": 256, "height": 0, "mip_levels": 0},
        ImageInfo("ktx", 256, 1, pixel_format="GL_0x8058"),
    ),
    (
        {"width": 64, "height": 48, "depth": 32},
        ImageInfo("ktx", 64, 48, depth=32, pixel_format="GL_0x8058"),
    ),
    (
        {"width": 64, "height": 48, "array_elements": 5},
        ImageInfo("ktx", 64, 48, layers=5, pixel_format="GL_0x8058"),
    ),
    (
        {"width": 64, "height": 64, "faces": 6},
        ImageInfo("ktx", 64, 64, layers=6, pixel_format="GL_0x8058"),
    ),
    (
        {"width": 64, "height": 64, "faces": 6, "array_elements": 3, "mip_levels": 7},
        ImageInfo("ktx", 64, 64, mip_count=7, layers=18, pixel_format="GL_0x8058"),
    ),
    # glType 0 is what marks compressed data (here GL_COMPRESSED_RGBA_S3TC_DXT1_EXT).
    (
        {"width": 1024, "height": 512, "gl_type": 0, "gl_internal_format": 0x83F1},
        ImageInfo("ktx", 1024, 512, pixel_format="GL_0x83F1", compressed=True),
    ),
    # A small number is padded to four hex digits, a large one is not cut.
    (
        {"width": 8, "height": 8, "gl_internal_format": 0x4},
        ImageInfo("ktx", 8, 8, pixel_format="GL_0x0004"),
    ),
    (
        {"width": 8, "height": 8, "gl_internal_format": 0xABCDEF01},
        ImageInfo("ktx", 8, 8, pixel_format="GL_0xABCDEF01"),
    ),
    # The largest values the fields hold are reported as they are.
    (
        {
            "width": 2**32 - 1,
            "height": 2**32 - 1,
            "depth": 2**32 - 1,
            "array_elements": 2**32 - 1,
            "faces": 6,
            "mip_levels": 2**32 - 1,
        },
        ImageInfo(
            "ktx",
            2**32 - 1,
            2**32 - 1,
            depth=2**32 - 1,
            mip_count=2**32 - 1,
            layers=(2**32 - 1) * 6,
            pixel_format="GL_0x8058",
        ),
    ),
]


@pytest.mark.parametrize(("fields", "expected"), KTX1_CASES)
@pytest.mark.parametrize("order", ["<", ">"])
def test_ktx1_in_both_byte_orders(fields: dict[str, int], expected: ImageInfo, order: str) -> None:
    data = ktx1_header(order=order, **fields)  # type: ignore[arg-type]
    assert len(data) == 64
    notes = ("big-endian",) if order == ">" else ()
    assert imageinfo.probe_bytes(data) == dataclasses.replace(expected, notes=notes)
    # The last header field (the size of the key/value data) is not needed.
    assert imageinfo.probe_bytes(data[:60]) == dataclasses.replace(expected, notes=notes)


def test_ktx1_endianness_field_is_the_byte_order_of_the_file() -> None:
    little = KTX1_IDENTIFIER + b"\x01\x02\x03\x04" + struct.pack("<11I", *range(1, 12))
    big = KTX1_IDENTIFIER + b"\x04\x03\x02\x01" + struct.pack(">11I", *range(1, 12))
    # Fields 1..11: glType, glTypeSize, glFormat, glInternalFormat, glBaseInternalFormat,
    # width 6, height 7, depth 8, array elements 9, faces 10, mip levels 11.
    for data in (little, big):
        with pytest.raises(FormatError, match=r"numberOfFaces \(offset 52\) is 10"):
            imageinfo.probe_bytes(data)
    fields = [1, 2, 3, 0x1234, 5, 6, 7, 8, 9, 6, 11]
    little = KTX1_IDENTIFIER + b"\x01\x02\x03\x04" + struct.pack("<11I", *fields)
    big = KTX1_IDENTIFIER + b"\x04\x03\x02\x01" + struct.pack(">11I", *fields)
    expected = ImageInfo("ktx", 6, 7, depth=8, mip_count=11, layers=9 * 6, pixel_format="GL_0x1234")
    assert imageinfo.probe_bytes(little) == expected
    assert imageinfo.probe_bytes(big) == dataclasses.replace(expected, notes=("big-endian",))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (KTX1_IDENTIFIER, r"truncated: 60 bytes needed, only 12 available"),
        (ktx1_header(64, 64)[:59], r"truncated: 60 bytes needed, only 59 available"),
        (ktx1_header(64, 64, endianness=0), r"endianness \(offset 12\) is 0x00000000"),
        (ktx1_header(64, 64, endianness=0x04030200), r"endianness \(offset 12\) is 0x04030200"),
        (ktx1_header(64, 64, endianness=0x02010403), r"endianness \(offset 12\) is 0x02010403"),
        (ktx1_header(0, 64), r"pixelWidth \(offset 36\) is 0"),
        (ktx1_header(0, 64, order=">"), r"pixelWidth \(offset 36\) is 0"),
        (ktx1_header(64, 64, faces=0), r"numberOfFaces \(offset 52\) is 0"),
        (ktx1_header(64, 64, faces=2), r"numberOfFaces \(offset 52\) is 2"),
        (ktx1_header(64, 64, faces=7), r"numberOfFaces \(offset 52\) is 7"),
        (ktx1_header(64, 64, faces=5, order=">"), r"numberOfFaces \(offset 52\) is 5"),
    ],
)
def test_ktx1_malformed(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("KTX: ")


# ----------------------------------------------------------------------------------
# KTX 2
# ----------------------------------------------------------------------------------

KTX2_CASES: list[tuple[dict[str, int], ImageInfo]] = [
    (
        {"width": 256, "height": 128, "level_count": 9},
        ImageInfo("ktx2", 256, 128, mip_count=9, pixel_format="VK_37"),
    ),
    # Zeros mean "one": a 1D texture, not an array, only the base level.
    (
        {"width": 256, "height": 0, "level_count": 0},
        ImageInfo("ktx2", 256, 1, pixel_format="VK_37"),
    ),
    (
        {"width": 64, "height": 48, "depth": 32},
        ImageInfo("ktx2", 64, 48, depth=32, pixel_format="VK_37"),
    ),
    (
        {"width": 64, "height": 48, "layer_count": 5},
        ImageInfo("ktx2", 64, 48, layers=5, pixel_format="VK_37"),
    ),
    (
        {"width": 64, "height": 64, "face_count": 6},
        ImageInfo("ktx2", 64, 64, layers=6, pixel_format="VK_37"),
    ),
    (
        {"width": 64, "height": 64, "face_count": 6, "layer_count": 3, "level_count": 7},
        ImageInfo("ktx2", 64, 64, mip_count=7, layers=18, pixel_format="VK_37"),
    ),
    # Zstandard (2) over plain texels: supercompressed, yet not block-compressed.
    (
        {"width": 64, "height": 64, "supercompression": 2},
        ImageInfo("ktx2", 64, 64, pixel_format="VK_37", notes=("supercompressed",)),
    ),
    (
        {"width": 64, "height": 64, "supercompression": 0x10000},
        ImageInfo("ktx2", 64, 64, pixel_format="VK_37", notes=("supercompressed",)),
    ),
    (
        {
            "width": 2**32 - 1,
            "height": 2**32 - 1,
            "depth": 2**32 - 1,
            "layer_count": 2**32 - 1,
            "face_count": 6,
            "level_count": 2**32 - 1,
            "vk_format": 2**32 - 1,
        },
        ImageInfo(
            "ktx2",
            2**32 - 1,
            2**32 - 1,
            depth=2**32 - 1,
            mip_count=2**32 - 1,
            layers=(2**32 - 1) * 6,
            pixel_format="VK_4294967295",
        ),
    ),
]


@pytest.mark.parametrize(("fields", "expected"), KTX2_CASES)
def test_ktx2_reports_what_the_header_says(fields: dict[str, int], expected: ImageInfo) -> None:
    data = ktx2_header(**fields)
    assert len(data) == 80
    assert imageinfo.probe_bytes(data) == expected
    assert imageinfo.probe_bytes(data[:48]) == expected  # the index is not needed


def test_ktx2_fields_are_little_endian() -> None:
    data = KTX2_IDENTIFIER + (
        b"\x91\x00\x00\x00"  # vkFormat 145
        b"\x01\x00\x00\x00"  # typeSize
        b"\x00\x08\x00\x00"  # pixelWidth 0x800
        b"\x00\x04\x00\x00"  # pixelHeight 0x400
        b"\x00\x00\x00\x00"  # pixelDepth
        b"\x00\x00\x00\x00"  # layerCount
        b"\x01\x00\x00\x00"  # faceCount
        b"\x0c\x00\x00\x00"  # levelCount 12
        b"\x00\x00\x00\x00"  # supercompressionScheme
    )
    expected = ImageInfo("ktx2", 0x800, 0x400, mip_count=12, pixel_format="VK_145", compressed=True)
    assert imageinfo.probe_bytes(data) == expected


@pytest.mark.parametrize(
    ("vk_format", "supercompression", "compressed"),
    [
        (37, 0, False),  # R8G8B8A8_UNORM
        (130, 0, False),  # D32_SFLOAT_S8_UINT, the last format before the blocks
        (131, 0, True),  # BC1_RGB_UNORM_BLOCK
        (146, 0, True),  # BC7_SRGB_BLOCK
        (147, 0, True),  # ETC2_R8G8B8_UNORM_BLOCK
        (156, 0, True),  # EAC_R11G11_SNORM_BLOCK
        (157, 0, True),  # ASTC_4x4_UNORM_BLOCK
        (184, 0, True),  # ASTC_12x12_SRGB_BLOCK
        (185, 0, False),
        (1000054000, 0, True),  # PVRTC1_2BPP_UNORM_BLOCK_IMG
        (1000054007, 0, True),  # PVRTC2_4BPP_SRGB_BLOCK_IMG
        (1000054008, 0, False),
        (1000065999, 0, False),
        (1000066000, 0, True),  # ASTC_4x4_SFLOAT_BLOCK
        (1000066013, 0, True),  # ASTC_12x12_SFLOAT_BLOCK
        (1000066014, 0, False),
        (1000288000, 0, True),  # ASTC_3x3x3_UNORM_BLOCK_EXT
        (1000288029, 0, True),  # ASTC_6x6x6_SFLOAT_BLOCK_EXT
        (1000288030, 0, False),
        (145, 2, True),  # BC7 under Zstandard is still BC7
        (0, 0, False),  # VK_FORMAT_UNDEFINED: this header alone cannot tell
        (0, 2, False),
        (0, 1, True),  # ... except under BasisLZ, which only exists for ETC1S blocks
    ],
)
def test_ktx2_block_compressed_formats(
    vk_format: int, supercompression: int, compressed: bool
) -> None:
    data = ktx2_header(64, 64, vk_format=vk_format, supercompression=supercompression)
    info = imageinfo.probe_bytes(data)
    assert info is not None
    assert info.pixel_format == f"VK_{vk_format}"
    assert info.compressed is compressed
    assert info.notes == (("supercompressed",) if supercompression else ())


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (KTX2_IDENTIFIER, r"truncated: 48 bytes needed, only 12 available"),
        (ktx2_header(64, 64)[:47], r"truncated: 48 bytes needed, only 47 available"),
        (ktx2_header(0, 64), r"pixelWidth \(offset 20\) is 0"),
        (ktx2_header(64, 64, face_count=0), r"faceCount \(offset 36\) is 0"),
        (ktx2_header(64, 64, face_count=2), r"faceCount \(offset 36\) is 2"),
        (ktx2_header(64, 64, face_count=7), r"faceCount \(offset 36\) is 7"),
    ],
)
def test_ktx2_malformed(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("KTX2: ")


# ----------------------------------------------------------------------------------
# ASTC
# ----------------------------------------------------------------------------------


def test_astc_dimensions_are_24_bit_little_endian() -> None:
    header = bytes(
        [
            *(0x13, 0xAB, 0xA1, 0x5C),  # magic
            *(6, 5, 1),  # 6x5 blocks, flat
            *(0x00, 0x08, 0x00),  # width 0x000800
            *(0x56, 0x34, 0x12),  # height 0x123456
            *(0x01, 0x00, 0x00),  # depth 1
        ]
    )
    assert len(header) == 16
    expected = ImageInfo("astc", 0x800, 0x123456, pixel_format="ASTC_6x5", compressed=True)
    assert imageinfo.probe_bytes(header) == expected
    assert imageinfo.probe_bytes(header + bytes(1000)) == expected


@pytest.mark.parametrize(
    ("block", "size", "expected"),
    [
        ((4, 4, 1), (1, 1, 1), ImageInfo("astc", 1, 1, pixel_format="ASTC_4x4", compressed=True)),
        (
            (12, 10, 1),
            (WIDTH, HEIGHT, 1),
            ImageInfo("astc", WIDTH, HEIGHT, pixel_format="ASTC_12x10", compressed=True),
        ),
        # The largest size the three-byte fields hold.
        (
            (8, 8, 1),
            (0xFFFFFF, 0xFFFFFF, 1),
            ImageInfo("astc", 0xFFFFFF, 0xFFFFFF, pixel_format="ASTC_8x8", compressed=True),
        ),
        # A volume texture, with flat blocks and with 3D blocks.
        (
            (6, 6, 1),
            (64, 32, 16),
            ImageInfo("astc", 64, 32, depth=16, pixel_format="ASTC_6x6", compressed=True),
        ),
        (
            (4, 4, 4),
            (64, 32, 16),
            ImageInfo("astc", 64, 32, depth=16, pixel_format="ASTC_4x4x4", compressed=True),
        ),
        (
            (255, 255, 255),
            (2, 3, 4),
            ImageInfo("astc", 2, 3, depth=4, pixel_format="ASTC_255x255x255", compressed=True),
        ),
    ],
)
def test_astc_reports_what_the_header_says(
    block: tuple[int, int, int], size: tuple[int, int, int], expected: ImageInfo
) -> None:
    assert imageinfo.probe_bytes(astc_header(block, size)) == expected


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (astc_header((8, 8, 1), (64, 64, 1))[:4], r"truncated: 16 bytes needed, only 4 available"),
        (
            astc_header((8, 8, 1), (64, 64, 1))[:15],
            r"truncated: 16 bytes needed, only 15 available",
        ),
        (astc_header((0, 8, 1), (64, 64, 1)), r"block dimension x \(offset 4\) is 0"),
        (astc_header((8, 0, 1), (64, 64, 1)), r"block dimension y \(offset 5\) is 0"),
        (astc_header((8, 8, 0), (64, 64, 1)), r"block dimension z \(offset 6\) is 0"),
        (astc_header((8, 8, 1), (0, 64, 1)), r"image dimension x \(offset 7\) is 0"),
        (astc_header((8, 8, 1), (64, 0, 1)), r"image dimension y \(offset 10\) is 0"),
        (astc_header((8, 8, 1), (64, 64, 0)), r"image dimension z \(offset 13\) is 0"),
    ],
)
def test_astc_malformed(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message) as error:
        imageinfo.probe_bytes(data)
    assert str(error.value).startswith("ASTC: ")


# ----------------------------------------------------------------------------------
# DDS (delegated to bea.formats.dds)
# ----------------------------------------------------------------------------------


def dds_like(**overrides: Any) -> types.SimpleNamespace:
    """What bea.formats.dds.parse_header returns, as far as the probe is concerned."""
    fields: dict[str, Any] = {
        "width": 64,
        "height": 32,
        "depth": 1,
        "mip_count": 1,
        "layers": 1,
        "is_cubemap": False,
        "is_volume": False,
        "array_size": 1,
        "format": types.SimpleNamespace(name="BC7_UNORM", compressed=True),
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def install_dds_parser(monkeypatch: pytest.MonkeyPatch, result: object) -> list[bytes]:
    """Stand in for bea.formats.dds; return the list of what parse_header gets called with."""
    calls: list[bytes] = []

    def parse_header(data: bytes) -> object:
        calls.append(bytes(data))
        if isinstance(result, Exception):
            raise result
        return result

    module = types.ModuleType("bea.formats.dds")
    module.parse_header = parse_header  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bea.formats.dds", module)
    return calls


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (dds_like(), ImageInfo("dds", 64, 32, pixel_format="BC7_UNORM", compressed=True)),
        (
            dds_like(width=4096, height=2048, mip_count=13),
            ImageInfo("dds", 4096, 2048, mip_count=13, pixel_format="BC7_UNORM", compressed=True),
        ),
        (
            dds_like(format=types.SimpleNamespace(name="R8G8B8A8_UNORM", compressed=False)),
            ImageInfo("dds", 64, 32, pixel_format="R8G8B8A8_UNORM"),
        ),
        (
            dds_like(is_cubemap=True, layers=6),
            ImageInfo(
                "dds",
                64,
                32,
                layers=6,
                pixel_format="BC7_UNORM",
                compressed=True,
                notes=("cubemap",),
            ),
        ),
        (
            dds_like(is_volume=True, depth=16),
            ImageInfo(
                "dds",
                64,
                32,
                depth=16,
                pixel_format="BC7_UNORM",
                compressed=True,
                notes=("volume",),
            ),
        ),
        (
            dds_like(array_size=4, layers=4),
            ImageInfo(
                "dds", 64, 32, layers=4, pixel_format="BC7_UNORM", compressed=True, notes=("array",)
            ),
        ),
        (
            dds_like(is_cubemap=True, array_size=2, layers=12),
            ImageInfo(
                "dds",
                64,
                32,
                layers=12,
                pixel_format="BC7_UNORM",
                compressed=True,
                notes=("cubemap", "array"),
            ),
        ),
    ],
)
def test_dds_header_is_mapped_field_by_field(
    monkeypatch: pytest.MonkeyPatch, header: types.SimpleNamespace, expected: ImageInfo
) -> None:
    calls = install_dds_parser(monkeypatch, header)
    data = b"DDS " + bytes(range(256))
    assert imageinfo.probe_bytes(data, extension=".dds") == expected
    # The parser gets the 148 bytes that hold the longest header, not the whole file.
    assert calls == [data[:148]]


def test_dds_shorter_than_its_longest_header_is_passed_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_dds_parser(monkeypatch, dds_like())
    data = b"DDS " + bytes(124)
    path = tmp_path / "legacy.dds"
    path.write_bytes(data)
    assert imageinfo.probe(path) == imageinfo.probe_bytes(data)
    assert calls == [data, data]


def test_dds_parser_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_dds_parser(monkeypatch, FormatError("DDS: truncated header"))
    with pytest.raises(FormatError, match="DDS: truncated header"):
        imageinfo.probe_bytes(b"DDS " + bytes(16))


#: The bytes of a BC1 texture of 64x32 with its full mip chain: 8 bytes per 4x4 block, for
#: 64x32, 32x16, 16x8, 8x4, 4x2, 2x1 and 1x1.
DDS_BC1_CHAIN_BYTES = (128 + 32 + 8 + 2 + 1 + 1 + 1) * 8


def test_dds_with_the_real_parser() -> None:
    dds = pytest.importorskip("bea.formats.dds")
    data = dds_header(64, 32, mip_count=7) + bytes(DDS_BC1_CHAIN_BYTES)
    assert pillow_reads(data)[:2] == ("DDS", (64, 32))

    info = imageinfo.probe_bytes(data)
    name = dds.parse_header(data).format.name
    assert "BC1" in name  # the legacy FourCC "DXT1" is BC1
    assert info == ImageInfo("dds", 64, 32, mip_count=7, pixel_format=name, compressed=True)
    assert imageinfo.probe_bytes(data[:128]) == info  # a legacy header is 128 bytes


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # A legacy cube map: CUBEMAP (0x200) and its six face bits (0xFC00).
        (
            dds_header(64, 64, caps2=0x200 | 0xFC00),
            ImageInfo("dds", 64, 64, layers=6, compressed=True, notes=("cubemap",)),
        ),
        # A legacy volume texture: the DEPTH flag and VOLUME (0x200000).
        (
            dds_header(64, 32, depth=16, caps2=0x200000),
            ImageInfo("dds", 64, 32, depth=16, compressed=True, notes=("volume",)),
        ),
        # DX10: BC7_UNORM (98), a 2D texture (3), an array of four.
        (
            dds_header(64, 32, pixel_format=DDS_DX10, dx10=(98, 3, 0, 4, 0)),
            ImageInfo("dds", 64, 32, layers=4, compressed=True, notes=("array",)),
        ),
        # DX10: two cubes (miscFlag 0x4).
        (
            dds_header(64, 64, pixel_format=DDS_DX10, dx10=(98, 3, 0x4, 2, 0)),
            ImageInfo("dds", 64, 64, layers=12, compressed=True, notes=("cubemap", "array")),
        ),
        # Plain 32-bit pixels described by channel masks.
        (dds_header(WIDTH, HEIGHT, pixel_format=DDS_BGRA), ImageInfo("dds", WIDTH, HEIGHT)),
    ],
)
def test_dds_notes_with_the_real_parser(header: bytes, expected: ImageInfo) -> None:
    dds = pytest.importorskip("bea.formats.dds")
    name = dds.parse_header(header).format.name
    assert isinstance(name, str)
    assert name
    assert imageinfo.probe_bytes(header) == dataclasses.replace(expected, pixel_format=name)


def test_dds_malformed_with_the_real_parser(tmp_path: Path) -> None:
    pytest.importorskip("bea.formats.dds")
    header = dds_header(64, 32)
    for data in (b"DDS ", header[:100], header[:127]):
        with pytest.raises(FormatError, match="DDS"):
            imageinfo.probe_bytes(data)
    path = tmp_path / "cut.dds"
    path.write_bytes(header[:100])
    with pytest.raises(FormatError, match="DDS"):
        imageinfo.probe(path)


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_dds_written_by_pillow(tmp_path: Path, mode: str) -> None:
    pytest.importorskip("bea.formats.dds")
    path = tmp_path / "image.dds"
    Image.new(mode, (WIDTH, HEIGHT)).save(path)
    with Image.open(path) as reopened:
        size = reopened.size

    info = imageinfo.probe(path)
    assert info is not None
    assert (info.container, info.width, info.height) == ("dds", *size)
    assert (info.depth, info.mip_count, info.layers) == (1, 1, 1)
    assert info.compressed is False
    assert info.notes == ()
    assert imageinfo.probe_bytes(path.read_bytes()) == info


# ----------------------------------------------------------------------------------
# What is not an image, and what decides
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00", id="one-byte"),
        pytest.param(b"\x89PNG\r\n\x1a", id="png-signature-cut"),
        pytest.param(b"\x89PNG\r\n\x1a\x0b" + bytes(40), id="png-signature-last-byte-wrong"),
        pytest.param(b"\xff", id="jpeg-signature-cut"),
        pytest.param(b"\xd8\xff" + bytes(40), id="jpeg-signature-swapped"),
        pytest.param(b"B", id="bmp-signature-cut"),
        pytest.param(b"MB" + bytes(40), id="bmp-signature-swapped"),
        pytest.param(b"DDS", id="dds-magic-cut"),
        pytest.param(b"dds " + bytes(124), id="dds-magic-lower-case"),
        pytest.param(KTX1_IDENTIFIER[:11], id="ktx-identifier-cut"),
        pytest.param(b"\xabKTX 12\xbb\r\n\x1a\n" + bytes(60), id="ktx-unknown-version"),
        pytest.param(b"\xabKTX 20\xbb\n\r\x1a\n" + bytes(60), id="ktx2-identifier-mangled"),
        pytest.param(b"\x13\xab\xa1", id="astc-magic-cut"),
        pytest.param(b"\x5c\xa1\xab\x13" + bytes(12), id="astc-magic-swapped"),
        pytest.param(b"GIF89a" + bytes(60), id="another-image-format"),
        pytest.param(b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(40), id="yet-another-image-format"),
        pytest.param(b"just some text, long enough to hold any header there is\n" * 3, id="text"),
        pytest.param(bytes(200), id="zeros"),
        pytest.param(bytes(range(256)), id="every-byte-value"),
    ],
)
@pytest.mark.parametrize("extension", ["", ".png", ".jpg", ".tga", ".dds", ".bin"])
def test_unknown_data_is_not_an_image(tmp_path: Path, data: bytes, extension: str) -> None:
    assert imageinfo.probe_bytes(data, extension=extension) is None
    path = tmp_path / f"file{extension}"
    path.write_bytes(data)
    assert imageinfo.probe(path) is None


@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.parametrize("extension", [".png", ".jpg", ".tga", ".bmp", ".ktx", ".astc", ".dat", ""])
def test_signature_beats_extension(tmp_path: Path, name: str, extension: str) -> None:
    sample = SAMPLES[name]
    # TGA is the exception: without a signature, the extension is what makes it one.
    recognized = sample.signature_size > 0 or extension == ".tga"
    expected = sample.expected if recognized else None
    assert imageinfo.probe_bytes(sample.data, extension=extension) == expected
    path = tmp_path / f"wrong-name{extension}"
    path.write_bytes(sample.data)
    assert imageinfo.probe(path) == expected


def test_recognized_signature_under_a_tga_name_is_an_error_when_malformed(tmp_path: Path) -> None:
    # A PNG named .tga is a PNG, so a broken one is a broken PNG: not "no image".
    path = tmp_path / "broken.tga"
    path.write_bytes(png_header(0, 0, 8, 6))
    with pytest.raises(FormatError, match="PNG"):
        imageinfo.probe(path)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_probe_and_probe_bytes_agree(tmp_path: Path, name: str) -> None:
    sample = SAMPLES[name]
    path = tmp_path / f"sample{sample.extension}"
    path.write_bytes(sample.data)
    assert imageinfo.probe(path) == sample.expected
    assert imageinfo.probe(str(path)) == sample.expected
    assert imageinfo.probe_bytes(sample.data, extension=sample.extension) == sample.expected


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(png_header(WIDTH, HEIGHT, 3, 2), id="png"),
        pytest.param(SOI + JFIF + SOS, id="jpeg"),
        pytest.param(_bmp_header(40, WIDTH, HEIGHT, 7), id="bmp"),
        pytest.param(ktx1_header(WIDTH, HEIGHT, faces=3), id="ktx"),
        pytest.param(ktx2_header(0, HEIGHT), id="ktx2"),
        pytest.param(astc_header((8, 8, 1), (WIDTH, 0, 1)), id="astc"),
        pytest.param(PNG_SIGNATURE, id="png-truncated"),
        pytest.param(SOI + jpeg_segment(0xE1, bytes(5000))[:-1], id="jpeg-truncated"),
    ],
)
def test_probe_and_probe_bytes_fail_alike(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "broken.bin"
    path.write_bytes(data)
    with pytest.raises(FormatError) as from_bytes:
        imageinfo.probe_bytes(data, extension=".bin")
    with pytest.raises(FormatError) as from_file:
        imageinfo.probe(path)
    assert str(from_file.value) == str(from_bytes.value)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_prefix_of_a_file_is_handled(name: str) -> None:
    sample = SAMPLES[name]
    assert len(sample.data) > sample.header_size
    # Every prefix up to 32 bytes past the header (or up to the whole sample, if shorter).
    for size in range(min(len(sample.data), sample.header_size + 32) + 1):
        prefix = sample.data[:size]
        if size >= sample.header_size:
            # Everything the probe reports is in the header: the rest is not needed.
            assert imageinfo.probe_bytes(prefix, extension=sample.extension) == sample.expected
        elif sample.signature_size and size >= sample.signature_size:
            # Recognized, then cut short: an error, never a guess.
            with pytest.raises(FormatError, match=r"truncated|ends before"):
                imageinfo.probe_bytes(prefix, extension=sample.extension)
        else:
            # Not even a whole signature (or, for TGA, a whole header): not an image.
            assert imageinfo.probe_bytes(prefix, extension=sample.extension) is None


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_what_follows_the_header_does_not_matter(name: str) -> None:
    sample = SAMPLES[name]
    header = sample.data[: sample.header_size]
    for tail in (b"\x00" * 300, b"\xff" * 300, bytes(range(256)) * 2, TGA_FOOTER):
        assert imageinfo.probe_bytes(header + tail, extension=sample.extension) == sample.expected


def test_mutated_headers_never_raise_anything_but_format_error() -> None:
    rng = random.Random(0xBEA)
    outcomes = {"image": 0, "none": 0, "error": 0}
    for sample in SAMPLES.values():
        prefix = sample.data[: sample.header_size + 16]
        for _ in range(400):
            mutated = bytearray(prefix)
            for _ in range(rng.randint(1, 4)):
                mutated[rng.randrange(len(mutated))] = rng.randrange(256)
            try:
                result = imageinfo.probe_bytes(bytes(mutated), extension=sample.extension)
            except FormatError as error:
                assert str(error)
                outcomes["error"] += 1
                continue
            if result is None:
                outcomes["none"] += 1
            else:
                assert isinstance(result, ImageInfo)
                assert result.width >= 1
                assert result.height >= 1
                assert result.depth >= 1
                assert result.mip_count >= 1
                assert result.layers >= 1
                json.dumps(result.as_dict())
                outcomes["image"] += 1
    # The mutations reach every kind of outcome, so the loop above is not vacuous.
    assert all(count > 100 for count in outcomes.values()), outcomes


# ----------------------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "head", "expected"),
    [
        ("big.png", SAMPLES["png"].data, SAMPLES["png"].expected),
        ("big.bmp", SAMPLES["bmp"].data, SAMPLES["bmp"].expected),
        ("big.ktx", SAMPLES["ktx"].data, SAMPLES["ktx"].expected),
        ("big.ktx2", SAMPLES["ktx2"].data, SAMPLES["ktx2"].expected),
        ("big.astc", SAMPLES["astc"].data, SAMPLES["astc"].expected),
        # A .tga name settles it: the footer at the far end is not consulted.
        ("big.tga", SAMPLES["tga"].data, SAMPLES["tga"].expected),
        # A plausible TGA header under another name: one more look, at the last bytes.
        ("big.bin", SAMPLES["tga"].data, None),
        ("unknown.bin", bytes(64), None),
    ],
)
def test_probe_reads_a_small_part_of_a_large_file(
    tmp_path: Path,
    opened_files: list[CountingFile],
    name: str,
    head: bytes,
    expected: ImageInfo | None,
) -> None:
    path = tmp_path / name
    path.write_bytes(head + bytes(range(256)) * 12_000)  # 3 MB behind the header
    assert path.stat().st_size > 3_000_000
    assert imageinfo.probe(path) == expected
    (raw,) = opened_files
    assert raw.closed
    assert 0 < raw.bytes_read <= 16 * 1024


def test_probe_finds_the_tga_footer_at_the_end_of_a_large_file(
    tmp_path: Path, opened_files: list[CountingFile]
) -> None:
    path = tmp_path / "large.bin"
    path.write_bytes(tga_header(4096, 4096, 2, 24) + bytes(3_000_000) + TGA_FOOTER)
    expected = ImageInfo("tga", 4096, 4096, pixel_format="RGB8", notes=("bottom-up",))
    assert imageinfo.probe(path) == expected
    (raw,) = opened_files
    assert 0 < raw.bytes_read <= 16 * 1024


def test_probe_closes_the_file_when_the_header_is_malformed(
    tmp_path: Path, opened_files: list[CountingFile]
) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(png_header(0, 0, 8, 6))
    with pytest.raises(FormatError):
        imageinfo.probe(path)
    (raw,) = opened_files
    assert raw.closed


def test_probe_opens_paths_longer_than_260_characters(tmp_path: Path) -> None:
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/texture.png"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, SAMPLES["png"].data)
    assert imageinfo.probe(target) == SAMPLES["png"].expected


def test_probe_lets_filesystem_errors_through(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        imageinfo.probe(tmp_path / "missing.png")
    with pytest.raises(OSError):
        imageinfo.probe(tmp_path)  # a directory


# ----------------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------------


def test_probe_bytes_accepts_any_bytes_like_object() -> None:
    sample = SAMPLES["png"]
    mutable = bytearray(sample.data)
    assert imageinfo.probe_bytes(mutable) == sample.expected
    mutable.extend(b"the buffer is not left locked")  # would raise if a view were still held
    assert imageinfo.probe_bytes(memoryview(sample.data)) == sample.expected
    assert imageinfo.probe_bytes(memoryview(sample.data)[:29]) == sample.expected


def test_probe_bytes_releases_the_buffer_when_the_header_is_malformed() -> None:
    mutable = bytearray(png_header(0, 0, 8, 6))
    with pytest.raises(FormatError):
        imageinfo.probe_bytes(mutable)
    mutable.clear()


@pytest.mark.parametrize("data", ["text", None, 12, ["\x89PNG"]])
def test_probe_bytes_rejects_what_is_not_bytes(data: object) -> None:
    with pytest.raises(TypeError):
        imageinfo.probe_bytes(data)  # type: ignore[arg-type]


@pytest.mark.parametrize("extension", [None, b".tga", 3])
def test_probe_bytes_rejects_an_extension_that_is_not_a_string(extension: object) -> None:
    with pytest.raises(TypeError):
        imageinfo.probe_bytes(SAMPLES["tga"].data, extension=extension)  # type: ignore[arg-type]
