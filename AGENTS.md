# Architecture map for agents

This is the map. The rules that hold in every session are in [CLAUDE.md](CLAUDE.md); read
them first. Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The five rules, in one screen

1. Zero game content in the repository: no game files, keys, dumps, listings or bytes.
2. Real dumps live only in `.gamedump/<Game>/`; results in `work/` and `out/`.
3. `bea/` is game-agnostic: no title IDs, build IDs or game names. Games are YAML in
   `profiles/<TitleID>/`.
4. Clean-room: write from public documentation and `docs/FORMATS.md`; copy no code.
5. Patches are verified (hash windows, all-or-nothing) and limited to performance and
   compatibility. Nothing that defeats a protection, ever.

## What BEA does

```text
extracted game folder ──▶ ingest ──▶ plan ──▶ execute ──▶ package ──▶ out/<TitleID>/ (LayeredFS)
        profile ────────────┘         │
                                      └─▶ bea analyze / bea build --dry-run stop here
```

## Where things are

| Path | What it is |
| :-- | :-- |
| `bea/cli.py` | Commands and text output. No logic. |
| `bea/pipeline.py` | Plan, execute (cached, atomic), reconcile, package. |
| `bea/analyze.py` | Read-only report. |
| `bea/ingest/` | Folder on disk to `GameInput`. |
| `bea/formats/` | One module per file format, each with a synthetic test file. |
| `bea/profile/schema.py`, `loader.py` | Profile dataclasses and the strict YAML loader. |
| `bea/transformers/` | `texture`, `config`, `media`, `binary`; contract in `base.py`. |
| `bea/packagers/` | `layeredfs`; contract in `base.py`. |
| `bea/model.py` | `GameInput`, `ModuleInfo`, `FileEntry`, logical paths. |
| `bea/synth.py` | Synthetic sample game (also the fixture of end-to-end tests). |
| `profiles/` | Per-game data. `_template/` is the starting point. |
| `docs/PROFILES.md` | Normative reference of the profile YAML. |
| `docs/FORMATS.md` | Format facts with their public sources. |
| `tests/test_arch_rules.py` | The guardrails. Never weaken them. |

## Where does this go?

| Change | Place |
| :-- | :-- |
| A game needs different limits, edits or patches | `profiles/<TitleID>/`, never `bea/` |
| A game needs behaviour the engine lacks | A general, profile-driven capability in `bea/`, plus schema, loader and `docs/PROFILES.md` |
| A new file format | `bea/formats/<name>.py`, `tests/test_<name>.py`, a section in `docs/FORMATS.md` |
| A new kind of transformation | `bea/transformers/<name>.py`, registered in `bea/transformers/__init__.py` |
| A new output layout | `bea/packagers/<name>.py` |
| A new dependency | `pyproject.toml`, `THIRD_PARTY_NOTICES.md`, the allowlist in `tests/test_arch_rules.py` |

## Commands

```console
uv sync                              # once: create the environment
uv run pytest                        # tests (synthetic data only)
uv run ruff check . && uv run ruff format --check .
uv run mypy                          # strict typing of bea/
uv run bea synth .gamedump/sample --profiles-dir .gamedump/sample-profiles
uv run bea build .gamedump/sample --profiles-dir .gamedump/sample-profiles --dry-run
```

## Conventions

- Layers: a module imports only from lower layers (table in `docs/ARCHITECTURE.md`).
- Errors meant for the user derive from `bea.errors.BeaError` and say what is wrong,
  where, and what to do.
- File I/O goes through `bea.fsutil` (long Windows paths, atomic writes, sorted walks).
- A transformer's `plan` is read-only; `transform` depends only on its source file and its
  parameters. Bump `version` when output changes.
- Sizes and offsets read from files are validated before use: a hostile file produces a
  `FormatError`, never a crash.
- Tests build their inputs themselves and must fail without the change they cover.
- Report only checks you actually ran, with their real output.
