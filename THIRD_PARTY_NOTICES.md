# Third-party notices

BEA (Backport Engineering Anything) is published under the MIT license; see
[LICENSE](LICENSE). This file lists the third-party software that BEA works with, and
says what this repository contains and what it does not.

In short:

- this repository contains no third-party code and redistributes no third-party
  software;
- the Python packages below are installed by your package manager, from the package
  index that you use;
- the external programs below are installed by you and located by BEA at run time.

License names are SPDX identifiers from the
[SPDX License List](https://spdx.org/licenses/). This file is information for users and
contributors, not legal advice. The license text that comes with each component is the
authoritative one.

## 1. Python packages

[pyproject.toml](pyproject.toml) declares these packages, and [uv.lock](uv.lock) pins the
versions used for development. Your package manager downloads and installs them. They are
not part of this repository, and this repository does not redistribute them.

| Package | Used for | Needed | License (SPDX) |
| :-- | :-- | :-- | :-- |
| Pillow | reading, resizing and writing images | at run time | `MIT-CMU` |
| PyYAML | reading YAML profiles | at run time | `MIT` |
| lz4 | LZ4 codec for executable modules | at run time | `BSD-3-Clause` |
| pytest | running the tests | for development | `MIT` |
| ruff | linting and formatting | for development | `MIT` |
| mypy | static type checking | for development | `MIT` |
| types-PyYAML | type information for PyYAML | for development | `Apache-2.0` |
| hatchling | build backend | to build or install from source | `MIT` |

Notes:

- The identifiers are the ones that each package declares, in its metadata or in its
  license file, at the time of writing. A later release of a package can carry a
  different license.
- A compiled distribution of a package can include libraries that have licenses of their
  own, for example the image codecs inside a Pillow wheel. They come from the package
  index, not from this repository. The license files inside each installed distribution
  name them.
- These packages have dependencies of their own. `uv.lock` records the complete resolved
  set, and `uv tree` prints it. Each of those packages is installed under its own
  license.

## 2. External programs

BEA can use two programs of the FFmpeg project. Both are optional.

| Program | Used for | License |
| :-- | :-- | :-- |
| `ffmpeg` | transcoding videos (the media rules of a profile) | depends on the build |
| `ffprobe` | reading the properties of video and audio files | the same as `ffmpeg` |

- **Never bundled, never downloaded.** You obtain and install these programs yourself,
  under their own license. BEA does not ship them and does not fetch them.
- **Located at run time.** BEA looks for each program in the environment variable
  `BEA_FFMPEG` or `BEA_FFPROBE`, then in the tools directory (`--tools-dir` or
  `BEA_TOOLS_DIR`; by default `tools/`, which is git-ignored), then on the `PATH`.
  `bea doctor` shows what it found.
- **Run as separate programs.** BEA starts them through their command line. It contains
  none of their code and does not link against their libraries.
- **License.** FFmpeg is licensed under `LGPL-2.1-or-later`. A build that enables its
  GPL-licensed parts is under `GPL-2.0-or-later`, and some build options raise either
  license to version 3. Which one applies depends on the build that you installed;
  `ffmpeg -L` prints it.
- **Optional.** Without these programs BEA still works. It reports the media files as
  unsupported and processes everything else.

The CI workflow installs FFmpeg from the package archive of its Linux runner, only to run
the media tests. Nothing from it is stored in this repository.

## 3. Format documentation

BEA contains no third-party code. The code in this repository was written for BEA, from
public documentation and from the project's own format notes. Nothing was copied,
translated or adapted from another project, whatever its license. Nothing comes from an
SDK, a development kit, confidential documents or leaked material.

BEA reads a few file formats found in an extracted Nintendo Switch game folder, such as
executable modules, and it writes IPS patch files and the LayeredFS folder layout. All of
this was implemented from public documentation only: published descriptions and
specifications of the formats. [docs/FORMATS.md](docs/FORMATS.md) holds the project's
notes on each format and lists the public sources per format.

- The documentation is used as a source of facts. Its text is not reproduced: the notes
  are written in the project's own words.
- The LayeredFS output (`atmosphere/contents/<TitleID>/romfs` and
  `atmosphere/exefs_patches/<name>/<BuildID>.ips`) uses the folder layout that the
  Atmosphere custom firmware reads. BEA contains no code from Atmosphere or from any
  other loader.
- Tests and the `bea synth` generator create their own data. No file from a game, from
  an SDK or from another project serves as a fixture.

The rules behind these statements are the clean-room policy in
[CONTRIBUTING.md](CONTRIBUTING.md#clean-room-policy).

## 4. Maintaining this file

Update this file in the same pull request that changes a dependency.

- **A Python package is added, removed or replaced.** Change `pyproject.toml`, refresh
  `uv.lock` and edit the table in section 1. Take the SPDX identifier from the package's
  own metadata or license file, not from memory. A package that `bea/` imports also needs
  an entry in the import allowlist of
  [tests/test_arch_rules.py](tests/test_arch_rules.py). That test fails when the
  allowlist and `pyproject.toml` disagree.
- **A package changes its license** in a release that the project adopts. Correct the
  table.
- **BEA learns to use another external program.** Add a row to section 2. The program is
  located at run time. It is never bundled and never downloaded.
- **New format knowledge arrives.** Its public source belongs in `docs/FORMATS.md`, not
  here.

A change that would put third-party code or binaries into this repository is not solved
by a notice in this file. The project does not accept such changes; see the hard rules in
[CONTRIBUTING.md](CONTRIBUTING.md#the-hard-rules).

## Names and trademarks

Third-party names appear in this file only to identify the components and formats
described above.

Nintendo Switch is a trademark of Nintendo. All other names belong to their owners.
BEA is not affiliated with or endorsed by any of them.
