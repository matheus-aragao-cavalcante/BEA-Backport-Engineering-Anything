# Profile reference

A **profile** is everything BEA knows about one game, written as data. The engine in
`bea/` never names a game; it only executes what a profile declares. Adding support for a
game therefore means adding a directory under `profiles/` and nothing else.

```text
profiles/
└── <TitleID>/              16 uppercase hex digits, e.g. the ID printed by `bea inspect`
    ├── profile.yaml        what to do with assets, media and configuration files
    ├── patches.yaml        optional: byte patches for the executables, per build ID
    └── *.md                optional notes (how the values were found, what was tested)
```

Directories whose name starts with `_` (for example `profiles/_template/`) are ignored by
the engine and are free-form.

This document is normative: `bea/profile/loader.py` implements exactly what is written
here and `bea profile validate` reports any deviation.

## General rules

- **Strict by design.** Unknown keys, wrong types, out-of-range values and duplicate
  entries are errors. A profile that loads is a profile BEA fully understood.
- **Quote identifiers.** Title IDs, build IDs and byte strings must be YAML *strings*.
  Unquoted, YAML may turn `0100000000001000` into an octal number or `12345678` into an
  integer; the loader rejects those with a "quote this value" message instead of guessing.
- **Paths** are RomFS-relative, use `/`, and are matched case-insensitively.
- **Data only.** A profile may contain `.yaml`, `.yml` and `.md` files, each at most
  256 KiB. No code, no assets, no binaries, no keys (enforced by `tests/test_arch_rules.py`).

### Globs

`match` and `exclude` take one glob or a list of globs:

| Pattern | Meaning |
| :-- | :-- |
| `*` | any run of characters inside one path segment |
| `?` | exactly one character (never `/`) |
| `[abc]`, `[a-z]`, `[!abc]` | one character from (or not from) a set |
| `**` (as a whole segment) | any number of directories, including none |
| `*.png` (no `/`) | matched against the file name at any depth |
| `ui/*.png` (has `/`) | anchored at the RomFS root |
| `ui/` | everything below `ui` (same as `ui/**`) |

Where rules are listed, **the first rule that matches a file wins**.

## `profile.yaml`

```yaml
schema: 1
title_id: "0100000000000000"     # must equal the directory name
name: "Example Game"             # optional, informative
description: >                    # optional, informative
  What this backport does and what it was tested on.

target:                           # optional; the default limit box
  max_width: 1280                 # default 1280
  max_height: 720                 # default 720

textures:                         # optional
  enabled: true                   # default true
  defaults:                       # optional; inherited by every rule
    scale: 1.0                    # default: none
    strategy: auto                # auto | mip_drop | resample          (default auto)
    filter: lanczos               # nearest | box | bilinear | hamming | bicubic | lanczos
    power_of_two: preserve        # preserve | ignore                   (default preserve)
    alpha: straight               # straight | weighted                 (default straight)
  rules:                          # optional; first match wins
    - match: ["ui/**"]
      max_width: 1920
      max_height: 1080
    - match: ["**/*_lut.*", "fonts/**"]
      skip: true
    - match: "characters/**/*.dds"
      exclude: "characters/**/*_n.dds"
      scale: 0.5
      strategy: mip_drop

media:                            # optional
  enabled: true                   # default true
  rules:                          # only files matched by a rule are transcoded
    - match: "movies/**/*.mp4"
      max_height: 720
      max_fps: 30
      crf: 23
      audio_bitrate: "128k"

configs:                          # optional
  - match: "config/graphics.ini"
    format: ini
    edits:
      - section: "Rendering"
        key: "ResolutionScale"
        value: "70"
      - section: "Rendering"
        key: "ShadowQuality"
        value: "Low"
        if_missing: add
  - match: "config/settings.json"
    format: json
    edits:
      - path: ["video", "maxFps"]
        value: 30
  - match: "boot/engine.cfg"
    format: text
    edits:
      - find: '^(vsync_interval\s*=\s*)\d+'
        replace: '\g<1>2'

binary:                           # optional
  patchset_name: "example-backport"
  mode: ips                       # ips | replace                        (default ips)
```

### Top level

| Key | Type | Required | Notes |
| :-- | :-- | :-- | :-- |
| `schema` | integer | yes | Must be `1`. |
| `title_id` | string | yes | 16 hex digits; stored uppercase; must equal the directory name (compared case-insensitively). |
| `name` | string | no | Display name. Informative only. |
| `description` | string | no | Free text. |
| `target` | mapping | no | `max_width`, `max_height`: integers >= 1. |
| `textures`, `media`, `binary` | mapping | no | See below. |
| `configs` | list | no | See below. |

### `textures`

A texture is every RomFS file BEA can read as an image (see the README for the list of
formats). A file larger than its limits is made smaller; a file within its limits is not
touched and not copied.

The **limits** of a file are the box `max_width x max_height` and, if set, the uniform
`scale`. The result:

- is never larger than the source and never exceeds the limits;
- keeps the aspect ratio;
- with `power_of_two: preserve` (the default), a texture whose width *and* height are
  powers of two is halved until it fits, so it stays a power of two
  (2048x2048 in a 1280x720 box becomes 512x512; with `ignore` it becomes 720x720).

**Strategies**

| `strategy` | Behaviour |
| :-- | :-- |
| `mip_drop` | Remove the largest mip levels until the texture fits. Lossless and independent of the pixel format (it works for formats BEA cannot encode). Needs a file with a mip chain; if the chain is too short, as many levels as possible are dropped and the build report says so. |
| `resample` | Decode, resize with `filter`, re-encode in the same pixel format, rebuilding the mip chain if there was one. Only for pixel formats BEA can encode. |
| `auto` | `mip_drop` when the file has enough mip levels, else `resample`, else a shorter `mip_drop`; when none is possible the file is reported as unsupported. |

**Alpha.** `straight` filters every channel independently, which is always safe for
textures that pack unrelated data into RGB and A. `weighted` weights colour by alpha while
filtering, which avoids dark or bright halos around cut-out sprites and UI art.

| Rule key | Type | Notes |
| :-- | :-- | :-- |
| `match` | glob or list | Required, non-empty. |
| `exclude` | glob or list | Files that the rule must not apply to (they fall through to later rules). |
| `skip` | boolean | `true`: leave matching files alone. No other tuning key may be set. |
| `max_width`, `max_height` | integer >= 1 | Default: inherit from `target`. |
| `scale` | number, 0 < scale <= 1 | Default: inherit from `defaults`. |
| `strategy`, `filter`, `power_of_two`, `alpha` | enum | Default: inherit from `defaults`. |

Files matched by no rule use `target` and `defaults`. To restrict processing to an explicit
list, end the rules with `- match: "**"` / `skip: true`.

### `media`

Media rules need the external `ffmpeg` and `ffprobe` programs (`bea doctor` tells whether
they were found). Only files matched by a rule are considered, and a matched file is
transcoded only when it exceeds a limit or a bitrate/quality is requested. The container
and the codec family are kept.

| Rule key | Type | Notes |
| :-- | :-- | :-- |
| `match`, `exclude`, `skip` | | As for textures. |
| `max_width`, `max_height` | integer >= 1 | Default: inherit from `target`. |
| `max_fps` | number > 0 | Frame-rate cap. Default: keep. |
| `crf` | integer 0..63 | Constant-quality target for the video encoder. |
| `video_bitrate` | string or integer | For example `"2500k"`, `"4M"` or bits per second. Mutually exclusive with `crf`. |
| `audio_bitrate` | string or integer | Default: copy the audio stream unchanged. |

### `configs`

Each entry edits the text file(s) selected by `match`. Files keep their encoding
(a byte-order mark is detected), their line endings and everything the edits do not touch.
A file may be selected by one `configs` entry only; a file that already holds the
requested values is left alone.

| Key | Type | Notes |
| :-- | :-- | :-- |
| `match` | glob or list | Required. |
| `exclude` | glob or list | Optional. |
| `format` | `ini` \| `json` \| `text` | Required; decides the shape of `edits`. |
| `encoding` | string | Optional Python codec name. Default: BOM detection, else UTF-8. |
| `required` | boolean | Default `true`: it is an error if the rule selects no file. |
| `edits` | list | Required, non-empty. Applied in order. |

**`format: ini`** – line-based; comments, order and duplicate keys elsewhere are preserved.

| Edit key | Type | Notes |
| :-- | :-- | :-- |
| `section` | string or null | Section name without brackets, compared case-insensitively. Omitted or `null`: the keys before the first section header. |
| `key` | string | Required. Compared case-insensitively. Must be unique in the section (several definitions are an error). |
| `value` | string or integer | Required. Other types must be written as quoted strings. |
| `if_missing` | `error` \| `add` \| `ignore` | Default `error`. `add` appends the key to the section (creating the section at the end of the file if needed). |

**`format: json`** – the document is parsed and written back with its original
indentation; key order is preserved.

| Edit key | Type | Notes |
| :-- | :-- | :-- |
| `path` | list of strings/integers | Required, non-empty. Strings select object keys, integers select array elements. |
| `value` | any JSON value | Required (may be `null`). |
| `if_missing` | `error` \| `add` \| `ignore` | Default `error`. `add` creates the final key (and missing parent objects); array indexes are never created. |

**`format: text`** – regular-expression replacement (Python syntax, multi-line mode).

| Edit key | Type | Notes |
| :-- | :-- | :-- |
| `find` | string | Required regular expression. |
| `replace` | string | Required replacement; `\g<1>` refers to a group. |
| `count` | integer >= 1 | Default `1`. The exact number of matches expected; any other number is an error. |

An edit that cannot be applied (`if_missing: error`, a wrong `count`) fails that file and
the build ends with a non-zero status: the profile does not fit this version of the game.

### `binary`

| Key | Type | Notes |
| :-- | :-- | :-- |
| `patchset_name` | string | Folder name for the generated patches. Letters, digits, `.`, `_`, `-`; at most 64 characters; must start with a letter or digit. Default: `bea-<TitleID>`. |
| `mode` | `ips` \| `replace` | `ips` (default) writes one small `<BuildID>.ips` per patched module. `replace` writes a fully patched copy of the module instead; that file is the whole executable of the game and must never be shared. |

## `patches.yaml`

```yaml
schema: 1
title_id: "0100000000000000"          # must equal profile.yaml
builds:
  - build_id: "0123456789ABCDEF0123456789ABCDEF01234567"
    module: main                       # default main
    label: "v1.0.0"                    # optional, informative
    image_base: 0x7100000000           # optional; enables `address`
    patches:
      - name: "render-720p"
        category: resolution
        description: "Internal resolution 1080p -> 720p"
        enabled: true                  # default true
        hunks:
          - offset: 0x1A2B3C
            patch_bytes: "08 5A 80 52"
            expected_sha256: "<64 hex digits printed by bea nso hunk>"
            window_offset: 0x1A2B30
            window_length: 32
```

| Key | Type | Required | Notes |
| :-- | :-- | :-- | :-- |
| `schema` | integer | yes | Must be `1`. |
| `title_id` | string | yes | Must equal the one in `profile.yaml`. |
| `builds` | list | yes | May be empty. One entry per (module, build ID); duplicates are an error. |

**Build entry**

| Key | Type | Notes |
| :-- | :-- | :-- |
| `build_id` | string | Required. Hex, even number of digits, at most 64. Compared the way mod loaders do (zero-padded to 32 bytes), so a 40-digit ID and the same ID followed by zeros are equal. |
| `module` | string | ExeFS file name (`main`, `sdk`, `subsdk0` ...). Lowercase letters and digits. Default `main`. |
| `label` | string | Informative (game version). |
| `image_base` | integer >= 0 | Base address used by your disassembler. Default `0`. |
| `patches` | list | Required. Patch names must be unique within the build. |

**Patch**

| Key | Type | Notes |
| :-- | :-- | :-- |
| `name` | string | Required. Letters, digits, `.`, `_`, `-`. |
| `category` | string | What the patch is for: `framerate`, `resolution`, `dynamic_resolution`, `render_scale`, `lod`, `shadow_quality`, `draw_distance`, `texture_budget`, `memory_budget`, `post_fx`, `perf_fix`. Optional for the engine, **required for profiles in this repository**. There is deliberately no category for anything else: patches that touch protections, licence, entitlement or online checks, platform restrictions, or that cheat, are out of scope. |
| `description` | string | Optional. What the patch does, in your own words. |
| `enabled` | boolean | Default `true`. Disabled patches are validated but not applied. |
| `hunks` | list | Required, non-empty. |

**Hunk** – one in-place replacement, verified before it is written.

| Key | Type | Notes |
| :-- | :-- | :-- |
| `offset` | integer >= 0 | Position relative to the start of the module's memory image: the address shown by a disassembler minus its image base. |
| `address` | integer | Alternative to `offset`: an absolute address; the loader subtracts `image_base`. Exactly one of `offset` / `address` must be given. |
| `patch_bytes` | string | Required. The replacement bytes, written by you. |
| `expected_sha256` | string | The SHA-256 (64 hex digits) of the window below, in the original module. **The form used in this repository: the profile holds no byte of the game.** |
| `window_offset` | integer | Module offset of the verification window. Default: the hunk's own offset. Always a module offset, even when the hunk uses `address`. |
| `window_length` | integer 16..256 | Length of the window. Default: 32, or the length of `patch_bytes` when longer. The window must contain the patched bytes. |
| `expected_bytes` | string | Alternative to `expected_sha256`: the literal bytes that must be at `offset` (same length as `patch_bytes`). For private profiles only: profiles in this repository may not contain bytes of a game. Exactly one of `expected_sha256` / `expected_bytes` must be given. |

Byte strings are hex pairs with optional separators: `"1F 20 03 D5"`, `"1F2003D5"`,
`"0x1F, 0x20, 0x03, 0xD5"`. `bea nso hunk <module> --offset <n> --patch "<hex>"` prints
a complete hunk, hash window included.

**Limits** (they keep profiles declarative): a byte string holds at most 256 bytes; the
`patch_bytes` of one build total at most 16 KiB; hunks of the same patch may not overlap.

### How patches are applied

1. BEA reads the build ID of each ExeFS module and looks for a build entry with the same
   `module` and `build_id`. No entry means no patch for that module (reported, not an
   error). System software is never patched.
2. Every hunk of every *enabled* patch is checked against the decompressed, original
   module: the patched range and the verification window must each lie inside one
   segment, and the window must have the expected SHA-256 (or the range must hold the
   expected bytes). Hunks of two enabled patches may not replace the same bytes.
3. **All or nothing per module**: if any check fails, nothing is written for that module
   and the build ends with a non-zero status. BEA never produces a partially applied
   patch set, and never patches a build the profile was not written for.
4. In `ips` mode the result is `<BuildID>.ips` whose record offsets are the hunk offsets
   plus `0x100` (loaders count the 0x100-byte NSO header). In `replace` mode the result is
   the patched module itself, which contains the whole executable and must never be
   shared.

A hunk whose `patch_bytes` equal the bytes already there produces no IPS record.

## Checking a profile

```console
$ bea profile validate                 # every profile under ./profiles
$ bea profile validate profiles/<TitleID>
$ bea build <game folder> --dry-run    # shows what the profile would do to this dump
```
