# Changelog

All notable changes to BEA are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (while the version is `0.x`, a minor release
may change behaviour). The version is defined once, in `bea/__init__.py`.

## [Unreleased]

Nothing yet. See [docs/ROADMAP.md](docs/ROADMAP.md) for what is planned and what is
known to be missing.

## [0.1.0] - 2026-11-13

First public version.

### Added

- **Command line** `bea`: `inspect`, `analyze`, `build` (with `--dry-run`, `--force`,
  `--jobs`, `--layout atmosphere|flat`, `--enable-patch` / `--disable-patch`),
  `profile validate`, `profile init`, `nso info`, `nso image`, `nso hunk`, `synth` and
  `doctor`. Every reporting command has `--json`. Also runnable as `python -m bea`.
- **Ingest** of an already-extracted game folder: ExeFS detected by content, RomFS by
  name, title ID from `main.npdm`, name and version from an optional `control.nacp`,
  build IDs from the module headers. Packaged dumps are recognized and refused.
- **Formats**, written from public documentation and tested on synthetic data only:
  NSO0 (LZ4 segments, hashes, build ID, memory image, uncompressed write-back),
  IPS / IPS32 (writer that every known loader accepts, strict reader, applier),
  NPDM, NACP (including the compressed title block), DDS (all common pixel formats,
  lossless mip dropping, writer, codec shim), image header probing (PNG, TGA, JPEG, BMP,
  KTX 1/2, ASTC, DDS), PFS0 listing (recognition only).
- **Transformers**: textures (PNG, TGA, DDS; mip dropping or resampling, power-of-two
  preservation, straight or alpha-weighted filtering), configuration files (INI, JSON,
  regular expressions; formatting, encoding and line endings preserved), media (ffmpeg
  located at run time), executable patches (all-or-nothing verification, IPS output).
- **Profiles**: strict YAML schema (`docs/PROFILES.md`) with every problem reported at
  once; executable patches verified by the SHA-256 of a window of the user's own module,
  so profiles hold no byte of a game; a fixed list of allowed patch categories.
- **Pipeline**: cached, atomic, parallel execution; stale outputs removed; LayeredFS
  packaging with a manifest so that only files BEA created are ever removed; build report
  (`bea_build.json`) and a do-not-distribute notice next to every package.
- **Synthetic sample game** (`bea synth`) to try and test BEA without any game.
- **Guardrails** (`tests/test_arch_rules.py`): no title IDs, build IDs, game names or
  key-file names in the engine; repository holds small text files only; repository
  profiles contain no game bytes and state a category; layered imports.
- **Documentation**: README (English, with a Brazilian Portuguese translation), architecture,
  profile reference and authoring guide, format notes with public sources, roadmap,
  disclaimer, security policy, contributing guide, third-party notices.
- **Agent skill** and Claude Code plugin manifest, GitHub issue forms, pull request
  template and CI (Linux and Windows, Python 3.12 to 3.14).

[Unreleased]: https://github.com/matheus-aragao-cavalcante/BEA-Backport-Engineering-Anything/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/matheus-aragao-cavalcante/BEA-Backport-Engineering-Anything/releases/tag/v0.1.0
