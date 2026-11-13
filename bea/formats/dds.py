"""DirectDraw Surface (``.dds``) textures: header, mip-chain arithmetic, lossless downsizing.

A DDS file is a fixed header that describes one texture, followed by the pixels of all
its surfaces stored back to back with no further structure. Everything BEA does with the
format follows from that:

* the header alone says how large every mip level is, so the levels can be located, and
  the file checked, without decoding a pixel (:func:`parse_header`, :class:`DdsHeader`);
* the largest levels can therefore simply be cut out (:func:`drop_mips`). Each level
  removed halves the texture and loses nothing, and it works for every pixel format whose
  size rule is known, including the ones nothing here can decode or encode;
* decoding and re-encoding pixels (:func:`decode_surface`, :func:`encode_surface`), which
  resampling needs, is limited to the formats that have a :attr:`DdsFormat.codec`.

File layout
-----------
Public documentation: Microsoft Learn, "Programming Guide for DDS" with its pages "DDS
Texture Example", "DDS Cube Map Example" and "DDS Volume Texture Example"; the
structures ``DDS_HEADER``, ``DDS_PIXELFORMAT`` and ``DDS_HEADER_DXT10``; the
``DXGI_FORMAT`` enumeration. Every integer is a little-endian ``u32``::

    0     magic, the four bytes "DDS "
    4     DDS_HEADER, 124 bytes
            +0    dwSize (124)            +4    dwFlags
            +8    dwHeight                +12   dwWidth
            +16   dwPitchOrLinearSize     +20   dwDepth
            +24   dwMipMapCount           +28   dwReserved1[11]
            +72   DDS_PIXELFORMAT, 32 bytes
                    +0   dwSize (32)      +4   dwFlags
                    +8   dwFourCC         +12  dwRGBBitCount
                    +16  dwRBitMask       +20  dwGBitMask
                    +24  dwBBitMask       +28  dwABitMask
            +104  dwCaps                  +108  dwCaps2
            +112  dwCaps3                 +116  dwCaps4
            +120  dwReserved2
    128   DDS_HEADER_DXT10, 20 bytes, only when the pixel format has the FOURCC flag and
          dwFourCC is "DX10"
            +0    dxgiFormat              +4    resourceDimension (2: 1D, 3: 2D, 4: 3D)
            +8    miscFlag (0x4: cube)    +12   arraySize
            +16   miscFlags2
    128 or 148    pixel data

Flag values::

    dwFlags             CAPS 0x1, HEIGHT 0x2, WIDTH 0x4, PITCH 0x8, PIXELFORMAT 0x1000,
                        MIPMAPCOUNT 0x20000, LINEARSIZE 0x80000, DEPTH 0x800000
    pixel format flags  ALPHAPIXELS 0x1, ALPHA 0x2, FOURCC 0x4, RGB 0x40, YUV 0x200,
                        LUMINANCE 0x20000
    dwCaps              COMPLEX 0x8, TEXTURE 0x1000, MIPMAP 0x400000
    dwCaps2             CUBEMAP 0x200; faces +X 0x400, -X 0x800, +Y 0x1000, -Y 0x2000,
                        +Z 0x4000, -Z 0x8000; VOLUME 0x200000

Pixel data
----------
For each array element, for each cube face (+X, -X, +Y, -Y, +Z, -Z; faces a legacy file
does not declare are simply absent), the complete mip chain from the largest level to the
smallest. Level ``i`` measures ``max(1, width >> i)`` by ``max(1, height >> i)`` and, in a
volume texture, holds ``max(1, depth >> i)`` slices one after the other. Nothing is
padded: rows are tightly packed and levels follow each other directly. One level of one
surface occupies

* ``ceil(w / bw) * ceil(h / bh) * block_bytes`` bytes in a block-compressed format, whose
  blocks cover ``bw`` x ``bh`` pixels (4 x 4 for BC1 to BC7, the footprint for ASTC);
* ``ceil(w * bits_per_pixel / 8) * h`` bytes in an uncompressed format.

Reading rules
-------------
Writers are sloppy about flags, so the reader is as forgiving as mainstream readers are,
and strict about sizes instead:

* the magic, ``dwSize`` (124) and the pixel format's ``dwSize`` (32) must be right;
* the mip count is ``dwMipMapCount``, or 1 when that is 0, whatever the MIPMAPCOUNT flag
  says; it cannot exceed what the dimensions allow;
* a legacy file is a volume texture when it has the DEPTH flag or the VOLUME caps bit,
  and a cube map when it has the CUBEMAP caps bit (as many faces as face bits, or 6 when
  none is set); a DX10 file says so with ``resourceDimension`` 4 and ``miscFlag`` 0x4;
* ``arraySize`` 0 is read as 1, and the height of a 1D texture as 1;
* a file shorter than its header announces is truncated (:meth:`DdsHeader.trailing_bytes`).

Pixel formats
-------------
A :class:`DdsFormat` is a name plus the size rule above. DX10 files name their format
with a ``DXGI_FORMAT`` number; legacy files use a FourCC (``DXT1`` ... ``DXT5``, ``ATI1``,
``ATI2``, ``BC4U``, ``BC4S``, ``BC5U``, ``BC5S``, or a ``D3DFORMAT`` number stored in the
FourCC field: the table "Common DDS File Resource Formats" of the programming guide) or
a bit count with channel masks. A legacy spelling of a format that also exists in DXGI
yields the same :class:`DdsFormat` (``DXT1`` is ``BC1_UNORM``); mask layouts have names of
their own (``B8G8R8A8``, ``L8`` ...). A format that is not recognized keeps a name
(``DXGI_<number>``, the four characters of the FourCC, ``UNKNOWN_<bits>BPP``) and, unless
a bit count gives it away, has no size rule: it can be reported, not processed.

The ASTC numbers (133 to 187) come from the ``DXGI_FORMAT`` page as Microsoft published
it in 2015, together with the page "Adaptive Scalable Texture Compression" (14
footprints, 16-byte blocks, a TYPELESS / UNORM / UNORM_SRGB triple each). They were later
withdrawn from the enumeration, which leaves that range unassigned, and some tools still
write them.

Codecs
------
:attr:`DdsFormat.codec` says how one surface is decoded and encoded with Pillow. The raw
codecs (``RGBA``, ``BGRA``, ``BGRX``, ``RGBX``, ``BGR``, ``RGB``, ``L``, ``LA``) are
Pillow raw-mode names: the bytes of a pixel in file order. The block codecs (``DXT1``,
``DXT5``, ``BC5``) borrow the encoder and decoder of Pillow's DDS plugin for the payload
alone; the headers Pillow writes are never reused. ``None`` means BEA cannot re-encode
the format (BC2: Pillow's encoder packs its alpha incorrectly; BC4, BC6H, BC7, ASTC: no
encoder), which leaves mip dropping.

Robustness
----------
Sizes and counts read from a file are never trusted: they are compared with the bytes
really available before anything is sliced or allocated, and the mip count is bounded by
the dimensions, so a truncated or hostile file ends in a
:class:`~bea.errors.FormatError`, not in a crash or a long wait.
"""

from __future__ import annotations

import io
import os
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

from bea.errors import FormatError
from bea.fsutil import fs_path

__all__ = [
    "CODEC_NAMES",
    "DDS_MAGIC",
    "DdsFormat",
    "DdsHeader",
    "build_dds",
    "decode_surface",
    "drop_mips",
    "encode_surface",
    "format_by_name",
    "is_dds",
    "parse_header",
    "read_header",
    "resized_header",
]

#: The four bytes every DDS file starts with.
DDS_MAGIC = b"DDS "
#: Every value :attr:`DdsFormat.codec` can take (besides ``None``).
CODEC_NAMES: frozenset[str] = frozenset(
    {"RGBA", "BGRA", "BGRX", "RGBX", "BGR", "RGB", "L", "LA", "DXT1", "DXT5", "BC5"}
)

type _Buffer = bytes | bytearray | memoryview

_HEADER_STRUCT_SIZE = 124
_PIXELFORMAT_STRUCT_SIZE = 32
#: Offset of the pixel data without / with a DDS_HEADER_DXT10.
_LEGACY_DATA_OFFSET = 128
_DX10_DATA_OFFSET = 148

# Absolute offsets of the header fields that are read or rewritten on their own.
_OFFSET_FLAGS = 8
_OFFSET_HEIGHT = 12
_OFFSET_WIDTH = 16
_OFFSET_PITCH = 20
_OFFSET_DEPTH = 24
_OFFSET_MIP_COUNT = 28
_OFFSET_PIXELFORMAT = 76
_OFFSET_FOURCC = 84
_OFFSET_CAPS = 108

_MAIN = struct.Struct("<7I")  # dwSize, dwFlags, dwHeight, dwWidth, pitch, dwDepth, mips
_PIXELFORMAT = struct.Struct("<II4s5I")  # dwSize, dwFlags, dwFourCC, bit count, 4 masks
_CAPS = struct.Struct("<4I")  # dwCaps, dwCaps2, dwCaps3, dwCaps4
_DX10 = struct.Struct("<5I")  # dxgiFormat, resourceDimension, miscFlag, arraySize, miscFlags2
_U32 = struct.Struct("<I")
_U32_MAX = 0xFFFFFFFF

_DDSD_CAPS = 0x1
_DDSD_HEIGHT = 0x2
_DDSD_WIDTH = 0x4
_DDSD_PITCH = 0x8
_DDSD_PIXELFORMAT = 0x1000
_DDSD_MIPMAPCOUNT = 0x20000
_DDSD_LINEARSIZE = 0x80000
_DDSD_DEPTH = 0x800000

_DDPF_ALPHAPIXELS = 0x1
_DDPF_ALPHA = 0x2
_DDPF_FOURCC = 0x4
_DDPF_RGB = 0x40
_DDPF_LUMINANCE = 0x20000

_DDSCAPS_COMPLEX = 0x8
_DDSCAPS_TEXTURE = 0x1000
_DDSCAPS_MIPMAP = 0x400000

_DDSCAPS2_CUBEMAP = 0x200
#: The six face bits: +X 0x400, -X 0x800, +Y 0x1000, -Y 0x2000, +Z 0x4000, -Z 0x8000.
_DDSCAPS2_CUBEMAP_FACES = 0xFC00
_DDSCAPS2_VOLUME = 0x200000

_DX10_FOURCC = b"DX10"
_DIMENSION_TEXTURE1D = 2
_DIMENSION_TEXTURE2D = 3
_DIMENSION_TEXTURE3D = 4
_MISC_TEXTURECUBE = 0x4

_CUBE_FACES = 6


@dataclass(frozen=True, slots=True)
class DdsFormat:
    """A pixel format: its name and the rule that gives the size of a surface.

    Exactly one of the two size rules applies, or neither for a format that is only
    known by name (see :attr:`size_known`).
    """

    #: ``BC7_UNORM``, ``R8G8B8A8_UNORM``, ``B8G8R8A8``, ``ASTC_6x6_UNORM`` ...
    name: str
    #: Pixels covered by one block, horizontally and vertically (block-compressed formats).
    block_width: int = 1
    block_height: int = 1
    #: Bytes of one block; greater than zero means the format is block-compressed.
    block_bytes: int = 0
    #: Bits of one pixel (uncompressed formats).
    bits_per_pixel: int = 0
    #: How Pillow decodes and encodes a surface (one of :data:`CODEC_NAMES`), or ``None``
    #: when BEA cannot re-encode the format.
    codec: str | None = None

    def __post_init__(self) -> None:
        if self.block_width < 1 or self.block_height < 1:
            raise ValueError(
                f"{self.name}: a block covers at least 1x1 pixels, "
                f"got {self.block_width}x{self.block_height}"
            )
        if self.block_bytes < 0 or self.bits_per_pixel < 0:
            raise ValueError(f"{self.name}: a size cannot be negative")
        if self.block_bytes and self.bits_per_pixel:
            raise ValueError(
                f"{self.name}: a format is sized by blocks or by bits per pixel, not both"
            )
        if self.codec is not None and self.codec not in CODEC_NAMES:
            raise ValueError(f"{self.name}: unknown codec {self.codec!r}")

    @property
    def compressed(self) -> bool:
        """Tell whether the format stores blocks of pixels rather than pixels."""
        return self.block_bytes > 0

    @property
    def size_known(self) -> bool:
        """Tell whether the size of a surface can be computed (:meth:`level_size`)."""
        return self.block_bytes > 0 or self.bits_per_pixel > 0

    def level_size(self, width: int, height: int) -> int:
        """Return the size in bytes of one ``width`` x ``height`` surface.

        A surface is one mip level of one layer (one depth slice of it, in a volume
        texture). Partial blocks count as whole blocks; rows of an uncompressed surface
        are tightly packed, each rounded up to a whole byte.

        Raises:
            FormatError: the format has no size rule (it is only known by name).
            ValueError: ``width`` or ``height`` is smaller than 1.
        """
        if width < 1 or height < 1:
            raise ValueError(f"a surface is at least 1x1 pixels, got {width}x{height}")
        if self.block_bytes:
            across = -(-width // self.block_width)
            down = -(-height // self.block_height)
            return across * down * self.block_bytes
        if self.bits_per_pixel:
            return (width * self.bits_per_pixel + 7) // 8 * height
        raise FormatError(
            f"DDS: the pixel format {self.name} is not understood, so the size of a "
            f"{width}x{height} surface cannot be computed"
        )


@dataclass(frozen=True, slots=True)
class DdsHeader:
    """What the header of a DDS file says, normalized by the reading rules of this module.

    The first group of fields is the interpretation (what the file holds); the second is
    the raw content of the header fields those decisions were taken from.
    """

    #: Dimensions of the largest mip level. ``height`` is 1 for a 1D texture and ``depth``
    #: is 1 for anything but a volume texture.
    width: int
    height: int
    depth: int
    #: Number of mip levels of every layer, at least 1.
    mip_count: int
    #: Number of array elements (number of cubes, for a cube map), at least 1.
    array_size: int
    #: Number of cube faces stored per array element; 1 when the texture is not a cube map.
    faces: int
    is_volume: bool
    is_cubemap: bool
    format: DdsFormat
    #: Whether a DDS_HEADER_DXT10 follows the main header.
    has_dx10: bool
    #: The ``DXGI_FORMAT`` number of a DX10 file, ``None`` for a legacy file.
    dxgi_format: int | None
    #: Offset of the pixel data: 128, or 148 with a DDS_HEADER_DXT10.
    header_size: int
    #: ``dwFlags``, as stored.
    flags: int
    #: ``dwPitchOrLinearSize``, as stored (writers fill it unreliably: it is never used).
    pitch_or_linear_size: int
    #: ``dwFlags`` of the pixel format, as stored.
    pf_flags: int
    #: The four bytes of ``dwFourCC``, as stored (meaningful with the FOURCC flag).
    fourcc: bytes
    #: ``dwRGBBitCount``, as stored.
    rgb_bit_count: int
    #: The R, G, B and A bit masks, as stored.
    masks: tuple[int, int, int, int]
    #: ``dwCaps`` and ``dwCaps2``, as stored.
    caps: int
    caps2: int

    @property
    def layers(self) -> int:
        """Number of mip chains in the file: array elements times cube faces."""
        return self.array_size * self.faces

    def level_dimensions(self, level: int) -> tuple[int, int, int]:
        """Return ``(width, height, depth)`` of mip level ``level`` (0 is the largest).

        Raises:
            ValueError: ``level`` is not in ``range(mip_count)``.
        """
        if not 0 <= level < self.mip_count:
            raise ValueError(
                f"mip level {level} is out of range: the texture has {self.mip_count} level(s)"
            )
        return (
            max(1, self.width >> level),
            max(1, self.height >> level),
            max(1, self.depth >> level),
        )

    def level_size(self, level: int) -> int:
        """Return the bytes of mip level ``level`` of one layer, all its depth slices included.

        Raises:
            FormatError: the pixel format has no size rule.
            ValueError: ``level`` is not in ``range(mip_count)``.
        """
        width, height, depth = self.level_dimensions(level)
        return self.format.level_size(width, height) * depth

    def layer_size(self) -> int:
        """Return the bytes of one layer: one complete mip chain.

        Raises:
            FormatError: the pixel format has no size rule.
        """
        return sum(self.level_size(level) for level in range(self.mip_count))

    def data_size(self) -> int:
        """Return the bytes of pixel data the header announces (every layer, every level).

        Raises:
            FormatError: the pixel format has no size rule.
        """
        return self.layers * self.layer_size()

    def level_offset(self, layer: int, level: int) -> int:
        """Return the offset in the file of mip level ``level`` of layer ``layer``.

        Layers are numbered in file order: array element by array element and, inside a
        cube map, face by face.

        Raises:
            FormatError: the pixel format has no size rule.
            ValueError: ``layer`` is not in ``range(layers)`` or ``level`` is not in
                ``range(mip_count)``.
        """
        if not 0 <= layer < self.layers:
            raise ValueError(
                f"layer {layer} is out of range: the texture has {self.layers} layer(s)"
            )
        if not 0 <= level < self.mip_count:
            raise ValueError(
                f"mip level {level} is out of range: the texture has {self.mip_count} level(s)"
            )
        before = sum(self.level_size(index) for index in range(level))
        return self.header_size + layer * self.layer_size() + before

    def trailing_bytes(self, file_size: int) -> int:
        """Return how many bytes of a ``file_size``-byte file follow the pixel data.

        Zero for a file that ends with its last mip level. This is also the completeness
        check: a file shorter than the header announces is refused.

        Raises:
            FormatError: the file is truncated (the message says by how much); the pixel
                format has no size rule; or the file is a legacy 24-bit texture exactly as
                large as it would be with every row padded to a multiple of 4 bytes, a
                layout some very old writers used and that is not supported: reading it
                as tightly packed rows followed by trailing bytes would shear the image.
            ValueError: ``file_size`` is negative.
        """
        if file_size < 0:
            raise ValueError(f"file_size cannot be negative, got {file_size}")
        data = self.data_size()
        expected = self.header_size + data
        if file_size < expected:
            raise FormatError(
                f"DDS: truncated file: {self._describe()} needs {expected} bytes "
                f"({self.header_size} of header and {data} of pixel data), the file has "
                f"{file_size}: {expected - file_size} bytes are missing"
            )
        if self.format.bits_per_pixel == 24 and not self.has_dx10:
            padded = self.layers * sum(
                ((width * 3 + 3) & ~3) * height * depth
                for width, height, depth in map(self.level_dimensions, range(self.mip_count))
            )
            if padded > data and file_size == self.header_size + padded:
                raise FormatError(
                    f"DDS: {self._describe()} holds {padded} bytes of pixel data where "
                    f"tightly packed rows take {data}: its rows are padded to a multiple "
                    f"of 4 bytes (DWORD-aligned), a layout that is not supported"
                )
        return file_size - expected

    def _describe(self) -> str:
        """Summarize the texture for an error message."""
        size = f"{self.width}x{self.height}"
        if self.is_volume:
            size += f"x{self.depth}"
        return (
            f"a {size} {self.format.name} texture with {self.mip_count} mip level(s) "
            f"and {self.layers} layer(s)"
        )


def is_dds(prefix: bytes) -> bool:
    """Tell whether a file that starts with ``prefix`` announces itself as a DDS file.

    Only the magic is looked at: :func:`parse_header` decides whether the header is valid.
    """
    return prefix[: len(DDS_MAGIC)] == DDS_MAGIC


def parse_header(data: _Buffer) -> DdsHeader:
    """Parse the header of a DDS file.

    ``data`` holds at least the header: 128 bytes, or 148 when the file has a
    DDS_HEADER_DXT10. Anything after it is ignored, so the whole file may be passed just
    as well; no pixel data is read, and the size of the file is not checked here (see
    :meth:`DdsHeader.trailing_bytes`).

    Raises:
        FormatError: wrong magic; fewer bytes than the header takes; ``dwSize`` is not
            124 or the pixel format's ``dwSize`` is not 32; a width or height of 0; more
            mip levels than the dimensions allow; a ``resourceDimension`` that is not 2,
            3 or 4; a volume texture that is also an array, or also a cube map.
    """
    # The header is copied out and the views are released on the way out of this block:
    # a view kept alive by a traceback would leave a caller's bytearray locked.
    with memoryview(data) as raw, raw.cast("B") as view:
        prefix = bytes(view[:_DX10_DATA_OFFSET])

    magic = prefix[: len(DDS_MAGIC)]
    # Compared over the bytes that are there: a file too short to hold the magic is
    # "truncated" only as long as what it does hold agrees with the magic.
    if magic != DDS_MAGIC[: len(magic)]:
        raise FormatError(f"DDS: wrong magic at offset 0: expected {DDS_MAGIC!r}, found {magic!r}")
    if len(prefix) < _LEGACY_DATA_OFFSET:
        raise FormatError(
            f"DDS: truncated header: the magic and DDS_HEADER take {_LEGACY_DATA_OFFSET} "
            f"bytes, only {len(prefix)} available"
        )
    size, flags, height, width, pitch, depth_field, mip_field = _MAIN.unpack_from(
        prefix, len(DDS_MAGIC)
    )
    if size != _HEADER_STRUCT_SIZE:
        raise FormatError(
            f"DDS: DDS_HEADER.dwSize at offset 4: expected {_HEADER_STRUCT_SIZE}, found {size}"
        )
    pf_size, pf_flags, fourcc, bit_count, *mask_fields = _PIXELFORMAT.unpack_from(
        prefix, _OFFSET_PIXELFORMAT
    )
    if pf_size != _PIXELFORMAT_STRUCT_SIZE:
        raise FormatError(
            f"DDS: DDS_PIXELFORMAT.dwSize at offset {_OFFSET_PIXELFORMAT}: expected "
            f"{_PIXELFORMAT_STRUCT_SIZE}, found {pf_size}"
        )
    masks = (mask_fields[0], mask_fields[1], mask_fields[2], mask_fields[3])
    caps, caps2, _caps3, _caps4 = _CAPS.unpack_from(prefix, _OFFSET_CAPS)

    has_dx10 = bool(pf_flags & _DDPF_FOURCC) and fourcc == _DX10_FOURCC
    dxgi_format: int | None = None
    array_size = 1
    if has_dx10:
        if len(prefix) < _DX10_DATA_OFFSET:
            raise FormatError(
                f"DDS: truncated header: the FourCC is 'DX10', so a DDS_HEADER_DXT10 "
                f"follows at offset {_LEGACY_DATA_OFFSET} and the header takes "
                f"{_DX10_DATA_OFFSET} bytes, only {len(prefix)} available"
            )
        dxgi_format, dimension, misc_flag, array_field, _misc_flags2 = _DX10.unpack_from(
            prefix, _LEGACY_DATA_OFFSET
        )
        if dimension not in (_DIMENSION_TEXTURE1D, _DIMENSION_TEXTURE2D, _DIMENSION_TEXTURE3D):
            raise FormatError(
                f"DDS: DDS_HEADER_DXT10.resourceDimension at offset "
                f"{_LEGACY_DATA_OFFSET + 4}: expected 2 (1D), 3 (2D) or 4 (3D), "
                f"found {dimension}"
            )
        pixel_format = _DXGI_FORMATS.get(dxgi_format) or DdsFormat(f"DXGI_{dxgi_format}")
        is_volume = dimension == _DIMENSION_TEXTURE3D
        is_cubemap = bool(misc_flag & _MISC_TEXTURECUBE)
        faces = _CUBE_FACES if is_cubemap else 1
        array_size = array_field or 1  # some writers leave 0 where they mean "one"
        if dimension == _DIMENSION_TEXTURE1D:
            height = 1  # a 1D texture has one row, whatever dwHeight says
    else:
        pixel_format = _legacy_format(pf_flags, fourcc, bit_count, masks)
        is_volume = bool(flags & _DDSD_DEPTH or caps2 & _DDSCAPS2_VOLUME)
        is_cubemap = bool(caps2 & _DDSCAPS2_CUBEMAP)
        # Direct3D 9 allowed partial cube maps: only the declared faces are stored. No
        # face bit at all is a writer that forgot them, not a cube map without faces.
        declared = (caps2 & _DDSCAPS2_CUBEMAP_FACES).bit_count()
        faces = (declared or _CUBE_FACES) if is_cubemap else 1
    depth = max(1, depth_field) if is_volume else 1

    if width < 1:
        raise FormatError(f"DDS: dwWidth at offset {_OFFSET_WIDTH} is 0: a texture has pixels")
    if height < 1:
        raise FormatError(f"DDS: dwHeight at offset {_OFFSET_HEIGHT} is 0: a texture has pixels")
    if is_volume and array_size > 1:
        raise FormatError(
            f"DDS: DDS_HEADER_DXT10.arraySize at offset {_LEGACY_DATA_OFFSET + 12} is "
            f"{array_size} in a volume texture: a 3D texture cannot be an array"
        )
    if is_volume and is_cubemap and depth > 1:
        raise FormatError(
            f"DDS: the header describes a volume texture (depth {depth}) that is also a "
            f"cube map: no layout is defined for that"
        )
    mip_count = mip_field or 1
    # Halving stops at 1x1x1: floor(log2(largest dimension)) + 1 levels at most. This
    # also bounds every loop over the levels, whatever the header claims.
    most = max(width, height, depth).bit_length()
    if mip_count > most:
        volume = f"x{depth}" if is_volume else ""
        raise FormatError(
            f"DDS: dwMipMapCount at offset {_OFFSET_MIP_COUNT} is {mip_count}, but a "
            f"{width}x{height}{volume} texture has at most {most} mip level(s)"
        )
    return DdsHeader(
        width=width,
        height=height,
        depth=depth,
        mip_count=mip_count,
        array_size=array_size,
        faces=faces,
        is_volume=is_volume,
        is_cubemap=is_cubemap,
        format=pixel_format,
        has_dx10=has_dx10,
        dxgi_format=dxgi_format,
        header_size=_DX10_DATA_OFFSET if has_dx10 else _LEGACY_DATA_OFFSET,
        flags=flags,
        pitch_or_linear_size=pitch,
        pf_flags=pf_flags,
        fourcc=fourcc,
        rgb_bit_count=bit_count,
        masks=masks,
        caps=caps,
        caps2=caps2,
    )


def read_header(path: str | os.PathLike[str]) -> DdsHeader:
    """Parse the header of the DDS file stored at ``path``.

    Only the first 148 bytes of the file are read, whatever its size.

    Raises:
        FormatError: the file is not a valid DDS file (see :func:`parse_header`).
        OSError: the file cannot be opened or read.
    """
    # Unbuffered on purpose: a buffered reader would fetch a whole buffer of pixel data
    # behind the header. One read of an unbuffered file may return less than was asked
    # for, hence the loop.
    chunks: list[bytes] = []
    remaining = _DX10_DATA_OFFSET
    with open(fs_path(path), "rb", buffering=0) as handle:
        while remaining > 0:
            chunk = handle.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    return parse_header(b"".join(chunks))


def resized_header(
    data: _Buffer, header: DdsHeader, *, width: int, height: int, depth: int, mip_count: int
) -> bytes:
    """Return the header bytes of ``data`` rewritten for new dimensions and mip count.

    ``header`` is :func:`parse_header` of ``data``. The result is a copy of
    ``data[:header.header_size]`` in which only the fields that depend on the size
    change; every other byte is preserved, reserved fields included (writers leave their
    signature there):

    * ``dwWidth``, ``dwHeight`` and ``dwMipMapCount``; ``dwDepth`` for a volume texture;
    * the MIPMAPCOUNT flag and the MIPMAP caps bit: set when ``mip_count`` is greater
      than 1, cleared otherwise;
    * the COMPLEX caps bit: set when ``mip_count`` is greater than 1 or the texture is a
      cube map or a volume texture, cleared otherwise;
    * ``dwPitchOrLinearSize``, only when the header declares it: with the LINEARSIZE flag
      it becomes the size of the new top level of one surface, with the PITCH flag the
      bytes of one row of it (with both, whichever the format calls for: the size for a
      block-compressed format, the row for an uncompressed one).

    Raises:
        FormatError: the header declares a pitch or a linear size and the pixel format
            has no size rule to recompute it with.
        ValueError: ``data`` is shorter than the header or does not start with the DDS
            magic; a dimension or the mip count is smaller than 1 or does not fit in 32
            bits; ``depth`` is not 1 for a texture that is not a volume; ``mip_count``
            is more than the new dimensions allow.
    """
    with memoryview(data) as raw, raw.cast("B") as view:
        out = bytearray(view[: header.header_size])
    if len(out) < header.header_size or not is_dds(bytes(out[: len(DDS_MAGIC)])):
        raise ValueError(
            f"data does not hold the {header.header_size}-byte DDS header it was parsed from"
        )
    _check_dimensions(width, height, depth)
    if depth != 1 and not header.is_volume:
        raise ValueError(f"only a volume texture has a depth, got depth={depth}")
    _check_mip_count(mip_count, width, height, depth)

    (flags,) = _U32.unpack_from(out, _OFFSET_FLAGS)
    (caps,) = _U32.unpack_from(out, _OFFSET_CAPS)
    if mip_count > 1:
        flags |= _DDSD_MIPMAPCOUNT
        caps |= _DDSCAPS_MIPMAP | _DDSCAPS_COMPLEX
    else:
        flags &= ~_DDSD_MIPMAPCOUNT
        caps &= ~_DDSCAPS_MIPMAP
        if header.is_cubemap or header.is_volume:
            caps |= _DDSCAPS_COMPLEX
        else:
            caps &= ~_DDSCAPS_COMPLEX
    _U32.pack_into(out, _OFFSET_FLAGS, flags)
    _U32.pack_into(out, _OFFSET_CAPS, caps)
    _U32.pack_into(out, _OFFSET_HEIGHT, height)
    _U32.pack_into(out, _OFFSET_WIDTH, width)
    _U32.pack_into(out, _OFFSET_MIP_COUNT, mip_count)
    if header.is_volume:
        _U32.pack_into(out, _OFFSET_DEPTH, depth)

    has_linear_size = bool(flags & _DDSD_LINEARSIZE)
    has_pitch = bool(flags & _DDSD_PITCH)
    if has_linear_size or has_pitch:
        # A header that sets both flags contradicts itself; the field then keeps the
        # meaning the documentation gives it for that kind of format.
        as_linear_size = has_linear_size and (header.format.compressed or not has_pitch)
        value = header.format.level_size(width, height if as_linear_size else 1)
        if value > _U32_MAX:
            raise ValueError(
                f"a {'linear size' if as_linear_size else 'pitch'} of {value} bytes does "
                f"not fit in the 32-bit dwPitchOrLinearSize field"
            )
        _U32.pack_into(out, _OFFSET_PITCH, value)
    return bytes(out)


def drop_mips(data: _Buffer, levels: int) -> bytes:
    """Remove the ``levels`` largest mip levels of every layer of a DDS file. Lossless.

    The result is :func:`resized_header` for the dimensions of the old level ``levels``,
    followed by the levels that are kept, layer by layer, in their original order and
    byte for byte. Each level dropped halves the texture (``max(1, size >> 1)``). Bytes
    that follow the pixel data in the input are not carried over.

    Raises:
        FormatError: the header is invalid (see :func:`parse_header`); the file is
            truncated or uses padded 24-bit rows (see
            :meth:`DdsHeader.trailing_bytes`); the pixel format has no size rule.
        TypeError: ``levels`` is not an integer.
        ValueError: ``levels`` is not between 1 and ``mip_count - 1``: at least one level
            must be dropped and at least one must remain.
    """
    if isinstance(levels, bool) or not isinstance(levels, int):
        raise TypeError(f"levels must be an integer, got {type(levels).__name__}")
    header = parse_header(data)
    if not 1 <= levels <= header.mip_count - 1:
        raise ValueError(
            f"cannot drop {levels} mip level(s) of a texture that has {header.mip_count}: "
            f"levels must be between 1 and mip_count - 1"
        )
    with memoryview(data) as raw, raw.cast("B") as view:
        header.trailing_bytes(len(view))  # complete, or FormatError
        width, height, depth = header.level_dimensions(levels)
        prefix = resized_header(
            view,
            header,
            width=width,
            height=height,
            depth=depth,
            mip_count=header.mip_count - levels,
        )
        layer_size = header.layer_size()
        dropped = sum(header.level_size(level) for level in range(levels))
        kept = layer_size - dropped
        first = header.header_size + dropped
        end = header.header_size + header.layers * layer_size
        if header.layers <= kept:
            # The usual case, a few large layers: one slice per layer, copied once.
            chunks = [view[start : start + kept] for start in range(first, end, layer_size)]
            try:
                return b"".join([prefix, *chunks])
            finally:
                for chunk in chunks:
                    chunk.release()
        # More layers than bytes kept per layer: a header may announce millions of tiny
        # layers, and one slice per layer would then cost far more than the data is
        # worth. Copying "column by column" (byte k of every layer in one strided pass)
        # takes as many steps as a layer keeps bytes, which is the smaller number here.
        out = bytearray(len(prefix) + header.layers * kept)
        out[: len(prefix)] = prefix
        for column in range(kept):
            with view[first + column : end : layer_size] as strided:
                out[len(prefix) + column :: kept] = strided.tobytes()
        return bytes(out)


def build_dds(
    width: int,
    height: int,
    fmt: DdsFormat | str,
    layers: Sequence[Sequence[_Buffer]],
    *,
    depth: int = 1,
    cubemap: bool = False,
    dx10: bool | None = None,
) -> bytes:
    """Build a DDS file from the bytes of its mip levels.

    A writer for tests, for the synthetic-game generator and for resampling. ``layers``
    holds, for each layer (array element by array element and, in a cube map, face by
    face in the order +X, -X, +Y, -Y, +Z, -Z), the bytes of each mip level from the
    largest to the smallest. ``fmt`` is a format known to :func:`format_by_name`, or its
    name.

    Header spelling: a legacy header (FourCC ``DXT1``, ``DXT3``, ``DXT5``, ``ATI1`` or
    ``ATI2``, or channel masks) when the format has one, a DDS_HEADER_DXT10 when the
    format only exists in DXGI (sRGB formats, BC6H, BC7 ...) or when the texture is an
    array, which a legacy header cannot describe. ``dx10`` forces one or the other.

    Flags, as documented: CAPS, HEIGHT, WIDTH and PIXELFORMAT always; LINEARSIZE with the
    size of the top level for a block-compressed format, PITCH with the bytes of a row
    for an uncompressed one; MIPMAPCOUNT, COMPLEX and MIPMAP for a mip chain; DEPTH,
    VOLUME and COMPLEX for a volume texture (``depth`` greater than 1); CUBEMAP, the six
    face bits and COMPLEX for a cube map, whose DDS_HEADER_DXT10 also carries ``miscFlag``
    0x4 and the number of cubes as ``arraySize``.

    Raises:
        ValueError: an unknown format name, or a format :func:`format_by_name` does not
            know; a dimension smaller than 1 or too large for 32 bits; no layer, no
            level, or layers with different numbers of levels; more levels than the
            dimensions allow; a level whose size is not the one the format dictates; a
            cube map whose layers are not a multiple of 6; a volume texture with more
            than one layer or flagged as a cube map; a header spelling that cannot
            express the texture (``dx10=False`` for a DXGI-only format or an array,
            ``dx10=True`` or an array for a format that has no ``DXGI_FORMAT`` number).
    """
    pixel_format = format_by_name(fmt) if isinstance(fmt, str) else fmt
    if _FORMATS_BY_NAME.get(pixel_format.name) != pixel_format:
        raise ValueError(
            f"cannot write the pixel format {pixel_format.name}: it is not one of the "
            f"formats format_by_name() knows"
        )
    name = pixel_format.name
    _check_dimensions(width, height, depth)
    if not layers or not layers[0]:
        raise ValueError("a texture needs at least one layer with at least one mip level")
    mip_count = len(layers[0])
    for index, levels in enumerate(layers):
        if len(levels) != mip_count:
            raise ValueError(
                f"layer {index} has {len(levels)} mip level(s), layer 0 has {mip_count}: "
                f"every layer needs the same number"
            )
    _check_mip_count(mip_count, width, height, depth)
    is_volume = depth > 1
    if is_volume and (cubemap or len(layers) != 1):
        raise ValueError(
            "a volume texture has exactly one layer and cannot be a cube map or an array"
        )
    if cubemap and len(layers) % _CUBE_FACES:
        raise ValueError(
            f"a cube map has 6 faces per cube, got {len(layers)} layer(s): not a multiple of 6"
        )
    for level in range(mip_count):
        level_width = max(1, width >> level)
        level_height = max(1, height >> level)
        level_depth = max(1, depth >> level)
        expected = pixel_format.level_size(level_width, level_height) * level_depth
        for index, levels in enumerate(layers):
            found = _byte_length(levels[level])
            if found != expected:
                slices = f" x {level_depth} slices" if is_volume else ""
                raise ValueError(
                    f"layer {index}, mip level {level}: a {level_width}x{level_height}"
                    f"{slices} {name} level is {expected} bytes, got {found}"
                )

    array_size = len(layers) // _CUBE_FACES if cubemap else len(layers)
    dxgi_format = _DXGI_NUMBERS.get(name)
    legacy = _LEGACY_SPELLINGS.get(name)
    use_dx10 = (legacy is None or array_size > 1) if dx10 is None else dx10
    if use_dx10 and dxgi_format is None:
        reason = "dx10=True was requested" if dx10 else "a texture array needs one"
        raise ValueError(
            f"the pixel format {name} has no DXGI_FORMAT number, so it cannot be written "
            f"with a DDS_HEADER_DXT10 ({reason})"
        )
    if not use_dx10 and legacy is None:
        raise ValueError(
            f"the pixel format {name} only exists in DXGI: it needs a DDS_HEADER_DXT10 "
            f"(dx10=False was requested)"
        )
    if not use_dx10 and array_size > 1:
        raise ValueError(
            f"a legacy header cannot describe an array of {array_size} textures: it needs "
            f"a DDS_HEADER_DXT10 (dx10=False was requested)"
        )

    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PIXELFORMAT
    caps = _DDSCAPS_TEXTURE
    caps2 = 0
    if pixel_format.compressed:
        flags |= _DDSD_LINEARSIZE
        pitch_or_linear_size = pixel_format.level_size(width, height)
    else:
        flags |= _DDSD_PITCH
        pitch_or_linear_size = pixel_format.level_size(width, 1)
    if pitch_or_linear_size > _U32_MAX:
        raise ValueError(
            f"a top level of {pitch_or_linear_size} bytes does not fit in the 32-bit "
            f"dwPitchOrLinearSize field"
        )
    if mip_count > 1:
        flags |= _DDSD_MIPMAPCOUNT
        caps |= _DDSCAPS_COMPLEX | _DDSCAPS_MIPMAP
    if is_volume:
        flags |= _DDSD_DEPTH
        caps |= _DDSCAPS_COMPLEX
        caps2 |= _DDSCAPS2_VOLUME
    if cubemap:
        caps |= _DDSCAPS_COMPLEX
        caps2 |= _DDSCAPS2_CUBEMAP | _DDSCAPS2_CUBEMAP_FACES

    if use_dx10 or legacy is None:
        pixel_format_fields = (_DDPF_FOURCC, _DX10_FOURCC, 0, 0, 0, 0, 0)
    else:
        pixel_format_fields = legacy
    parts: list[_Buffer] = [
        DDS_MAGIC,
        _MAIN.pack(
            _HEADER_STRUCT_SIZE,
            flags,
            height,
            width,
            pitch_or_linear_size,
            depth if is_volume else 0,
            mip_count,
        ),
        bytes(_OFFSET_PIXELFORMAT - len(DDS_MAGIC) - _MAIN.size),  # dwReserved1
        _PIXELFORMAT.pack(_PIXELFORMAT_STRUCT_SIZE, *pixel_format_fields),
        _CAPS.pack(caps, caps2, 0, 0),
        bytes(4),  # dwReserved2
    ]
    if use_dx10 and dxgi_format is not None:
        parts.append(
            _DX10.pack(
                dxgi_format,
                _DIMENSION_TEXTURE3D if is_volume else _DIMENSION_TEXTURE2D,
                _MISC_TEXTURECUBE if cubemap else 0,
                array_size,
                0,
            )
        )
    for levels in layers:
        parts.extend(levels)
    return b"".join(parts)


def format_by_name(name: str) -> DdsFormat:
    """Return the pixel format called ``name``.

    The names are those of the ``DXGI_FORMAT`` members this module knows, without their
    prefix (``BC7_UNORM``, ``R8G8B8A8_UNORM_SRGB``, ``ASTC_6x6_UNORM`` ...), and those of
    the legacy mask layouts (``B8G8R8A8``, ``R8G8B8A8``, ``B8G8R8X8``, ``R8G8B8X8``,
    ``B8G8R8``, ``R8G8B8``, ``L8``, ``A8L8``, ``A8``). Every one of them can be written
    by :func:`build_dds`.

    Raises:
        ValueError: no format has that name (names are case-sensitive).
    """
    found = _FORMATS_BY_NAME.get(name)
    if found is None:
        raise ValueError(f"unknown DDS pixel format name: {name!r}")
    return found


def decode_surface(fmt: DdsFormat, data: _Buffer, width: int, height: int) -> Image.Image:
    """Decode one ``width`` x ``height`` surface (one mip level of one layer) into an image.

    The mode of the image follows the codec: ``RGBA`` for the codecs ``RGBA``, ``BGRA``,
    ``DXT1`` and ``DXT5``; ``RGB`` for ``BGRX``, ``RGBX``, ``BGR``, ``RGB`` and ``BC5``
    (whose two channels land in R and G); ``L`` for ``L``; ``LA`` for ``LA``. The image
    owns its pixels: it does not alias ``data``.

    Raises:
        FormatError: ``data`` does not have the size of such a surface, or Pillow cannot
            decode it.
        ValueError: the format has no codec, or ``width`` or ``height`` is smaller than 1.
    """
    codec = _require_codec(fmt)
    expected = fmt.level_size(width, height)
    with memoryview(data) as raw, raw.cast("B") as view:
        if len(view) != expected:
            raise FormatError(
                f"DDS: a {width}x{height} {fmt.name} surface is {expected} bytes, got {len(view)}"
            )
        mode = _RAW_CODEC_MODES.get(codec)
        if mode is None:
            return _decode_blocks(codec, view, width, height)
        # frombytes rather than frombuffer: frombuffer may hand back a window on the
        # caller's buffer (which stays locked for as long as the image lives) and, for
        # RGBX, an image whose mode is "RGBX". frombytes always decodes into a new image
        # of the mode asked for. Stride 0 and orientation 1: packed rows, top to bottom.
        return Image.frombytes(mode, (width, height), view, "raw", codec, 0, 1)


def encode_surface(image: Image.Image, fmt: DdsFormat) -> bytes:
    """Encode an image as one surface (one mip level of one layer) of format ``fmt``.

    The image is first converted to the mode the codec works in (see
    :func:`decode_surface`), so any mode Pillow can convert is accepted; an alpha channel
    the format has no room for is dropped. The unused byte of ``BGRX`` and ``RGBX`` is
    written as ``0xFF``.

    Raises:
        FormatError: Pillow cannot encode the surface, or what it produced does not have
            the size the format dictates.
        ValueError: the format has no codec, or the image is empty.
    """
    codec = _require_codec(fmt)
    width, height = image.size
    expected = fmt.level_size(width, height)
    mode = _RAW_CODEC_MODES.get(codec)
    if mode is not None:
        converted = image if image.mode == mode else image.convert(mode)
        if codec in _PADDED_RAW_CODECS:
            # Pillow leaves the fourth byte of an RGB pixel unspecified when it packs
            # BGRX or RGBX (0 in practice). An opaque alpha channel packed in the same
            # position gives the padding a defined value.
            payload = converted.convert("RGBA").tobytes("raw", _PADDED_RAW_CODECS[codec])
        else:
            payload = converted.tobytes("raw", codec)
    else:
        payload = _encode_blocks(codec, image)
    if len(payload) != expected:
        raise FormatError(
            f"DDS: encoding a {width}x{height} {fmt.name} surface produced {len(payload)} "
            f"bytes where the format dictates {expected}"
        )
    return payload


# ------------------------------------------------------------------------ internals


def _check_dimensions(width: int, height: int, depth: int) -> None:
    """Refuse dimensions that no header can hold."""
    for label, value in (("width", width), ("height", height), ("depth", depth)):
        if not 1 <= value <= _U32_MAX:
            raise ValueError(f"{label} must be between 1 and {_U32_MAX}, got {value}")


def _check_mip_count(mip_count: int, width: int, height: int, depth: int) -> None:
    """Refuse a mip count that the dimensions do not allow (halving stops at 1x1x1)."""
    most = max(width, height, depth).bit_length()
    if not 1 <= mip_count <= most:
        raise ValueError(
            f"a {width}x{height}x{depth} texture has between 1 and {most} mip level(s), "
            f"got {mip_count}"
        )


def _require_codec(fmt: DdsFormat) -> str:
    """Return the codec of ``fmt``, refusing the formats BEA cannot decode or encode."""
    if fmt.codec is None:
        raise ValueError(
            f"the pixel format {fmt.name} has no codec: BEA cannot decode or encode it"
        )
    return fmt.codec


def _byte_length(buffer: _Buffer) -> int:
    """Return the size of a buffer in bytes (``len`` of a memoryview counts items)."""
    return buffer.nbytes if isinstance(buffer, memoryview) else len(buffer)


def _packed(name: str, bits: int, codec: str | None = None) -> DdsFormat:
    """Make an uncompressed format of ``bits`` bits per pixel."""
    return DdsFormat(name, bits_per_pixel=bits, codec=codec)


def _blocks(name: str, block_bytes: int, codec: str | None = None) -> DdsFormat:
    """Make a block-compressed format whose blocks cover 4x4 pixels (BC1 to BC7)."""
    return DdsFormat(name, block_width=4, block_height=4, block_bytes=block_bytes, codec=codec)


#: ASTC footprints (pixels per block, across and down) in the order of their numbers.
_ASTC_FOOTPRINTS = (
    (4, 4),
    (5, 4),
    (5, 5),
    (6, 5),
    (6, 6),
    (8, 5),
    (8, 6),
    (8, 8),
    (10, 5),
    (10, 6),
    (10, 8),
    (10, 10),
    (12, 10),
    (12, 12),
)
_ASTC_FIRST_NUMBER = 133
_ASTC_BLOCK_BYTES = 16


def _astc_formats() -> dict[int, DdsFormat]:
    """Return the ASTC formats by number.

    Footprint ``i`` is TYPELESS at ``133 + 4 * i``, UNORM one above and UNORM_SRGB two
    above; the fourth number of each group is unassigned. Every ASTC block is 16 bytes,
    whatever its footprint.
    """
    return {
        _ASTC_FIRST_NUMBER + 4 * index + step: DdsFormat(
            f"ASTC_{across}x{down}_{suffix}",
            block_width=across,
            block_height=down,
            block_bytes=_ASTC_BLOCK_BYTES,
        )
        for index, (across, down) in enumerate(_ASTC_FOOTPRINTS)
        for step, suffix in enumerate(("TYPELESS", "UNORM", "UNORM_SRGB"))
    }


#: DXGI_FORMAT number -> format, for the numbers BEA knows.
_DXGI_FORMATS: dict[int, DdsFormat] = {
    2: _packed("R32G32B32A32_FLOAT", 128),
    10: _packed("R16G16B16A16_FLOAT", 64),
    11: _packed("R16G16B16A16_UNORM", 64),
    24: _packed("R10G10B10A2_UNORM", 32),
    26: _packed("R11G11B10_FLOAT", 32),
    27: _packed("R8G8B8A8_TYPELESS", 32, "RGBA"),
    28: _packed("R8G8B8A8_UNORM", 32, "RGBA"),
    29: _packed("R8G8B8A8_UNORM_SRGB", 32, "RGBA"),
    31: _packed("R8G8B8A8_SNORM", 32),
    34: _packed("R16G16_FLOAT", 32),
    35: _packed("R16G16_UNORM", 32),
    41: _packed("R32_FLOAT", 32),
    49: _packed("R8G8_UNORM", 16),
    51: _packed("R8G8_SNORM", 16),
    54: _packed("R16_FLOAT", 16),
    56: _packed("R16_UNORM", 16),
    61: _packed("R8_UNORM", 8, "L"),
    65: _packed("A8_UNORM", 8),
    70: _blocks("BC1_TYPELESS", 8, "DXT1"),
    71: _blocks("BC1_UNORM", 8, "DXT1"),
    72: _blocks("BC1_UNORM_SRGB", 8, "DXT1"),
    73: _blocks("BC2_TYPELESS", 16),
    74: _blocks("BC2_UNORM", 16),
    75: _blocks("BC2_UNORM_SRGB", 16),
    76: _blocks("BC3_TYPELESS", 16, "DXT5"),
    77: _blocks("BC3_UNORM", 16, "DXT5"),
    78: _blocks("BC3_UNORM_SRGB", 16, "DXT5"),
    79: _blocks("BC4_TYPELESS", 8),
    80: _blocks("BC4_UNORM", 8),
    81: _blocks("BC4_SNORM", 8),
    82: _blocks("BC5_TYPELESS", 16, "BC5"),
    83: _blocks("BC5_UNORM", 16, "BC5"),
    84: _blocks("BC5_SNORM", 16),
    85: _packed("B5G6R5_UNORM", 16),
    86: _packed("B5G5R5A1_UNORM", 16),
    87: _packed("B8G8R8A8_UNORM", 32, "BGRA"),
    88: _packed("B8G8R8X8_UNORM", 32, "BGRX"),
    90: _packed("B8G8R8A8_TYPELESS", 32, "BGRA"),
    91: _packed("B8G8R8A8_UNORM_SRGB", 32, "BGRA"),
    92: _packed("B8G8R8X8_TYPELESS", 32, "BGRX"),
    93: _packed("B8G8R8X8_UNORM_SRGB", 32, "BGRX"),
    94: _blocks("BC6H_TYPELESS", 16),
    95: _blocks("BC6H_UF16", 16),
    96: _blocks("BC6H_SF16", 16),
    97: _blocks("BC7_TYPELESS", 16),
    98: _blocks("BC7_UNORM", 16),
    99: _blocks("BC7_UNORM_SRGB", 16),
    115: _packed("B4G4R4A4_UNORM", 16),
    **_astc_formats(),
}

#: Legacy FourCC -> format. DXT2 and DXT4 are DXT3 and DXT5 with premultiplied alpha:
#: the same blocks, hence the same format.
_FOURCC_FORMATS: dict[bytes, DdsFormat] = {
    b"DXT1": _DXGI_FORMATS[71],
    b"DXT2": _DXGI_FORMATS[74],
    b"DXT3": _DXGI_FORMATS[74],
    b"DXT4": _DXGI_FORMATS[77],
    b"DXT5": _DXGI_FORMATS[77],
    b"ATI1": _DXGI_FORMATS[80],
    b"BC4U": _DXGI_FORMATS[80],
    b"BC4S": _DXGI_FORMATS[81],
    b"ATI2": _DXGI_FORMATS[83],
    b"BC5U": _DXGI_FORMATS[83],
    b"BC5S": _DXGI_FORMATS[84],
}

#: Legacy D3DFORMAT number stored in the FourCC field -> format. The two formats whose
#: DXGI twin is not in the table above are only ever read, never written.
_D3DFORMAT_FORMATS: dict[int, DdsFormat] = {
    36: _DXGI_FORMATS[11],  # D3DFMT_A16B16G16R16
    110: _packed("R16G16B16A16_SNORM", 64),  # D3DFMT_Q16W16V16U16
    111: _DXGI_FORMATS[54],  # D3DFMT_R16F
    112: _DXGI_FORMATS[34],  # D3DFMT_G16R16F
    113: _DXGI_FORMATS[10],  # D3DFMT_A16B16G16R16F
    114: _DXGI_FORMATS[41],  # D3DFMT_R32F
    115: _packed("R32G32_FLOAT", 64),  # D3DFMT_G32R32F
    116: _DXGI_FORMATS[2],  # D3DFMT_A32B32G32R32F
}

_B8G8R8A8 = _packed("B8G8R8A8", 32, "BGRA")
_R8G8B8A8 = _packed("R8G8B8A8", 32, "RGBA")
_B8G8R8X8 = _packed("B8G8R8X8", 32, "BGRX")
_R8G8B8X8 = _packed("R8G8B8X8", 32, "RGBX")
_B8G8R8 = _packed("B8G8R8", 24, "BGR")
_R8G8B8 = _packed("R8G8B8", 24, "RGB")
_L8 = _packed("L8", 8, "L")
_A8L8 = _packed("A8L8", 16, "LA")
_A8 = _packed("A8", 8)

#: 32-bit layouts recognized by their four masks (R, G, B, A). The first byte of a pixel
#: in the file is the least significant one, which is where the names come from.
_RGB32_LAYOUTS: dict[tuple[int, int, int, int], DdsFormat] = {
    (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000): _B8G8R8A8,
    (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000): _R8G8B8A8,
    (0x00FF0000, 0x0000FF00, 0x000000FF, 0x00000000): _B8G8R8X8,
    (0x000000FF, 0x0000FF00, 0x00FF0000, 0x00000000): _R8G8B8X8,
}
#: 24-bit layouts recognized by their R, G and B masks (there is no room for alpha).
_RGB24_LAYOUTS: dict[tuple[int, int, int], DdsFormat] = {
    (0x00FF0000, 0x0000FF00, 0x000000FF): _B8G8R8,
    (0x000000FF, 0x0000FF00, 0x00FF0000): _R8G8B8,
}

#: Name -> (pixel format flags, FourCC, bit count, R, G, B, A masks): the legacy header
#: :func:`build_dds` writes for the formats that have one.
_LEGACY_SPELLINGS: dict[str, tuple[int, bytes, int, int, int, int, int]] = {
    "BC1_UNORM": (_DDPF_FOURCC, b"DXT1", 0, 0, 0, 0, 0),
    "BC2_UNORM": (_DDPF_FOURCC, b"DXT3", 0, 0, 0, 0, 0),
    "BC3_UNORM": (_DDPF_FOURCC, b"DXT5", 0, 0, 0, 0, 0),
    "BC4_UNORM": (_DDPF_FOURCC, b"ATI1", 0, 0, 0, 0, 0),
    "BC5_UNORM": (_DDPF_FOURCC, b"ATI2", 0, 0, 0, 0, 0),
    "L8": (_DDPF_LUMINANCE, bytes(4), 8, 0xFF, 0, 0, 0),
    "A8L8": (_DDPF_LUMINANCE | _DDPF_ALPHAPIXELS, bytes(4), 16, 0x00FF, 0, 0, 0xFF00),
    "A8": (_DDPF_ALPHA, bytes(4), 8, 0, 0, 0, 0xFF),
    **{
        fmt.name: (_DDPF_RGB | (_DDPF_ALPHAPIXELS if masks[3] else 0), bytes(4), 32, *masks)
        for masks, fmt in _RGB32_LAYOUTS.items()
    },
    **{fmt.name: (_DDPF_RGB, bytes(4), 24, *masks, 0) for masks, fmt in _RGB24_LAYOUTS.items()},
}

#: Name -> DXGI_FORMAT number, for the formats a DDS_HEADER_DXT10 can name.
_DXGI_NUMBERS: dict[str, int] = {fmt.name: number for number, fmt in _DXGI_FORMATS.items()}

#: Every format :func:`format_by_name` knows, which is every format :func:`build_dds`
#: can write.
_FORMATS_BY_NAME: dict[str, DdsFormat] = {
    fmt.name: fmt
    for fmt in (
        *_DXGI_FORMATS.values(),
        *_RGB32_LAYOUTS.values(),
        *_RGB24_LAYOUTS.values(),
        _L8,
        _A8L8,
        _A8,
    )
}


def _legacy_format(
    pf_flags: int, fourcc: bytes, bit_count: int, masks: tuple[int, int, int, int]
) -> DdsFormat:
    """Identify the pixel format of a legacy header (no DDS_HEADER_DXT10)."""
    if pf_flags & _DDPF_FOURCC:
        known = _FOURCC_FORMATS.get(fourcc) or _D3DFORMAT_FORMATS.get(
            int.from_bytes(fourcc, "little")
        )
        return known if known is not None else DdsFormat(_fourcc_name(fourcc))

    red, _green, _blue, alpha = masks
    if bit_count == 32 and masks in _RGB32_LAYOUTS:
        return _RGB32_LAYOUTS[masks]
    if bit_count == 24 and masks[:3] in _RGB24_LAYOUTS:
        return _RGB24_LAYOUTS[masks[:3]]
    if pf_flags & _DDPF_LUMINANCE:
        if bit_count == 8 and red == 0xFF:
            return _L8
        # The programming guide lists A8L8 with the LUMINANCE flag alone, the structure
        # reference says to add ALPHAPIXELS: the alpha mask settles it either way.
        if bit_count == 16 and red == 0x00FF and alpha == 0xFF00:
            return _A8L8
    if pf_flags & _DDPF_ALPHA and bit_count == 8:
        return _A8
    # A bit count is enough to size the surfaces, as long as pixels are whole bytes.
    whole_bytes = bit_count > 0 and bit_count % 8 == 0
    return DdsFormat(f"UNKNOWN_{bit_count}BPP", bits_per_pixel=bit_count if whole_bytes else 0)


def _fourcc_name(fourcc: bytes) -> str:
    """Name an unrecognized FourCC: its four characters, or its value when not printable."""
    if all(0x21 <= byte <= 0x7E for byte in fourcc):
        return fourcc.decode("ascii")
    return f"FOURCC_{int.from_bytes(fourcc, 'little'):08X}"


#: Raw codec -> mode of the Pillow image. The codec name is also the Pillow raw mode.
_RAW_CODEC_MODES: dict[str, str] = {
    "RGBA": "RGBA",
    "BGRA": "RGBA",
    "BGRX": "RGB",
    "RGBX": "RGB",
    "BGR": "RGB",
    "RGB": "RGB",
    "L": "L",
    "LA": "LA",
}
#: Raw codec with an unused byte -> the RGBA raw mode that puts 0xFF in that byte.
_PADDED_RAW_CODECS: dict[str, str] = {"BGRX": "BGRA", "RGBX": "RGBA"}
#: Block codec (also the ``pixel_format`` Pillow's encoder takes) -> the format a surface
#: is wrapped as for Pillow's decoder, whether that file gets a DDS_HEADER_DXT10, and the
#: mode of the image.
_BLOCK_CODECS: dict[str, tuple[str, bool, str]] = {
    "DXT1": ("BC1_UNORM", False, "RGBA"),
    "DXT5": ("BC3_UNORM", False, "RGBA"),
    "BC5": ("BC5_UNORM", True, "RGB"),
}
#: Pixels handed to Pillow's decoder in one go. Pillow refuses to open images it takes
#: for decompression bombs (and warns well before that); large surfaces are decoded in
#: bands of whole block rows, each far below those limits.
_BAND_PIXELS = 16 * 1024 * 1024


def _decode_blocks(codec: str, view: memoryview, width: int, height: int) -> Image.Image:
    """Decode a block-compressed surface with the decoder of Pillow's DDS plugin.

    Blocks are independent of each other, so a surface can be decoded as horizontal bands
    of whole block rows: each band is wrapped in a minimal single-level file of its own.
    """
    name, dx10, mode = _BLOCK_CODECS[codec]
    fmt = _FORMATS_BY_NAME[name]
    row_bytes = fmt.level_size(width, 1)  # one row of blocks
    block_rows = -(-height // fmt.block_height)
    rows_per_band = max(1, _BAND_PIXELS // (width * fmt.block_height))
    image = Image.new(mode, (width, height))
    for first in range(0, block_rows, rows_per_band):
        count = min(rows_per_band, block_rows - first)
        top = first * fmt.block_height
        band_height = min(count * fmt.block_height, height - top)
        with view[first * row_bytes : (first + count) * row_bytes] as band:
            wrapped = build_dds(width, band_height, fmt, [[band]], dx10=dx10)
        try:
            with Image.open(io.BytesIO(wrapped), formats=["DDS"]) as opened:
                opened.load()
                decoded = opened if opened.mode == mode else opened.convert(mode)
                if decoded.size != (width, band_height):
                    raise FormatError(
                        f"DDS: Pillow decoded a {width}x{band_height} {name} surface as "
                        f"{decoded.size[0]}x{decoded.size[1]}"
                    )
                image.paste(decoded, (0, top))
        except (OSError, ValueError, NotImplementedError, Image.DecompressionBombError) as exc:
            raise FormatError(
                f"DDS: Pillow could not decode a {width}x{band_height} {name} surface: {exc}"
            ) from exc
    return image


def _encode_blocks(codec: str, image: Image.Image) -> bytes:
    """Encode an image with the encoder of Pillow's DDS plugin and return the payload."""
    name, _dx10, mode = _BLOCK_CODECS[codec]
    converted = image if image.mode == mode else image.convert(mode)
    buffer = io.BytesIO()
    try:
        converted.save(buffer, format="DDS", pixel_format=codec)
    except (OSError, ValueError, KeyError) as exc:
        raise FormatError(
            f"DDS: Pillow could not encode a {image.size[0]}x{image.size[1]} {name} surface: {exc}"
        ) from exc
    with buffer.getbuffer() as written:
        # Only the payload is kept: the header Pillow writes is unreliable (and is the
        # 148-byte kind when its FourCC is "DX10").
        has_dx10 = bytes(written[_OFFSET_FOURCC : _OFFSET_FOURCC + 4]) == _DX10_FOURCC
        return bytes(written[_DX10_DATA_OFFSET if has_dx10 else _LEGACY_DATA_OFFSET :])
