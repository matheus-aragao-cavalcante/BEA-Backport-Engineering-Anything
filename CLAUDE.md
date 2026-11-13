# Rules for AI agents and contributors

These rules hold in every session and for every change, with or without further context.
The architecture map, the commands and the "where does this go?" guide are in
[AGENTS.md](AGENTS.md) (@AGENTS.md).

## 1. Zero game content in the repository

- Nothing from a game ever enters the repository: no code, assets, dumps, keys, firmware,
  file listings, disassembly, decompiled code, or files derived from them. Not in commits,
  not in tests, not in docs, not in issues or pull requests.
- Tests use synthetic data only: bytes built inside the test, or the generator behind
  `bea synth`. A test that needs a real game is not a test this repository can have.
- Reports produced from a real game (`backport_report.json`, `bea_build.json`), logs and
  analysis artefacts stay on your machine. They are git-ignored; keep it that way.
- Never link to, or explain how to obtain, games, keys or firmware.

## 2. Game files live only in `.gamedump/`

- A real dump used for local testing goes in `.gamedump/<Game>/` at the repository root
  (git-ignored). Build state goes in `work/`, results in `out/` (both git-ignored). Never
  put game files anywhere else: not in `tests/`, not in a scratch or temp directory.
- BEA never writes into the input folder. Do not "fix" a dump in place.
- Before ending a session that created anything under `.gamedump/`, `work/` or `out/`,
  say which folders are left and why.

## 3. The engine is game-agnostic; games are data

- `bea/` never names a game: no title IDs (`0100…`), no build IDs, no game names, no
  per-game branches, no path into `profiles/`.
- Everything specific to a game is declarative data in `profiles/<TitleID>/`
  (`profile.yaml`, `patches.yaml`, notes). Profiles never contain code or binaries.
- When a game needs something the engine cannot do, add a general, profile-driven
  capability to the engine. A special case for one game is never the answer.
- `tests/test_arch_rules.py` enforces these rules. It is part of the suite and may never
  be weakened to let a change through.

## 4. Clean-room implementation

- Write code from public documentation and from the facts recorded in `docs/FORMATS.md`.
  Do not copy code from other projects, whatever their license, and never from GPL,
  unlicensed or leaked sources (SDKs, NDA material).
- New format knowledge goes into `docs/FORMATS.md` together with its public source.
- A new dependency needs an entry in `pyproject.toml`, in `THIRD_PARTY_NOTICES.md` and in
  the import allowlist of `tests/test_arch_rules.py`. External programs (ffmpeg, …) are
  located at run time and never bundled.

## 5. Patches are verified, narrow and honest

- A binary patch always carries `expected_bytes`. Verification is all-or-nothing per
  module and may not be relaxed: BEA never patches a build it was not written for and
  never writes a partial patch set.
- In scope: compatibility and performance (resolution, frame rate, memory, quality
  settings, asset size). Out of scope and never accepted: circumventing DRM, licence,
  signature or online checks; cheats for online play; anything whose purpose is piracy.
- A patch that applies is not yet a patch that works. Profiles record what was tested
  (hardware, game version, result) in a notes file next to the YAML.

## 6. Before saying "done"

```console
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

- All four pass. Report only checks you actually ran, with their real results.
- Unknown stays unknown: when BEA cannot read or process something it says so
  (`unsupported`, `invalid`, `mismatch`) instead of guessing or staying silent. Keep that
  property in every change.
- Commit messages follow Conventional Commits (`feat: …`, `fix(formats): …`).
