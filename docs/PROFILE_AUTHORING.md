# Writing a profile

A profile is the recipe for one game: what to shrink, which settings to change, which
bytes of the executable to replace. This guide walks through writing one and getting it
right. The reference for every key is [PROFILES.md](PROFILES.md).

Ground rules, before anything else:

- You work on a copy of the game **you own**, in a folder **you extracted yourself**, kept
  under `.gamedump/` (git-ignored).
- A profile contains identifiers, hashes, settings and bytes *you wrote*. It never
  contains bytes, strings, listings or assets taken from the game.
- Analysis artefacts (memory images, disassembler databases, decompiler output, reports)
  stay on your machine. Never commit them, never paste them into an issue or a pull
  request.
- Patches change how a game performs on hardware it already targets. Anything that
  defeats a protection, a licence or entitlement check, an online check or a platform
  restriction is out of scope and will not be accepted.

## 1. Look at the game

```console
bea inspect .gamedump/MyGame
bea analyze .gamedump/MyGame
```

`inspect` prints the title ID and the build ID of every executable module. `analyze`
writes `backport_report.json`: the textures larger than the target, the videos, the
configuration keys that look like graphics settings, the containers BEA cannot open yet,
and a guess at the engine. Start from that report; it tells you where the wins are.

## 2. Create the skeleton

```console
bea profile init .gamedump/MyGame
```

This creates `profiles/<TitleID>/` with a `profile.yaml`, a `patches.yaml` that already
lists the build ID of your `main` module, and a `NOTES.md`. The skeleton is valid and does
nothing beyond the generic texture pass.

## 3. Assets and settings (`profile.yaml`)

Work in small steps and look at the plan after each one:

```console
bea build .gamedump/MyGame --dry-run
```

- **Textures.** Decide the target box, then carve out exceptions with rules: larger limits
  for UI, `skip: true` for lookup tables, fonts and anything that encodes data rather than
  a picture. Prefer `strategy: mip_drop` where files have mip chains: it is lossless.
- **Configuration files.** Every edit names the exact section and key (INI), path (JSON)
  or pattern (text). An edit that does not find its target fails the build instead of
  guessing; that is the point.
- **Videos.** Add `media` rules only for files you have checked the game plays through a
  standard decoder.

## 4. Executable patches (`patches.yaml`)

A patch replaces a few bytes of an executable module at a known place, for one exact build.

### 4.1 Get something to analyze

```console
bea nso info  .gamedump/MyGame/ExeFS/main
bea nso image .gamedump/MyGame/ExeFS/main -o work/main.image.bin
```

`nso image` writes the decompressed *memory image* of the module: one flat file in which
**file offset equals module memory offset**. That is the address space profiles use.

Load it in the disassembler of your choice as a raw AArch64 little-endian binary at an
image base you pick (for example `0x7100000000`). Then for any address you see:

```text
module memory offset = address - image base
```

A flat image has no symbol names and no relocations applied, so pointers stored in data
read as zero. Code is position-independent and analyzes normally. If your disassembler has
a loader for this executable format, loading the module itself gives better
cross-references; the offsets are the same.

Agent-driven toolkits help here. [REA](https://github.com/morluto/rea) connects a coding
agent to disassemblers and can open a flat image as a raw binary (format `raw-binary`,
architecture `arm64`, entry point = your image base); it has no loader of its own for this
executable format, and its raw-binary route needs a disassembler host that supports raw
AArch64. Questions that work well: *where is the internal render resolution set?*, *where
is the frame-rate limit?*, *which function sizes the main heap?* Ask for the address and
the instruction, and keep observations, inferences and unknowns apart.

### 4.2 Turn a finding into a hunk

Write the replacement bytes yourself (assemble the instruction you want), then let BEA
produce the hunk:

```console
bea nso hunk .gamedump/MyGame/ExeFS/main --offset 0x1A2B3C --patch "08 5A 80 52"
bea nso hunk .gamedump/MyGame/ExeFS/main --image-base 0x7100000000 --offset 0x71001A2B3C --patch "08 5A 80 52"
```

It prints YAML ready to paste:

```yaml
- offset: 0x1A2B3C
  patch_bytes: "08 5A 80 52"
  expected_sha256: "…"
  window_offset: 0x1A2B3C
  window_length: 32
```

`expected_sha256` is the hash of a 32-byte window of *your* executable around the patch
site. When someone else builds with this profile, BEA hashes the same window of *their*
executable and refuses unless it matches. No byte of the game is stored. (The command also
shows the bytes currently at the site on standard error, for your eyes only.)

Put the hunks in a patch:

```yaml
builds:
  - build_id: "…"            # printed by `bea inspect`
    module: main
    label: "1.0.0"
    patches:
      - name: render-720p
        category: resolution
        description: "Sets the internal render target to 1280x720."
        hunks:
          - offset: 0x1A2B3C
            patch_bytes: "08 5A 80 52"
            expected_sha256: "…"
            window_offset: 0x1A2B3C
            window_length: 32
```

Rules of thumb:

- One patch = one idea. All hunks of a patch apply together or not at all.
- Patch code, not relocated data: bytes that hold absolute pointers are rewritten by the
  system's dynamic linker after patches are applied.
- A profile is tied to a build. For another version of the game, find the sites again in
  that build and add a second entry under `builds`; never reuse offsets across builds.
- Describe what a patch does in your own words. No disassembly listings.

### 4.3 Check it

```console
bea profile validate profiles/<TitleID>
bea build .gamedump/MyGame --dry-run
bea build .gamedump/MyGame
```

`--dry-run` reports, per module, whether the build ID is known to the profile and whether
every window matched. A mismatch means the profile does not fit this executable; BEA
writes nothing for that module.

## 5. Test, then write it down

A patch that applies is not yet a patch that works. Frame-rate changes in particular can
alter game speed or physics. Test on your own hardware, then record in `NOTES.md` what you
tested (hardware, game version, scenes, measurements) and what is known not to work. Text
only.

## 6. Share the recipe

Open a pull request with the profile directory. Do not attach outputs, reports or
captures. See [CONTRIBUTING](../CONTRIBUTING.md) for the checklist.
