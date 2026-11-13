# Security policy

BEA (Backport Engineering Anything) is a command-line tool that reads files it has no
reason to trust. This document says which versions get security fixes, what BEA defends
against, what counts as a vulnerability and how to report one.

What the project does and refuses to do in general is described in
[DISCLAIMER.md](DISCLAIMER.md).

## Supported versions

| Version | Security fixes |
| :-- | :-- |
| Latest release | Yes |
| `main` branch | Yes |
| Older releases | No |

If you use an older release, update first and check whether the problem is still there.

## What BEA is from a security point of view

- **Local.** BEA runs on your computer, under your user account, with your permissions.
- **Offline.** BEA works only with files on your disk and never opens a network
  connection.
- **A parser of untrusted files.** BEA reads executables, textures, configuration files
  and metadata from a game folder, and YAML from a profile directory. Media files are
  handed to `ffmpeg` and `ffprobe`, which parse them on their own.
- **Not a sandbox.** BEA isolates nothing. Whatever your account may do, a bug in BEA or
  in a program that BEA starts may do as well.

## Threat model

### Untrusted

- Everything you give BEA as game data: the game folder, every file in it, and any single
  file you pass to a command.
- Profiles that you did not write yourself.
- Text that BEA takes from those files and then prints or writes into a report.

BEA must treat all of this as data. A file or a profile must never be able to run code,
to reach a path outside the folders you chose, or to change the command line of an
external program.

### Trusted

- Your user account and everything it already controls: the BEA installation, the Python
  environment, environment variables and the directory you run BEA from.
- The external programs you configured. BEA runs the `ffmpeg` and `ffprobe` that it finds
  through an environment variable (`BEA_FFMPEG`, `BEA_FFPROBE`), then in the tools
  directory (`--tools-dir` or `BEA_TOOLS_DIR`), then on the `PATH`. `bea doctor` shows
  which ones it found and where.

Whoever can place a program in one of those locations decides what BEA runs. Keep them
under your control. By default the tools directory is `tools` in the directory you run
BEA from, so do not run BEA inside a folder that someone else prepared.

### Not defended

- An attacker who already controls your account or your computer.
- A modified copy of BEA. Forks and repackaged builds are outside this policy.
- Harm that a profile does within its rules. A profile is data and must not be able to
  run code, but it decides which files BEA rewrites and which bytes it patches. Read a
  profile from someone else before you use it. `bea build --dry-run` shows what it would
  do and writes nothing.

## Guarantees

These hold for every command and every option. A way to break one of them is a
vulnerability.

- **No telemetry.** BEA sends no data to anyone. The reports it writes stay on your disk.
- **No network access.** BEA never connects to a network and never downloads anything: no
  updates, no profiles, no tools.
- **No key material.** BEA never reads console keys or key files. It has no code for
  that, and it decrypts nothing.

The guarantees cover BEA's own code. `ffmpeg` and `ffprobe` are separate programs, and
what they do while they read a media file is their own behavior. BEA starts them
directly, without a shell, and gives them absolute paths to local files.

## Using BEA with care

- Run BEA as an ordinary user, not as administrator or root.
- Give `bea build` an output folder and a work folder of their own. BEA is built to
  remove only files it created there, but separate folders keep a mistake small.
- Keep `ffmpeg` and `ffprobe` up to date. They parse the media files of the game folder.
- For a folder or a profile you do not trust, run BEA in an isolated environment, such as
  a virtual machine or a separate user account.

## What counts as a vulnerability

- **Parser bugs.** A crafted file or profile that makes BEA crash, hang, loop without end
  or corrupt memory. This includes memory-safety problems reached through the way BEA
  uses a dependency.
- **Writes outside the chosen folders.** Path traversal, or any other way to create,
  overwrite or delete something outside the output folder, the work folder, or the file
  or directory that a command was told to write. The way in can be a file name, a profile
  value, a link, or a state file that an earlier run left behind. Changing or deleting a
  file of the game folder counts too: BEA reads game files and never changes them.
- **Decompression bombs.** A small input that expands into an unreasonable amount of
  memory or disk space. Examples are a compressed executable segment, an image that
  declares huge dimensions, and a document built to grow when it is parsed.
- **Command or argument injection.** A file name, a profile value or file content that
  changes what BEA asks an external program to do: an extra option, another input or
  output, a network address or a shell construct.
- **Escapes through an external program.** An input that makes `ffmpeg` or `ffprobe` read
  files outside the game folder, write outside the chosen folders or contact a network,
  when BEA could have prevented it by starting the program differently.
- **Code execution from data.** Any way for a profile or an asset to run code. A profile
  is data, and nothing in it may ever be executed.
- **Forbidden content.** Anything from a game, key material or other forbidden content
  that reached the repository, its history or a release. See
  [Forbidden content in the repository](#forbidden-content-in-the-repository).
- **A broken guarantee.** Any network access, any data sent, any read of key material.

## What is out of scope

- **A game that misbehaves.** Crashes, glitches or poor performance of a game that runs
  with BEA's output come from a profile, not from a security flaw. Open a normal issue.
- **Compatibility bugs.** A format BEA cannot read, a texture that looks wrong, a patch
  that reports a mismatch. A mismatch means that the verification did its job.
- **Anything about protections.** BEA does not work on signature, license, encryption,
  online or integrity checks, and it does not accept packaged or encrypted input, on
  purpose. Requests to change that are not security reports. They are closed without an
  answer.
- **Bugs in other software.** A flaw in `ffmpeg`, `ffprobe`, Python or a dependency that
  does not depend on how BEA uses it belongs to that project.
- **Attacks that need trusted access.** Anything that requires control of your account,
  your Python environment or the programs you configured.

## Reporting a vulnerability

Report privately, through
[GitHub's private vulnerability reporting](https://github.com/matheus-aragao-cavalcante/BEA-Backport-Engineering-Anything/security/advisories/new). Do
not open a public issue, pull request or discussion for a suspected vulnerability. If
that feature is not available to you, open an issue that only asks for a private channel
and gives no details.

Include:

- the BEA version (`bea --version`) or the commit you built from;
- your operating system and Python version (`bea doctor` prints the Python version and
  the platform);
- the command you ran, what happened and what you expected;
- a synthetic reproducer.

Build the reproducer from synthetic data. `bea synth` generates a sample game folder that
contains nothing from any real game. Change that sample until it shows the problem, or
send a short script that writes the bytes. If you cannot reproduce the problem with
synthetic data, describe the input in words: its format, its size and what is unusual
about it.

Keep game files, dumps and keys out of your report: attach none and paste no part of
them. Do not attach a report that BEA wrote for a real game either, because it lists the
game's files. A few redacted lines are enough. Remove personal details such as local
paths and user names before you send anything. For ordinary issues,
[CONTRIBUTING.md](CONTRIBUTING.md#issues-and-bug-reports) says what may be attached.

## What to expect

- The maintainers acknowledge your report and tell you whether they treat it as a
  vulnerability.
- A confirmed vulnerability is fixed on `main` and in the next release.
- You are credited for the report if you ask for it. Otherwise your name is not
  published.
- There is no service-level promise: no guaranteed response time and no guaranteed date
  for a fix.

Please keep the details private until a fix is available.

## Forbidden content in the repository

Nothing taken from a game may be in this project: no code, assets, dumps, file listings,
disassembly or decompiled code, and nothing derived from them. The same goes for keys,
system software, leaked or confidential material, and links to places where any of this
can be obtained. The rule covers commits, profiles, tests, documentation, issues, pull
requests and releases.

If you see such content, report it privately, in the same way as a vulnerability. Say
where it is: the file and commit, the issue number or the release. Do not quote it, copy
it or post it again.

The maintainers then do three things:

1. **Remove.** They take the content out of the current tree and delete the affected
   attachment, comment or release file.
2. **Purge history.** They rewrite the history so that no branch or tag still leads to
   the content, and they replace affected releases with clean ones.
3. **Clear cached references.** They ask the repository host to remove cached views and
   unreachable commits that still expose the content.

A purge changes commit identifiers, and the maintainers cannot clean copies they do not
control. If you have a clone or a fork from before the purge, update or delete it, and
do not push the old history back.

A rights holder who wants a profile removed can use the same private channel. That
process is described in
[DISCLAIMER.md](DISCLAIMER.md#removal-requests-from-rights-holders).
