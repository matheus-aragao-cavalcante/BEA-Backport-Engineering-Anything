# Architecture

BEA turns *a game folder you extracted yourself* plus *a declarative profile* into
*a LayeredFS mod folder*. This document describes how the code is organized and the rules
it obeys. The rules are enforced by `tests/test_arch_rules.py`; when the two disagree,
fix whichever is wrong in the same change.

## Principles

1. **The engine is game-agnostic; games are data.** Nothing under `bea/` names a game.
   Everything specific to a game lives in `profiles/<TitleID>/` as YAML.
2. **Clean-room.** Code is written from public documentation and from the facts in
   [FORMATS.md](FORMATS.md). The repository holds source text only: no game files, no
   keys, no binaries, and profiles hold no bytes of any game.
3. **Verify, then act; all or nothing.** A profile's assumptions (a build ID, the bytes at
   a patch site, a configuration key) are checked against the user's files before anything
   is written. When one does not hold, BEA refuses and says what it expected and found.
4. **Unknown stays unknown.** What BEA cannot read or process is reported as
   `unsupported`, `invalid` or `mismatch` with a reason. Nothing is skipped silently.
5. **Idempotent and interruptible.** Running a build twice changes nothing the second
   time; an interrupted build resumes; outputs are never left half-written.
6. **No keys, no network.** The only input is an already-extracted folder. BEA does not
   decrypt, download or upload anything.

## Repository layout

```text
bea/                     the engine (game-agnostic)
├── cli.py               argument parsing and text output only
├── pipeline.py          plan -> execute -> reconcile -> package
├── analyze.py           read-only report (backport_report.json)
├── synth.py             synthetic sample game generator (`bea synth`, tests)
├── scaffold.py          profile skeleton (`bea profile init`)
├── ingest/              turns a folder on disk into a GameInput
├── formats/             file formats: nso, ips, npdm, nacp, dds, imageinfo, mediainfo
├── profile/             schema (dataclasses) and the strict YAML loader
├── transformers/        texture, config, media, binary: one input file -> one output file
├── packagers/           layeredfs: staged files -> folder layout
├── model.py             shared data model and logical paths
├── toolbox.py           locating optional external programs (ffmpeg, ffprobe)
└── errors.py  hexutil.py  fsutil.py  globmatch.py     foundation
profiles/                per-game data: <TitleID>/profile.yaml, patches.yaml, notes
docs/                    this file, FORMATS.md, PROFILES.md, PROFILE_AUTHORING.md, ROADMAP.md
tests/                   synthetic tests only; test_arch_rules.py enforces this document
```

Local, git-ignored folders: `.gamedump/` (your dumps), `work/` (staging and cache),
`out/` (results), `tools/` (optional external programs).

## Layers

A module may import only from the layers to its left. `tests/test_arch_rules.py` holds the
same table (`LAYERS`) and checks every import.

| Layer | Modules | May import |
| :-- | :-- | :-- |
| foundation | `errors`, `hexutil`, `fsutil`, `globmatch` | standard library |
| shared model | `model`, `toolbox` | foundation |
| formats | `formats/*` | foundation |
| profile | `profile/*` | foundation |
| ingest | `ingest/*` | foundation, model, formats |
| transformers | `transformers/*` | foundation, model, toolbox, formats, profile |
| packagers | `packagers/*` | foundation, model |
| orchestration | `pipeline`, `analyze`, `synth`, `scaffold` | everything except the CLI |
| command line | `cli`, `__main__` | everything |

Third-party imports are limited to the declared dependencies (Pillow, PyYAML, lz4).

## The pipeline

```text
 game folder ──ingest──▶ GameInput ─┐
                                    ├─plan──▶ actions + skips ──execute──▶ staging tree ──package──▶ out/<TitleID>/
 profiles/<TitleID>/ ──load──▶ Profile ┘        (nothing written)          work/<TitleID>/staging      LayeredFS layout
```

| Step | Code | What happens | Guarantees |
| :-- | :-- | :-- | :-- |
| Ingest | `bea.ingest.open_input` | Finds the ExeFS and RomFS trees, reads the title ID (`main.npdm`), the build ID of every module (NSO header), the optional `control.nacp`, and indexes the RomFS. | Read-only. Detection is by content, names are compared without regard to case. |
| Load | `bea.profile.loader` | Parses and validates `profile.yaml` / `patches.yaml` strictly. No profile: the built-in generic profile (texture downsizing only). | Every problem is reported at once, with its key path. |
| Plan | `bea.pipeline.make_plan` | Each transformer yields an `Action` per file it will transform and a `Skip` per file it recognizes but will not. Config edits and binary patches are verified here. | Read-only. `bea build --dry-run` and `bea analyze` stop here. |
| Execute | `bea.pipeline.build` | Each action runs once; its output goes to `work/<TitleID>/staging/<logical path>`. | Cached, atomic, parallel; one failure does not stop the rest. |
| Reconcile | same | Staged files the plan no longer produces are deleted. | A changed profile never leaves stale outputs. |
| Package | `bea.packagers` | Staged files are linked or copied into the target layout under `out/<TitleID>/`. | Only files BEA created are ever removed; a manifest records them. |

### Logical paths

Inside the pipeline a file is named by a *logical path* whose first segment is its tree:
`romfs/<path>`, `exefs/<name>` or `exefs_patches/<BUILD_ID>.ips`. Transformers produce
logical paths; only packagers know where they end up on disk.

### Transformers

A transformer turns one input file into one output file, in two phases
(`bea/transformers/base.py`):

- `plan(context)` is cheap and read-only. It looks at headers and at the profile.
- `transform(src, dst, config)` executes one action and receives only the action's
  parameters, never the profile. Its result therefore depends on nothing but the source
  file and those parameters, which is what makes caching correct.

| Transformer | Handles | Notes |
| :-- | :-- | :-- |
| `texture` | loose `.png`, `.tga`, `.dds` | Dropping top mip levels (DDS, any pixel format, lossless) or decode / resize / re-encode. Formats it cannot re-encode are reported as unsupported. |
| `config` | text files selected by profile rules | Line-preserving INI edits, JSON edits, regular-expression edits; verified at plan time. |
| `media` | video files selected by profile rules | Runs a user-installed ffmpeg; skipped with a hint when it is missing. |
| `binary` | ExeFS modules with patches for their exact build ID | Verifies every hunk against the decompressed module, then writes `<BuildID>.ips` (or a patched module in `replace` mode). |

A `Skip` carries a kind: `unsupported` (this version cannot do it), `excluded` (the
profile says so), `invalid` (unreadable file) or `mismatch` (the profile does not fit the
game; this one makes the build fail).

### Caching

An action is up to date when its cache key is unchanged and its staged file still has the
recorded size and modification time. The key is the SHA-256 of: transformer name and
version, the action's parameters, the source's logical path, size and modification time,
and the output path. State lives in `work/<TitleID>/state.json`. Bump a transformer's
`version` whenever its output for the same inputs would change. `--force` ignores the
cache.

### Packaging

`LayeredFSPackager` maps logical paths to one of two layouts:

| Logical path | `atmosphere` layout | `flat` layout |
| :-- | :-- | :-- |
| `romfs/<path>` | `atmosphere/contents/<TitleID>/romfs/<path>` | `<name>/romfs/<path>` |
| `exefs/<module>` | `atmosphere/contents/<TitleID>/exefs/<module>` | `<name>/exefs/<module>` |
| `exefs_patches/<BuildID>.ips` | `atmosphere/exefs_patches/<name>/<BuildID>.ips` | `<name>/exefs/<BuildID>.ips` |

`<name>` is the profile's `binary.patchset_name`, by default `bea-<TitleID>`. A manifest
(`.bea-package.json`) lists every file BEA put in the output folder; a later run removes
only files from that list. Next to the package BEA writes `bea_build.json` (what was
built) and a notice that the output must not be redistributed.

## Executable patches

- A patch belongs to exactly one build of one module, identified by the module's build ID
  (the 32-byte module ID of the NSO header, written as upper-case hex without trailing
  zero bytes).
- A hunk replaces bytes at a *module memory offset* (the address a disassembler shows
  minus its image base). Before anything is written, BEA checks the SHA-256 of a small
  window of the user's decompressed module (`expected_sha256`). Profiles in this
  repository never contain original bytes.
- All enabled patches of a module are verified first. If any fails, nothing is written for
  that module and the build is marked as failed.
- The IPS record offset is the memory offset plus `0x100`, the convention of every loader
  that applies such patches. See [FORMATS.md](FORMATS.md).
- BEA patches only modules found in the user's own game folder and never system titles.

## Reports

| File | Written by | Content |
| :-- | :-- | :-- |
| `backport_report.json` | `bea analyze` | Inventory of the game and a summary of the plan. |
| `out/<TitleID>/bea_build.json` | `bea build` | Every action and its status, every skip and its reason, the packaged files. |

Both carry a `schema` number, the tool version, the input identity (title ID, build IDs)
and a `limitations` list stating what was not looked at or not verified. Strings that
come from game files (paths, configuration values) are data: a consumer must never treat
them as instructions. Reports describe a game's files and stay on your machine.

## Guardrails (`tests/test_arch_rules.py`)

1. No title ID, no build ID or long hex blob, no path into `profiles/`, and nothing a
   profile declares (IDs, names) anywhere under `bea/`.
2. The repository contains only small UTF-8 text files of an allowed kind.
3. Profile directories hold only YAML and Markdown, are named after a title ID, load
   without problems, and contain no original game bytes.
4. `.gitignore` keeps the patterns that shield dumps, keys and outputs.
5. Imports respect the layer table and the dependency allowlist.

Every detector is also run against a planted violation, so a rule cannot pass vacuously.

## Extending BEA

| You want to | Do this |
| :-- | :-- |
| Support a game | Add `profiles/<TitleID>/` (start with `bea profile init`). No code. |
| Read a new file format | Add a module under `bea/formats/` with synthetic tests; record the facts and their public source in `FORMATS.md`. |
| Transform a new kind of asset | Add a `Transformer` under `bea/transformers/`, register it in `bea/transformers/__init__.py`, and expose its settings through the profile schema and loader. |
| Target another folder layout | Add a `Packager` under `bea/packagers/`. |
| Use a new external program | Declare it in `bea/toolbox.py`; locate it at run time, never bundle it. |

## Non-goals

BEA does not read packaged dumps, decrypt anything or handle keys; it does not rebuild
installable packages; it does not patch system software; and it does not accept patches
whose purpose is to defeat a protection, a licence or entitlement check, an online check
or a platform restriction. See [ROADMAP.md](ROADMAP.md) for the reasoning.
