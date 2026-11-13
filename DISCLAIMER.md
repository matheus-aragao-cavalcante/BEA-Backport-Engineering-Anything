# Disclaimer

BEA (Backport Engineering Anything) is a free and open-source command-line tool. This
document says what BEA is for, what the project refuses to do, and which responsibilities
stay with you when you use it.

It states project policy. It is not legal advice, and the maintainers cannot tell you
what the law allows in your situation.

## The short version

- BEA takes an already extracted folder of a game you own. It writes a separate mod
  folder and never writes into the input folder.
- BEA does not obtain, unpack or decrypt games. It reads no keys and uses no network.
- This repository holds source code, tests, documentation and declarative profiles. It
  holds nothing taken from any game.
- The output is made from your own copy of the game and is for you alone. Share
  profiles, never output.
- Running modified game data may breach the terms of the platform. It can lead to
  sanctions against your account or your console, and to lost save data. That risk is
  yours.
- The law of your country and the agreements you accepted decide whether you may extract
  or modify a game you own. A clean repository does not decide it for you.

## What BEA is for

BEA adapts the data of a Nintendo Switch game that you own. Its purpose is performance
and compatibility on hardware the game already targets.

You give BEA a game folder that is already extracted, and a profile for that game. BEA
analyzes the folder and then:

- makes loose textures smaller (PNG, TGA, DDS);
- optionally transcodes videos, using an `ffmpeg` that you installed yourself;
- edits configuration files;
- checks small, declared byte patches against your own executable and writes them as an
  IPS file.

The result is a mod folder in one of two layouts: LayeredFS, or a flat layout for generic
mod loaders.

BEA is meant for:

- personal use, with your own copy of a game on your own hardware;
- study, because the engine, its tests and its documentation show how such a pipeline is
  built.

You do not need a game to try BEA or to work on it. The `bea synth` command writes a
synthetic sample folder. All of its content is generated; none of it comes from a real
game.

## What BEA does not do

These are design decisions, not gaps. Nothing in this list is planned.

- **No acquisition.** BEA never downloads anything. It makes no network access and has no
  telemetry.
- **No unpacking and no decryption.** The only input is a folder that is already
  extracted. BEA does not take packaged dumps (`.nsp`, `.xci`, `.nsz` and similar
  formats) as input. It does not unpack them and it decrypts nothing.
- **No keys.** BEA has no code that reads console keys, and it never asks for any.
- **No repackaging.** BEA does not rebuild installable packages.
- **No work on protections.** BEA contains no code written to remove, weaken or get
  around a protection, and the project accepts no profile that does. The exact limits
  are listed under [Profiles](#profiles).

BEA expects a folder you already extracted from a game you own; producing that folder is
outside the scope of this project.

Support has the same limits. The maintainers do not assist anyone in obtaining games,
keys or system software, in modifying a console, or in getting around a protection.
Requests of that kind are closed without an answer, wherever they are made.

## What is in this repository, and what never is

The repository contains:

- the source code of the engine (`bea/`), which is game-agnostic and names no game;
- tests, which use synthetic data only;
- documentation, including instructions for coding agents;
- profiles (`profiles/<TitleID>/`): declarative YAML data and notes, one folder per game.

It never contains:

- anything taken from a game: code, assets, text, lists of its files, disassembled or
  decompiled code, and anything derived from these;
- keys or any other secret of a console or a game;
- system software;
- screenshots, logos or artwork;
- generated patch files, prebuilt mods or third-party programs;
- links or pointers to sources of games, keys or system software, or guides for
  extracting games or modifying a console.

Profiles in this repository hold zero bytes of any game. A patch site is not described by
a copy of the original bytes. It is described by the SHA-256 hash of a small window of
the executable (`expected_sha256`). BEA computes the same hash from your own executable
and compares the two. The replacement bytes are written by the contributor of the
profile.

The engine also accepts literal original bytes (`expected_bytes`), so that you can keep a
private profile in that form. The tests of this repository reject them, so no profile
published here contains any.

BEA is developed clean-room. Its code is based on public documentation and on the
project's own format notes ([docs/FORMATS.md](docs/FORMATS.md)). No code is copied from
other projects, whatever their license. No leaked SDK and no material under a
non-disclosure agreement is used. Contributors sign off every commit under the Developer
Certificate of Origin.

Tests that run on every pull request enforce part of these rules
(`tests/test_arch_rules.py`). The rest depends on contributors and reviewers, who follow
[CONTRIBUTING.md](CONTRIBUTING.md). If forbidden content slips through anyway, the
procedure in [SECURITY.md](SECURITY.md) applies.

## Your input

You supply the game folder, and you are responsible for it.

- **Ownership.** Use BEA only on a game you own, and only on a folder that you extracted
  yourself from your own copy.
- **No verification.** BEA cannot check where a folder comes from or whether you own the
  game. It asks for no proof, because it could not verify one. A run that succeeds says
  nothing about whether it was permitted.
- **Law and agreements.** Whether you may extract a game you own, keep the extracted
  files or change them depends on the law where you live and on what you agreed to, such
  as the terms of the platform and the license of the game. Laws differ from country to
  country and change over time. Owning a game does not settle the question, and a rights
  holder or a platform operator may see it differently than you do.
- **Agents.** If a coding agent runs BEA for you, what it does at your request is your
  responsibility as well.

### What a clean repository does not change

This repository is kept free of game content, and BEA decrypts nothing. Neither fact
makes an input lawful. If a folder was not lawfully made or obtained, running BEA on it
does not repair that.

If you are not sure about your own situation, find out before you use BEA.

## Your output

Everything BEA writes into an output folder is derived from your own copy of the game.
Smaller textures, transcoded videos and edited configuration files are altered copies of
the game's files.

- The output is for your personal use. Do not upload it, pass it on or sell it.
- BEA writes a DO_NOT_DISTRIBUTE notice into every output folder as a reminder.
- The generated patch file is covered too. It adds nothing to the profile it was built
  from, so there is nothing to gain by passing it on.
- The same care applies to everything else BEA writes from a real game: the reports,
  which list the game's files, and the memory image written by `bea nso image`, which is
  game code. Keep them on your machine.

To share your work, share the profile. A profile holds the instructions, not the result:
with it, other people can build the same mod from their own copy. The format is described
in [docs/PROFILES.md](docs/PROFILES.md).

## Risks

The output is only useful on a system that loads modified game data. The software that
does this is not part of BEA. BEA does not provide it, install it or explain it.

- **Platform terms.** Running modified software may breach the terms you accepted for
  your console, your account or your games. A platform operator can respond with
  sanctions against an account or a console. BEA cannot predict, detect or prevent that.
- **Data loss.** A modified game can crash, misbehave or damage its save data. A patch
  can apply cleanly and still not work. Back up whatever you cannot afford to lose.
- **No warranty.** Neither BEA nor any profile comes with a warranty for your console,
  your save data or your accounts. See [Warranty and liability](#warranty-and-liability).

You decide whether these risks are acceptable. If they are not, do not use the output.

## Profiles

A profile is the data BEA needs for one game: which files to change, with which settings,
and which byte patches to verify and write. Profiles are declarative YAML under
`profiles/<TitleID>/`. They hold data only, never code.

Profiles are contributed by the community. They record what contributors observed on
their own copies of a game. The project can check the form of a profile automatically.
It cannot test a profile against a game, because no game data exists in the repository
or in its tests. A profile therefore comes with no guarantee that it is correct, complete
or right for your version of a game.

BEA lowers one risk for you. It patches an executable only when the build ID of that
executable is declared in the profile and every verification window matches. If a single
check fails, that executable is not patched at all. This keeps a patch away from every
build except the one it was written for. It does not show that the patch does what its
description says.

The project accepts patches for one purpose only: performance and compatibility on
hardware the game already targets. That covers:

- frame rate, resolution, dynamic resolution and render scale;
- level of detail, shadows and draw distance;
- texture budget and memory budget;
- post-processing;
- performance fixes.

The project never accepts, whatever reason is given:

- anything that touches signature, license, ticket, entitlement, DLC, online, account,
  ban, telemetry, anti-tamper or integrity checks;
- region locks;
- platform, hardware-model or firmware-version gates;
- permission (NPDM) changes;
- cheats and content unlocks;
- removal of legal screens;
- any system module.

When it is unclear whether a patch stays within these limits, it is not merged. The rules
for contributing a profile are in
[CONTRIBUTING.md](CONTRIBUTING.md#contributing-a-profile).

These limits describe what this repository accepts and what the maintainers support. A
profile that someone keeps outside this repository is that person's responsibility. The
project does not support it.

## Third-party software and names

- **Python packages.** BEA depends on Pillow (MIT-CMU), PyYAML (MIT) and lz4
  (BSD-3-Clause). Your package manager installs them, and each keeps its own license. See
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **External programs.** `ffmpeg` and `ffprobe` are optional. BEA looks for them on your
  system at run time and never bundles or downloads them. You obtain them yourself and
  follow their license, which is LGPL-2.1-or-later or GPL depending on the build.
- **Folder layouts.** The LayeredFS output uses the directory names read by the
  Atmosphere custom firmware: `atmosphere/contents/<TitleID>/romfs` and
  `atmosphere/exefs_patches/<name>/<BuildID>.ips`. The flat output (`<name>/romfs`,
  `<name>/exefs`) is for generic mod loaders. These are folder layouts and nothing more.
  BEA contains no code from the software that reads them.

Names of products, companies, formats and tools appear in this repository only to say
what something is or what it works with. A mention does not mean that the owner of the
name approves of BEA. It does not mean that BEA recommends the product either.

Nintendo Switch is a trademark of Nintendo. All other names belong to their owners. BEA
is not affiliated with or endorsed by any of them.

## Warranty and liability

BEA is released under the MIT License. The warranty disclaimer and the limitation of
liability that apply to BEA are the ones in the [LICENSE](LICENSE) file. This document
does not extend them and does not restrict them. It gives no additional warranty, and it
takes away no right that the license or the law gives you.

## How this document relates to the license

This document states project policy and support boundaries: what the maintainers build,
what they accept into this repository, and what they help with. It is not part of the
license.

- It adds no conditions to the MIT License and removes none of its permissions.
- Phrases such as "never accepts" and "does not support" describe what happens in this
  project. They are not license terms.
- The request not to distribute output is not a license term either. It follows from
  what the output is: files derived from a game. The MIT License covers BEA. It cannot
  give you rights in a game or in files derived from one.

The license lets anyone use, change and redistribute BEA, including in ways this project
would not accept. This document cannot prevent that and does not claim to. It says what
this project stands behind.

## Removal requests from rights holders

If you hold rights in a game and want a profile for it removed from this repository, ask
the maintainers. You can do the same for anything else here that concerns your work.

1. Open an issue in this repository; any issue form will do. If you prefer a private
   channel, use [GitHub's private vulnerability reporting](https://github.com/matheus-aragao-cavalcante/BEA-Backport-Engineering-Anything/security/advisories/new), as
   described in [SECURITY.md](SECURITY.md), and say that it is a removal request.
2. Name the profile (its folder under `profiles/`) or the file, and say whom you
   represent.

A request is credible when it identifies the material and plausibly comes from the rights
holder or from someone who acts for them. On a credible request the maintainers remove
the profile without debate. They do not ask you to argue your case first. The project
cannot promise a response time.

The project keeps profiles free of game content, but removal does not depend on that
question. The process is voluntary and offered in good faith. It does not replace any
formal procedure, and you do not have to use it before you use one.

If you find game content, keys or other forbidden material in this repository, follow
[SECURITY.md](SECURITY.md).

## Scope of this document

This document applies to every version of BEA and to every channel through which the
project distributes it. It applies whether you got BEA as a source checkout, a release
archive or an installed package. A change to this document never changes the license of
code that was already released.

Forks, copies and repackaged builds made by other people are not this project. The
maintainers do not review, endorse or support them, even when they keep the name or
repeat this text. A fork may behave differently from what is described here.
