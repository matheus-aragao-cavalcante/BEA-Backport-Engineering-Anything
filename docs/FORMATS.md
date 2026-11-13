# File formats

The facts BEA relies on, each with the public documentation it comes from. Code under
`bea/formats/` implements exactly what is written here; when you learn something new
about a format, add it here with its source in the same change.

All multi-byte integers are little-endian unless a section says otherwise.

Public sources used throughout:

- switchbrew wiki: <https://switchbrew.org/wiki/NSO>, <https://switchbrew.org/wiki/NPDM>,
  <https://switchbrew.org/wiki/NACP>, <https://switchbrew.org/wiki/ExeFS>,
  <https://switchbrew.org/wiki/NCA>, <https://switchbrew.org/wiki/Title_list>
- Atmosphere documentation of its loader and LayeredFS (the folder layout BEA writes):
  `docs/components/modules/loader.md` and `docs/features/` in the Atmosphere repository
- LZ4 block format: <https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md>
- Microsoft Learn, DDS: <https://learn.microsoft.com/en-us/windows/win32/direct3ddds/dx-graphics-dds-pguide>
  and the `DDS_HEADER`, `DDS_PIXELFORMAT`, `DDS_HEADER_DXT10` and `DXGI_FORMAT` pages
- W3C PNG specification: <https://www.w3.org/TR/png/>
- Truevision TGA 2.0 specification; ITU-T T.81 (JPEG); Khronos KTX 1.1 and KTX 2.0;
  ARM ASTC encoder file format (`Docs/FileFormat.md` in the astc-encoder repository)

## Identifiers

### Title ID (program ID)

A 64-bit value written as **16 upper-case hex digits** (BEA accepts either case on
input). For a game with application ID `B` (low 13 bits zero):

| ID | Value |
| :-- | :-- |
| application / program 0 | `B` |
| program *n* of a multi-program application | `B + n` |
| update (patch) | `B + 0x800` |
| add-on content item *n* | `B + 0x1000 + n` |

The base ID of any member of the family is the ID with its low 13 bits cleared. LayeredFS
folders and BEA profiles use the **program ID**; an update's executable carries the base
program ID. Applications start at `0x0100_0000_0001_0000`; lower IDs are system software,
which BEA never patches.

Atmosphere writes program IDs in lower case and the SD card is case-insensitive; some mod
loaders on case-sensitive hosts require upper case. Upper case works everywhere.

### Build ID (module ID)

The 32-byte module ID at offset `0x40` of an NSO header: the linker's build-id digest,
left-aligned and zero-padded (usually 20 significant bytes; 16, 32 and all-zero exist).

BEA's canonical text form: **upper-case hex with trailing zero bytes removed**
(`bea.hexutil.build_id_from_bytes`). Two IDs are equal when they are equal after
zero-padding to 32 bytes, which is how loaders compare them. A prefix that drops non-zero
bytes (for example the first 16 bytes of a 20-byte ID) is a *different* ID.

## NSO0 executable modules

The files `main`, `rtld`, `sdk` and `subsdk0`…`subsdk9` of an ExeFS. Source: switchbrew
"NSO"; LZ4 block format.

Header, `0x100` bytes:

| Offset | Size | Field |
| :-- | :-- | :-- |
| 0x00 | 4 | magic `NSO0` |
| 0x04 | 4 | version (0) |
| 0x0C | 4 | flags (below) |
| 0x10 / 0x20 / 0x30 | 4 | text / rodata / data: file offset |
| 0x14 / 0x24 / 0x34 | 4 | memory offset |
| 0x18 / 0x28 / 0x38 | 4 | size in memory (decompressed) |
| 0x1C / 0x2C | 4 | module-name offset / size (not validated by loaders) |
| 0x3C | 4 | bss size |
| 0x40 | 32 | module ID (build ID) |
| 0x60 / 0x64 / 0x68 | 4 | stored size of text / rodata / data |
| 0x88–0x9F | 24 | informational offsets relative to rodata (preserved verbatim) |
| 0xA0 / 0xC0 / 0xE0 | 32 | SHA-256 of the decompressed text / rodata / data |

Flags: bits 0–2 segment compressed; bits 3–5 verify that segment's hash at load; bit 6
execute-only text (newer firmware); bit 7 compressed segments use a zstd-derived codec
("ZBIC", newer firmware) instead of LZ4.

Facts that shape the implementation (`bea/formats/nso.py`):

- File offsets are arbitrary; follow the header fields. For an uncompressed segment the
  loader reads *size* bytes and ignores the stored size.
- A compressed segment is one **raw LZ4 block** (no frame, no size prefix) that must
  decode to exactly *size* bytes.
- BEA refuses ZBIC-compressed modules (`UnsupportedInputError`) rather than guess.
- The **memory image** places each segment at its memory offset; gaps are zero; bss is
  only a number (real modules declare gigabytes of it, so BEA never allocates it).
- Loader rules (warnings in BEA, not errors): memory offsets are multiples of `0x1000`,
  text starts at 0, segments do not overlap and come in order.
- Hashes cover the decompressed bytes; loaders check them before applying IPS patches.
- Writing a module back (`replace` mode) uses the image layout: header, then every segment
  at `memory offset + 0x100`, stored uncompressed, hashes recomputed. Then
  `file[0x100 + m] == image[m]`.

## IPS and IPS32 patches

Sources: the classic IPS format as widely documented; Atmosphere's loader documentation
for how patches are found and applied to modules.

All integers in IPS files are **big-endian**.

| Part | IPS | IPS32 |
| :-- | :-- | :-- |
| magic | `PATCH` | `IPS32` |
| record offset | 3 bytes | 4 bytes |
| record size | 2 bytes (`0` introduces an RLE record: 2-byte run length + 1 fill byte) | same |
| terminator | `EOF` | `EEOF` |

- A record can never *start* at the offset whose bytes equal the terminator (`0x454F46`,
  resp. `0x45454F46`): readers check for the terminator first. BEA starts such a record
  one byte earlier; this is the only case in which one original byte of the module ends
  up in a generated file, and the build report says so.
- Both formats use the `.ips` extension; the magic selects the format. BEA writes classic
  IPS when every offset fits in 24 bits, IPS32 otherwise; literal records only, sorted,
  non-overlapping, never merged across unchanged bytes.
- Patch files for a module are found at
  `atmosphere/exefs_patches/<any folder name>/<build ID hex>.ips`; trailing zero bytes of
  the ID may be omitted, the extension must be lower-case `.ips`.
- They are applied to the **decompressed memory image**, after the hash check, and the
  **record offset equals memory offset + `0x100`** (the first `0x100` offsets stand for
  the module header and are never written). Records outside the image make loaders abort
  or skip; BEA never emits them.

## Folder layouts BEA writes

Atmosphere (copy the `atmosphere` folder to the root of the SD card):

```text
atmosphere/contents/<TitleID>/romfs/<path>     replaces one file of the game's RomFS
atmosphere/contents/<TitleID>/exefs/<module>   replaces one ExeFS module
atmosphere/exefs_patches/<name>/<BuildID>.ips  patches the module with that build ID
```

LayeredFS replaces **whole files**; it cannot delete a file. Paths are compared byte for
byte, so a replacement must reproduce the original path's letter case exactly. RomFS
overlays are keyed by title ID only (they stay active after an update), while IPS files
are keyed by build ID (they stop matching after an update). Holding a button at launch
can disable LayeredFS, but not IPS patches.

Flat layout, for generic mod loaders: `<name>/romfs/<path>` and `<name>/exefs/<BuildID>.ips`.

## main.npdm

Program metadata inside the ExeFS; the authoritative source of the program ID. Source:
switchbrew "NPDM".

| Offset | Field |
| :-- | :-- |
| 0x00 | magic `META` |
| 0x0C | flags: bit 0 = 64-bit code; bits 1–3 address-space type |
| 0x20 | process name, 16 bytes (normally `Application`; **not** the game's title) |
| 0x70 / 0x74 | ACI0 offset / size |
| 0x78 / 0x7C | ACID offset / size |
| ACI0 + 0x00 | magic `ACI0` |
| ACI0 + 0x10 | **program ID** (u64) |
| ACID + 0x200 | magic `ACID` |
| ACID + 0x210 / 0x218 | program ID minimum / maximum |

Always follow the offsets in the header. A file is at most `0x8000` bytes.

## control.nacp

Optional: most extracted folders do not contain it (it belongs to a different part of the
package). Source: switchbrew "NACP". Size `0x4000` bytes.

| Offset | Field |
| :-- | :-- |
| 0x0000 | title block: 16 entries × `0x300` (name `0x200` + publisher `0x100`, UTF-8, NUL-padded) |
| 0x302C | supported-language flags (bit *i* = language index *i*) |
| 0x3038 | presence group ID |
| 0x3060 | display version, 16 bytes ASCII, free-form text |
| 0x3070 | add-on content base ID |
| 0x3078 | save-data owner ID |
| 0x30B0 | local communication IDs, 8 × u64 |
| 0x30F8 | seed for pseudo device ID |
| 0x3212 | program index |
| 0x3215 | title-block format: 0 plain, 1 compressed |

Compressed title block (newer files): a u16 length at offset 0, then a raw DEFLATE stream
that inflates to exactly `0x6000` bytes (32 entries). Language order: American English,
British English, Japanese, French, German, Latin American Spanish, Spanish, Italian, Dutch,
Canadian French, Portuguese, Russian, Korean, Traditional Chinese, Simplified Chinese,
Brazilian Portuguese, Polish, Thai.

The NACP has **no application-ID field**. Several fields usually hold it (save-data owner,
presence group, add-on base minus `0x1000`, first local communication ID); BEA infers the
ID from them by majority only when there is no `main.npdm`, and labels it as inferred.

## Extracted folders

An executable section holds `main` and `main.npdm`, usually `rtld` and `sdk`, optionally
`subsdkN`; modules load in the order `rtld`, `main`, `subsdk0`…`subsdk9`, `sdk`. Tools name
the folders differently (`ExeFS`, `exefs`, `0`, …), so BEA detects the ExeFS by content
(a `main` file starting with `NSO0`) and the RomFS by name (`romfs`, `data`, `1`),
comparing names without regard to case.

## DDS textures

Source: Microsoft Learn (programming guide for DDS, `DDS_HEADER`, `DDS_PIXELFORMAT`,
`DDS_HEADER_DXT10`, `DXGI_FORMAT`).

- Magic `DDS ` then a 124-byte header (`dwSize` 124): flags, height, width,
  pitch-or-linear-size, depth, mip count, 11 reserved words, a 32-byte pixel format
  (flags, FourCC, bit count, four masks), caps 1–4. With FourCC `DX10` a 20-byte extension
  follows (DXGI format, resource dimension, misc flag `0x4` = cube, array size).
- Mip count: `dwMipMapCount` when non-zero, else 1, whatever the flags say. Readers do not
  rely on `dwPitchOrLinearSize`.
- Data order: for each array element and each cube face (+X, −X, +Y, −Y, +Z, −Z), the full
  mip chain from largest to smallest; level *i* is `max(1, w >> i)` × `max(1, h >> i)`.
  No padding between levels, faces or rows.
- Level size: block-compressed `max(1, ⌈w/4⌉) × max(1, ⌈h/4⌉) × B` with B = 8 (BC1, BC4)
  or 16 (BC2, BC3, BC5, BC6H, BC7); ASTC `⌈w/bw⌉ × ⌈h/bh⌉ × 16`; uncompressed
  `⌈w × bpp / 8⌉ × h`.
- DXGI numbers: BC1 70–72, BC2 73–75, BC3 76–78, BC4 79–81, BC5 82–84, BC6H 94–96, BC7
  97–99; ASTC is not part of Microsoft's enumeration (some tools use 133 + 4·i).
- **Dropping the top k mip levels** is lossless and independent of the pixel format: keep
  levels k… of every layer, rewrite width, height (and depth), mip count and the pitch or
  linear size, clear the mip flags when one level is left, preserve every other byte.

## Image headers (probing)

`bea/formats/imageinfo.py` reads dimensions without decoding pixels.

| Container | Signature | Dimensions | Byte order |
| :-- | :-- | :-- | :-- |
| PNG | `89 50 4E 47 0D 0A 1A 0A`, first chunk `IHDR` | width, height u32 at 16, 20; bit depth, colour type, interlace | big-endian |
| TGA | none (18-byte header; optional `TRUEVISION-XFILE.` footer) | width, height u16 at 12, 14; image type at 2 (9–11 = RLE); descriptor bit 5 = top-down | little-endian |
| JPEG | `FF D8`, then marker segments | frame header (`C0`–`CF` except `C4`, `C8`, `CC`): height, width u16 | big-endian |
| BMP | `BM` | DIB header at 14; negative height = top-down | little-endian |
| KTX 1 / KTX 2 | 12-byte identifiers | fields after the identifier | KTX 1: per endianness field |
| ASTC | `13 AB A1 5C` | block size at 4–6, 24-bit dimensions at 7, 10, 13 | little-endian |
| DDS | `DDS ` | see above | little-endian |

Detection is by signature; TGA, which has none, only when the extension says so and the
header is plausible.

## Packaged dumps (recognition only)

BEA does not read packaged dumps. It only recognizes them to say so: the extensions
`.nsp`, `.nsz`, `.xci`, `.xcz`, `.nca`, `.ncz`; a file starting with `PFS0` or `HFS0`; a
file with `HEAD` at offset `0x100`. Their content is encrypted and opening it is out of
scope (see [ROADMAP.md](ROADMAP.md)). `bea/formats/pfs0.py` can list the entries of the
unencrypted outer container, which is used only to produce that message and in tests.
