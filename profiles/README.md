# Profiles

A profile is everything BEA knows about one game, written as data. The engine in `bea/`
never names a game; it only executes what a profile declares. Supporting a game means
adding one folder here and nothing else.

```text
profiles/
    README.md            this guide
    _template/           a commented starting point (ignored by the engine)
    <TitleID>/           one folder per game, named after its title ID
        profile.yaml     what to do with textures, videos and configuration files
        patches.yaml     optional: byte patches for the executables, per build ID
        *.md             optional notes: how values were found, what was tested
```

The complete reference of both files is [docs/PROFILES.md](../docs/PROFILES.md). That
document is normative: the loader implements exactly what it says.

## Adding a game

You need a game folder that you extracted yourself from a game you own (an `ExeFS`
and/or a `RomFS` directory). Keep it outside the repository's tracked files, for example
under `.gamedump/`.

1. **Identify the game.** `bea inspect <game folder>` prints the title ID and the build
   ID of every executable module.
2. **Start the profile.** `bea profile init <game folder>` creates
   `profiles/<TitleID>/` with the IDs of your dump already filled in. (Or copy
   `_template/` to a folder named after the title ID: 16 hex digits, uppercase.)
3. **Fill it in.** Write the same title ID in `profile.yaml` (and in `patches.yaml` if
   you keep it), then keep the sections you need and delete the rest. Only `schema` and
   `title_id` are required. Patches are written per build ID, so use the build IDs that
   `bea inspect` printed for *your* dump.
4. **Validate.** `bea profile validate profiles/<TitleID>` lists every problem at once,
   each with the file, the key path and what to do about it. Fix them until it reports
   none. Without arguments, `bea profile validate` checks every profile.
5. **Dry run.** `bea build <game folder> --dry-run` shows what the profile would do to
   your dump without writing anything: which textures shrink and how, which settings
   change, which patches match. A mismatch here means the profile does not fit this
   version of the game.
6. **Test and take notes.** A patch that applies is not yet a patch that works. Record
   what you tested (hardware, game version, result) in a `.md` file next to the YAML.

Tips that save a round trip:

- Quote identifiers and values. Unquoted, YAML reads an ID made of digits as a number,
  `on` as `true` and `0.70` as `0.7`. The loader refuses those instead of guessing.
- Where rules are listed, the first rule that matches a file wins.
- Paths are relative to the RomFS root, use `/` and are matched case-insensitively.

## The data-only rule

A profile is a recipe, not a copy of anything:

- A profile folder holds only `.yaml`, `.yml` and `.md` files, each at most 256 KiB.
- No code, no assets, no binaries, no keys, no dumps and no listings of a game's files.
- No byte of the game: a patch site is verified by the SHA-256 of a small window of the
  user's own executable (`expected_sha256`, printed by `bea nso hunk`), never by the
  original bytes. Replacement bytes are written by you.
- Byte patches are small: a byte string holds at most 256 bytes and the patch bytes of
  one build total at most 16 KiB.
- Every patch states its `category` (framerate, resolution, ... see docs/PROFILES.md).
- Patches exist to make a game you own fit more modest hardware (resolution, frame rate,
  memory, quality settings). Anything meant to defeat a protection, a licence check or
  an online check does not belong here.

`tests/test_arch_rules.py` enforces the file rules for everything under this directory,
and it also keeps game-specific names and IDs out of `bea/`. If a game needs something
the engine cannot do, the answer is a general, profile-driven capability in the engine,
never a special case for one game.
