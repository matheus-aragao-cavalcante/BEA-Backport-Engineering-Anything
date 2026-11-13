# Contributing to BEA

Thank you for your interest in BEA (Backport Engineering Anything). BEA is a free and
open-source command-line tool, published under the [MIT license](LICENSE). It takes a
game folder that you have already extracted, applies a per-game profile to it and writes
a mod folder.

This document says what the project accepts, which rules every contribution follows and
how a change gets reviewed. Two ideas run through all of it:

- nothing from a game ever enters this repository;
- the engine never names a game: everything about one game is data in a profile.

## Contents

- [What belongs in BEA](#what-belongs-in-bea)
- [What BEA will never do](#what-bea-will-never-do)
- [The hard rules](#the-hard-rules)
- [Development setup](#development-setup)
- [Clean-room policy](#clean-room-policy)
- [Contributing a profile](#contributing-a-profile)
- [Changing the engine](#changing-the-engine)
- [Changing the documentation](#changing-the-documentation)
- [Issues and bug reports](#issues-and-bug-reports)
- [AI-assisted contributions](#ai-assisted-contributions)
- [Commits and sign-off](#commits-and-sign-off)
- [Review checklist](#review-checklist)
- [Report only what you ran](#report-only-what-you-ran)

Related documents:

- [CLAUDE.md](CLAUDE.md): the short list of rules that hold for every change, for people
  and for AI agents.
- [AGENTS.md](AGENTS.md): the map of the code, and a table that says where each kind of
  change goes.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the engine is organized.
- [docs/PROFILES.md](docs/PROFILES.md): the reference for every key of a profile.
- [docs/PROFILE_AUTHORING.md](docs/PROFILE_AUTHORING.md): a walk through writing a
  profile.
- [docs/FORMATS.md](docs/FORMATS.md): the project's format notes and their public
  sources.
- [docs/ROADMAP.md](docs/ROADMAP.md): what comes next and what is out of scope on
  purpose.
- [DISCLAIMER.md](DISCLAIMER.md): what BEA is for and which responsibilities stay with
  the user.
- [SECURITY.md](SECURITY.md): how to report a vulnerability or forbidden content.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): dependencies, external programs and
  their licenses.

## What belongs in BEA

BEA analyzes an extracted game folder (an ExeFS folder, a RomFS folder or both) and
applies a profile to it. It downsizes loose textures (PNG, TGA, DDS), optionally
transcodes videos with an ffmpeg that the user installed, edits configuration files, and
verifies small declared byte patches against the user's own executable before it writes
them as an IPS file. The result is a mod folder in the LayeredFS layout, or in a flat
layout for generic mod loaders.

Contributions that fit:

- fixes and improvements to the engine in `bea/` that work for any game;
- profiles for games you own, written as data under `profiles/<TitleID>/`;
- tests, documentation, clearer messages and better reports;
- bug reports that another person can reproduce.

Keep a pull request small and about one thing. Propose a profile with the profile request
form. For an engine change that is larger than a fix, open the pull request early, as a
draft, and fill in its description, so that scope and design are settled before the work
is finished.

## What BEA will never do

These are project decisions, not open questions. A pull request that adds any of them is
closed, however well it is written. [docs/ROADMAP.md](docs/ROADMAP.md) records the
reasoning.

- **Packaged dumps, decryption and keys.** The only input is a folder that was already
  extracted. BEA does not take packaged dumps (`.nsp`, `.xci`, `.nsz` and similar) as
  input, decrypts nothing and has no code that reads console keys. None of this is
  planned.
- **Installable packages.** BEA writes a mod folder. It does not rebuild installable
  packages, and that is not planned either.
- **Network access.** BEA never downloads anything, has no telemetry and makes no
  network access.
- **Patches outside performance and compatibility.** A patch may only change how a game
  performs on hardware that the game already targets. The accepted subjects are frame
  rate, resolution, dynamic resolution, render scale, level of detail, shadows, draw
  distance, texture budget, memory budget, post-processing and performance fixes.

Never accepted, in the engine or in a profile:

- anything that touches a signature, license, ticket, entitlement, DLC, online, account,
  ban, telemetry, anti-tamper or integrity check;
- region locks;
- platform, hardware-model or firmware-version gates;
- permission (NPDM) changes;
- cheats and content unlocks;
- removal of legal screens;
- any system module.

Making such a feature optional, hiding it behind a flag or moving it into a plug-in does
not change the answer.

### Unresolved means not merged

BEA fails closed. When it is not settled whether a contribution is in scope, or whether
it is lawful, the contribution is not merged. Passing checks, a popular request or a
quiet review thread do not settle the question. Ask before you invest: use the profile
request form for a profile and an early draft pull request for an engine change, and wait
for a maintainer's answer.

## The hard rules

The rules fit on one screen, and none of them has exceptions. They apply to everything
that reaches the repository or its issue tracker: code, profiles, tests, documentation,
comments, commit messages, CI logs, issues, pull requests and release files.

1. **Zero game content.** No game file and no part of one: no executables, assets, text,
   file listings, hex dumps of original bytes, screenshots or other captures.
2. **No keys.** No console, title or game keys, no key files, no code that looks for
   keys.
3. **No firmware.** No system software, system data or SDK binaries.
4. **No circumvention functionality.** No decryption. No bypass of a signature, license,
   permission or integrity check. No patch for a system module.
5. **No links and no how-tos** for downloading or dumping games, for obtaining keys or
   firmware, or for modifying a console. Encoded links and "search for ..." hints count.
   Tools made for those purposes are not named.
6. **No copied third-party code.** Nothing copied, translated or adapted from another
   project, whatever its license.
7. **No leaked or confidential material.** Nothing from a leaked SDK or development kit,
   from a document under a non-disclosure agreement, or from leaked or proprietary
   source code.
8. **No decompiled or disassembled game code.** No decompiler output, no disassembly
   listings, no symbol or string dumps. Describe behavior in your own words.
9. **No unreleased builds.** Profiles describe commercially released builds only, never
   a leaked or pre-release build.
10. **No binary artifacts.** The repository holds small text files only: no `.ips` files,
    mod folders, archives, images or bundled tools.
11. **Synthetic test fixtures only.** Test data is built by the test itself or by the
    generator behind `bea synth`. It is never derived from a real game file, not even
    from a header.
12. **Text-only nominative references.** A third-party name appears as plain words, and
    only where it is needed to say what something is for. No logos, artwork or
    look-alike branding.
13. **No monetization tied to game support.** No paid early access, no paywalled
    profiles, no paid priority for a game.
14. **No telemetry.** BEA collects nothing, reports nothing and contacts no network.

The sections below explain the rules that need more than a line.

## Development setup

You need Python 3.12 or newer and the `uv` package manager. Run the commands from the
root of the repository.

```console
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`uv sync` creates the environment and installs BEA with its development tools. The other
four commands are the checks that every pull request must pass: the tests, the linter,
the formatter in check mode and mypy in strict mode. `uv run ruff format .` fixes the
formatting. CI runs the same four checks on Linux and Windows, with Python 3.12, 3.13 and
3.14.

### Trying BEA without a game

No step of development needs a real game. `bea synth` generates a synthetic,
copyright-free sample game and, with `--profiles-dir`, a profile that matches it:

```console
uv run bea synth .gamedump/sample --profiles-dir .gamedump/sample-profiles
uv run bea build .gamedump/sample --profiles-dir .gamedump/sample-profiles --dry-run
uv run bea build .gamedump/sample --profiles-dir .gamedump/sample-profiles
uv run bea doctor
```

The first command writes the sample game and its profile. The second prints the build
plan and writes nothing. The third writes the mod folder under `out/`. `bea doctor` shows
the versions in use and whether the optional programs `ffmpeg` and `ffprobe` were found.
Tests that need those programs are skipped when they are missing.

Keep generated folders in `.gamedump/`, `work/` and `out/`. All three are git-ignored.
The architecture test inspects every file of the checkout that git does not ignore, so a
sample game created anywhere else in the repository makes that test fail.

## Clean-room policy

BEA is written from public documentation and from the project's own format notes in
[docs/FORMATS.md](docs/FORMATS.md). It contains no code from any other project, whatever
that project's license. Leaked SDKs and material under a non-disclosure agreement are
never used. Tests use synthetic data only.

### Two roles

The policy keeps two kinds of work apart.

- **Research.** A person or an AI agent who has read another implementation of a format
  writes down facts, in prose: the size of a field, the meaning of a value, the order of
  two steps. The facts go into `docs/FORMATS.md`, each with the public source that
  confirms it. No code, no pseudo-code and no copied comments.
- **Implementation.** The person or agent who writes the code works only from
  `docs/FORMATS.md` and from public documentation, and does not open other
  implementations.

If you have read the source of another implementation of something, you are a researcher
for that thing: contribute notes, and let someone else write the code.

### Inputs

Allowed:

- public format descriptions, specifications and standards;
- the facts recorded in `docs/FORMATS.md`;
- what you observe in the files of a game that you lawfully own, written down as facts
  and never as copied bytes;
- synthetic files that you generate yourself.

Not allowed:

- the source code of another project as something to copy, translate or closely
  paraphrase, under any license, permissive ones included;
- leaked SDKs or development kits, documents under a non-disclosure agreement, leaked or
  proprietary source code;
- decompiled or disassembled code of a game, of system software or of a proprietary
  tool;
- code that an AI assistant produced from third-party code it was given.

Finding a patch site in your own copy of a game is a different activity. The profile
rules below say what may leave your machine in that case.

### Every pull request states its sources

The pull request template has a "Sources" section. List the public documents you
consulted (title and section), or the part of `docs/FORMATS.md` you worked from. "None
beyond this repository" is a valid answer for a refactoring. A pull request that does not
state its sources is not merged.

### When copied material is found

1. **Revert.** The change is reverted as soon as the problem is known.
2. **Purge.** The material is removed from the history of the repository and from any
   release files.
3. **Reimplement.** Someone who has not seen the copied material writes the feature
   again, from `docs/FORMATS.md` and public documentation.

If you suspect that something in the repository was copied, report it the way
[SECURITY.md](SECURITY.md#forbidden-content-in-the-repository) describes: privately, by
saying where it is, without quoting it. An honest mistake is handled with the same three
steps, and nobody is blamed for reporting one.

### What this policy does and does not do

The policy lowers the risk that BEA contains code that belongs to someone else. It does
nothing more than that: it is not a legal safe harbor, and it answers no other legal
question. It is an engineering practice, not legal advice.

## Contributing a profile

A profile is everything BEA knows about one game, written as data: which textures to
downsize, which videos to transcode, which settings to edit and which byte patches to
apply. [docs/PROFILES.md](docs/PROFILES.md) is the reference for every key, and
[docs/PROFILE_AUTHORING.md](docs/PROFILE_AUTHORING.md) walks through the work. This
section holds the rules for profiles that live in this repository.

BEA expects a folder you already extracted from a game you own; producing that folder is
outside the scope of this project. Keep the folder under `.gamedump/`, which is
git-ignored.

```console
uv run bea inspect <game folder>
uv run bea profile init <game folder>
uv run bea nso hunk <ExeFS folder>/main --offset <offset> --patch "<your bytes, in hex>"
uv run bea profile validate profiles/<TitleID>
uv run bea build <game folder> --dry-run
```

- `bea inspect` prints the title ID and the build ID of every executable.
- `bea profile init` creates `profiles/<TitleID>/` with a valid skeleton: `profile.yaml`,
  `patches.yaml` and `NOTES.md`.
- `bea nso hunk` prints a ready-to-paste entry for one patch site. The entry holds your
  offset, your replacement bytes and `expected_sha256`, the SHA-256 of a small window
  (32 bytes by default) of your own executable around the site. The command also shows
  you the bytes that are there now. Do not commit those bytes and do not paste them
  anywhere.
- `bea profile validate` checks the files against the reference.
- `bea build --dry-run` shows what the profile would do to your folder and writes
  nothing.

### Rules for profiles in this repository

- **Data only.** A profile directory holds `.yaml`, `.yml` and `.md` files, each at most
  256 KiB. No code, no binaries, no assets, no keys.
- **One directory per title ID.** The directory is named after the title ID, 16 uppercase
  hexadecimal digits, and never after the game.
- **Zero bytes of the game.** A patch site is verified through `expected_sha256`, never
  through literal original bytes. The engine also accepts literal `expected_bytes`, for
  profiles that you keep private. The tests of this repository reject them.
- **Your own replacement bytes.** You write the bytes that a patch puts in. Do not copy
  patches from another collection, whatever its license. Derive the change from your own
  analysis.
- **All or nothing.** BEA patches an executable only when its build ID is declared in the
  profile and every verification window matches. Do not weaken this, in a profile or in
  the engine.
- **Accepted subjects only.** Frame rate, resolution, dynamic resolution, render scale,
  level of detail, shadows, draw distance, texture budget, memory budget, post-processing
  and performance fixes, on hardware that the game already targets. Every patch states
  one of these in its `category` key (the values are listed in
  [docs/PROFILES.md](docs/PROFILES.md)); the tests reject a patch without one. The list
  of what is never accepted is under [What BEA will never do](#what-bea-will-never-do).
- **A description for every patch, in your own words.** Say what the patch changes and
  why. No disassembly listings, no decompiler output, no strings copied from the game.
- **A notes file.** `NOTES.md`, next to the YAML, records the hardware you tested on, the
  game version, what you tested and the result. A patch that applies has not yet been
  shown to work: say which of the two you checked.
- **Released builds only.** No profile for a leaked or pre-release build.
- **Evidence is text.** Show that a patch works with measurements (frame rate, memory
  use, sizes before and after) and with logs. Never with game files, screenshots or video
  captures.
- **Lawful ownership.** By opening the pull request you attest that you analyzed a copy
  of the game that you lawfully own. The pull request template asks you to confirm it.

Maintainers may remove any profile on a credible request, for example from a rights
holder, without a debate first.
[DISCLAIMER.md](DISCLAIMER.md#removal-requests-from-rights-holders) describes that
process.

### What stays on your machine

Everything BEA writes from a real game is derived from your copy of that game: the mod
folder with everything in it, the reports, and the memory image written by
`bea nso image`. It is for your personal use. Never commit it, attach it or redistribute
it. BEA writes a DO_NOT_DISTRIBUTE notice into every output folder as a reminder. Share
the profile, not the output.

## Changing the engine

The engine is the Python package in `bea/`. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
describes its design, and [AGENTS.md](AGENTS.md) says where each kind of change goes.
[tests/test_arch_rules.py](tests/test_arch_rules.py) enforces the rules below. That test
is part of the suite and may not be weakened to let a change through. Extending it for a
new module or an accepted dependency is normal. Loosening an existing check is not.

### The engine is game-agnostic

Nothing under `bea/` names a game: no title ID, no build ID or other long hexadecimal
string, no game name, no branch for one game and no path into `profiles/`. The engine
receives the profile directory as a parameter. When a game needs something that the
engine cannot do, add a general capability that any profile can declare.

### Layers

Low-level modules never import high-level ones.

| Layer | Members of `bea` | May import from `bea` |
| :-- | :-- | :-- |
| foundation | `errors`, `hexutil`, `fsutil`, `globmatch` | nothing |
| shared model | `model`, `toolbox` | foundation |
| formats | `formats` | foundation |
| profile | `profile` | foundation |
| ingest | `ingest` | foundation, `model`, `formats` |
| transformers | `transformers` | foundation, `model`, `toolbox`, `formats`, `profile` |
| packagers | `packagers` | foundation, `model` |
| orchestration | `pipeline`, `analyze`, `synth`, `scaffold` | all but the command line |
| command line | `cli`, `__main__` | everything |

The table in the test (`LAYERS`) is the authoritative one. Imports inside `bea/` are
absolute, as in `from bea.errors import BeaError`. The only third-party packages that the
engine may import are the declared dependencies: Pillow, PyYAML and lz4.

### Format knowledge

New knowledge about a file format goes into [docs/FORMATS.md](docs/FORMATS.md) first, in
your own words, with the public source of each fact. The code is then written from those
notes.

### Dependencies and external programs

A new dependency needs three edits in one pull request: an entry in `pyproject.toml`, a
row in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and, for a package that `bea/`
imports, an entry in the import allowlist of the architecture test. Say in the pull
request why the standard library is not enough.

External programs such as ffmpeg are located at run time. They are never bundled and
never downloaded.

### Tests

- Tests use synthetic data only: bytes built inside the test, or the generator behind
  `bea synth`.
- A test must fail without your change. Run it once against the old code to be sure.
- A test that needs a real game cannot exist in this repository.
- Tests that need ffmpeg carry the `ffmpeg` marker and skip themselves when the program
  is missing.

### Behavior to keep

- BEA never writes into the input folder.
- Patch verification is all-or-nothing per executable.
- When BEA cannot read or process something, it reports that (`unsupported`, `invalid`,
  `mismatch`). It never guesses and never drops an item without a word.

## Changing the documentation

- Write plain English in short sentences. Say what BEA does and what it does not do.
- Link to other files of the repository with relative links.
- The repository holds text files only, so the documentation has no screenshots, logos or
  other images.
- Rule 5 applies in full: no links, pointers or instructions for obtaining games or keys,
  for dumping, or for modifying a console.
- Wrap prose at about 90 columns. Files use LF line endings and end with a newline.

## Issues and bug reports

The issue tracker has two forms: a bug report and a profile request. Blank issues are
disabled.

A useful bug report says what happened, what you expected, the steps that reproduce the
problem, the exact command line, the output of `bea doctor --json` and your operating
system. The best reports reproduce the problem with the sample game from `bea synth`,
because then everyone can run the same steps.

You may attach or paste text only:

- command lines and what BEA printed;
- the output of `bea doctor --json`;
- short, redacted excerpts of a report (`backport_report.json`, `bea_build.json`) or of a
  log. Reports list the files of a game, so keep the few lines that matter and remove the
  rest.

Never attach or paste game files, extracted files, keys, dumps, files that BEA produced
from a game, or screenshots and other captures of a game. Maintainers remove such
material when they see it.

Questions about obtaining games or keys, or about modifying a console, are outside the
scope of this project. They are closed without help.

Do not report a security problem in a public issue. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/matheus-aragao-cavalcante/BEA-Backport-Engineering-Anything/security/advisories/new).
[SECURITY.md](SECURITY.md) has the details.

## AI-assisted contributions

Contributions written with the help of an AI assistant are welcome. They follow the same
rules as every other contribution, and these as well:

- **A named human is responsible.** One person reviews the final diff, understands it and
  answers for it. That person signs off the commits and ticks the boxes of the pull
  request template. An assistant does neither on its own.
- **Never paste third-party code into a prompt** to have it transcribed, translated or
  rewritten. The result is a copy made by other means, and rule 6 applies to it. The same
  goes for game data, disassembly and decompiler output.
- **Give the assistant the inputs that an implementer may use**: this repository,
  `docs/FORMATS.md` and public documentation.
- **Report honestly.** A check that the assistant did not run is not reported as passing.

AI agents that work in this repository follow [CLAUDE.md](CLAUDE.md) and
[AGENTS.md](AGENTS.md).

## Commits and sign-off

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): a
type, an optional scope and a short summary.

```text
feat: add an option to the build command
fix(formats): reject a truncated header
docs: describe the notes file of a profile
```

Common types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci` and
`chore`. The hard rules apply to commit messages too.

Every commit is signed off under the
[Developer Certificate of Origin](https://developercertificate.org/) (DCO):

```console
git commit -s
```

The `-s` option adds a `Signed-off-by:` line with the name and e-mail address from your
git configuration. With that line you certify, in short, that:

- you have the right to submit the contribution under the license of this repository,
  because you wrote it or because its origin allows it;
- you understand that the contribution and your sign-off are public and are kept with the
  project.

The text of the certificate is the authoritative one; read it once. The certificate also
provides for work that builds on other people's open-source code. BEA is stricter than
that: rule 6 excludes third-party code whatever its license.

The sign-off does not transfer copyright. Contributions are accepted under the MIT
license of this repository.

A commit that lacks the line can be fixed with `git commit --amend -s`. For several
commits, `git rebase --signoff <base>` adds the line to each of them.

## Review checklist

A reviewer checks these points. Check them yourself before you ask for a review.

- The change is in scope, and no question about scope or lawfulness is open.
- The diff, the commit messages and the description respect the hard rules. In
  particular, they contain no game content.
- The pull request states its sources, and nothing was copied from another project.
- Engine changes stay game-agnostic and respect the layers. New format facts are in
  `docs/FORMATS.md` with their public source.
- New and changed tests use synthetic data and fail without the change.
- Profiles validate, use hash windows instead of original bytes, stay within the accepted
  subjects, describe every patch in the contributor's own words and come with
  `NOTES.md`.
- A new dependency appears in `pyproject.toml`, in `THIRD_PARTY_NOTICES.md` and, where
  needed, in the import allowlist of the architecture test.
- The four checks pass, and the reported results are real.
- Every commit is signed off and follows Conventional Commits.
- The documentation matches the new behavior.
- For AI-assisted work, a named human reviewed the final diff and answers for it.

## Report only what you ran

In the pull request, list the commands you ran and what they printed. Do not write "all
checks pass" from memory or from an earlier run.

- A check that you did not run is reported as not run, with the reason: no ffmpeg on the
  machine, a platform you do not have, a test you could not finish.
- A check that failed is reported as failed, even when you think that the failure has
  nothing to do with your change.
- Say what you did not test. For a profile, say which hardware and which game versions
  you did not try.

Reviewers can work with an incomplete report. They cannot work with a wrong one.
