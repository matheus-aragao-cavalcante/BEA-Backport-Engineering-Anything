"""A synthetic sample game, generated on the spot: ``bea synth``.

It lets anyone try BEA end to end, and the test suite exercise the whole pipeline,
without any real game. Every byte is generated procedurally from a seed: gradients and
shapes for the textures, made-up executable modules, small configuration files. Nothing
comes from, or imitates the content of, a real game.

The sample is built to show each kind of outcome a real build has:

* textures that are resampled (PNG, TGA), mip-dropped (DDS with a full chain), left
  alone (within the limits), excluded by a rule, and one that BEA honestly reports as
  unsupported (a BC7 DDS without mips: nothing can be dropped and there is no encoder);
* configuration edits in an INI file with CRLF line endings and in a JSON file;
* an executable module with two planted patch sites, and a matching ``patches.yaml``
  whose hunks are verified by hash windows (the form repository profiles use);
* optionally a short generated video, when ffmpeg is installed.

The generator is deterministic: the same seed and options give byte-identical files.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from bea.errors import OutputError
from bea.formats import dds
from bea.formats.nacp import build_nacp
from bea.formats.npdm import build_npdm
from bea.formats.nso import build_nso
from bea.fsutil import atomic_write_bytes, atomic_write_text, fs_path, walk_files
from bea.hexutil import format_hex_bytes
from bea.toolbox import Toolbox

__all__ = ["SAMPLE_PUBLISHER", "SynthResult", "synthesize", "synthetic_title_id"]

SAMPLE_PUBLISHER = "BEA Synthetic Publisher"
_SAMPLE_VERSION = "1.0.0"
_WINDOW = 32
_FFMPEG_TIMEOUT_SECONDS = 120

# Two made-up patch sites in the text segment of the sample's main module. The planted
# words are AArch64 "movz" instructions loading 1080 and 60; the patches load 720 and 30.
_SITE_RESOLUTION = 0x1000
_SITE_FRAMERATE = 0x2400


def _movz_w(register: int, value: int) -> bytes:
    """Encode ``movz w<register>, #value`` (little-endian)."""
    return (0x52800000 | (value << 5) | register).to_bytes(4, "little")


@dataclass(frozen=True, slots=True)
class SynthResult:
    """What :func:`synthesize` wrote."""

    root: Path
    #: The matching profile directory, when one was requested.
    profile_dir: Path | None
    title_id: str
    #: Number of files written under ``root``.
    files: int
    notes: tuple[str, ...] = ()


def synthetic_title_id(seed: int) -> str:
    """Return a deterministic application ID for the sample made with ``seed``.

    It lies in the application range, has its low 13 bits clear like a base ID, and is
    derived from a hash, so it is as unlikely as any random ID to belong to a real title.
    """
    digest = hashlib.sha256(f"bea-synth-{seed}".encode()).digest()
    middle = (int.from_bytes(digest[:5], "big") & ((1 << 34) - 1)) | (1 << 34)
    return f"{(0x0100 << 48) | (middle << 13):016X}"


# ----------------------------------------------------------------------- generators


def _module_bytes(rng: random.Random, size: int, vocabulary: list[bytes]) -> bytearray:
    """Code-like bytes: mostly words from a small vocabulary (so LZ4 has work to do)."""
    out = bytearray()
    while len(out) < size:
        if rng.random() < 0.8:
            out += rng.choice(vocabulary)
        else:
            out += rng.randbytes(4)
    del out[size:]
    return out


def _main_module(seed: int) -> tuple[bytes, bytes, dict[str, int]]:
    rng = random.Random(f"main-{seed}")
    vocabulary = [rng.randbytes(4) for _ in range(24)]
    text = _module_bytes(rng, 0x4000, vocabulary)
    text[_SITE_RESOLUTION : _SITE_RESOLUTION + 4] = _movz_w(8, 1080)
    text[_SITE_FRAMERATE : _SITE_FRAMERATE + 4] = _movz_w(9, 60)
    rodata = _module_bytes(rng, 0x1800, vocabulary)
    data = bytes(_module_bytes(rng, 0x800, vocabulary))
    module_id = hashlib.sha256(f"bea-synth-main-{seed}".encode()).digest()[:20]
    nso = build_nso(bytes(text), bytes(rodata), data, module_id=module_id, bss_size=0x1000)
    return nso, bytes(text), {"resolution": _SITE_RESOLUTION, "framerate": _SITE_FRAMERATE}


def _sdk_module(seed: int) -> bytes:
    rng = random.Random(f"sdk-{seed}")
    vocabulary = [rng.randbytes(4) for _ in range(16)]
    module_id = hashlib.sha256(f"bea-synth-sdk-{seed}".encode()).digest()[:20]
    return build_nso(
        bytes(_module_bytes(rng, 0x2000, vocabulary)),
        bytes(_module_bytes(rng, 0x800, vocabulary)),
        bytes(_module_bytes(rng, 0x400, vocabulary)),
        module_id=module_id,
    )


def _picture(rng: random.Random, width: int, height: int, *, alpha: bool) -> Image.Image:
    """A procedural picture: gradients plus a few shapes, built with C-level operations."""
    red = Image.linear_gradient("L").resize((width, height))
    green = Image.linear_gradient("L").rotate(90).resize((width, height))
    blue = Image.radial_gradient("L").resize((width, height))
    image = Image.merge("RGB", (red, green, blue))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        left, top = rng.randrange(width), rng.randrange(height)
        size = rng.randrange(max(2, min(width, height) // 8), max(3, min(width, height) // 3))
        colour = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        if rng.random() < 0.5:
            draw.ellipse((left, top, left + size, top + size), fill=colour)
        else:
            draw.rectangle((left, top, left + size, top + size // 2), fill=colour)
    if alpha:
        mask = Image.radial_gradient("L").resize((width, height)).point(lambda value: 255 - value)
        image.putalpha(mask)
    return image


def _encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _dds_with_mips(image: Image.Image, name: str) -> bytes:
    fmt = dds.format_by_name(name)
    width, height = image.size
    chain = []
    level = 0
    while True:
        size = (max(1, width >> level), max(1, height >> level))
        chain.append(dds.encode_surface(image.resize(size, Image.Resampling.BOX), fmt))
        if size == (1, 1):
            break
        level += 1
    return dds.build_dds(width, height, fmt, [chain])


_GRAPHICS_INI = (
    "; Synthetic sample settings (generated by bea synth)\r\n"
    "[Display]\r\n"
    "Fullscreen=1\r\n"
    "\r\n"
    "[Rendering]\r\n"
    "ResolutionScale=100\r\n"
    "ShadowQuality=High\r\n"
    "MaxFPS=60\r\n"
)
_SETTINGS_JSON = '{\n  "video": {\n    "maxFps": 60,\n    "vsync": true\n  },\n  "audio": {\n    "volume": 0.8\n  }\n}\n'


def _video(ffmpeg: Path, destination: Path) -> str | None:
    """Encode a one-second test pattern; return why it failed, or ``None``."""
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=60:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        fs_path(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if completed.returncode != 0 or not destination.exists():
        lines = (completed.stderr or "").strip().splitlines()
        return lines[-1] if lines else f"ffmpeg exited with status {completed.returncode}"
    return None


# --------------------------------------------------------------------------- profile


def _profile_yaml(title_id: str, name: str) -> str:
    return f"""\
# Profile of the BEA synthetic sample. It is not a game: `bea synth` generated every file.
schema: 1
title_id: "{title_id}"
name: "{name}"
description: >
  Demonstrates every kind of outcome of a build on generated data.

target:
  max_width: 1280
  max_height: 720

textures:
  rules:
    - match: "**/*_lut.*"
      skip: true
    - match: "textures/crate_*"
      max_width: 512
      max_height: 512

media:
  rules:
    - match: "movies/**/*.mp4"
      max_height: 720
      max_fps: 30

configs:
  - match: "config/graphics.ini"
    format: ini
    edits:
      - {{section: "Rendering", key: "ResolutionScale", value: "70"}}
      - {{section: "Rendering", key: "MaxFPS", value: "30"}}
  - match: "config/settings.json"
    format: json
    edits:
      - {{path: ["video", "maxFps"], value: 30}}
"""


def _hunk_yaml(text: bytes, offset: int, patch: bytes) -> str:
    start = offset - offset % 4
    digest = hashlib.sha256(text[start : start + _WINDOW]).hexdigest()
    return (
        f"          - offset: 0x{offset:X}\n"
        f'            patch_bytes: "{format_hex_bytes(patch)}"\n'
        f'            expected_sha256: "{digest}"\n'
        f"            window_offset: 0x{start:X}\n"
        f"            window_length: {_WINDOW}\n"
    )


def _patches_yaml(title_id: str, build_id: str, text: bytes, sites: dict[str, int]) -> str:
    return (
        "# Executable patches of the BEA synthetic sample (made-up code, made-up sites).\n"
        "schema: 1\n"
        f'title_id: "{title_id}"\n'
        "builds:\n"
        f'  - build_id: "{build_id}"\n'
        "    module: main\n"
        f'    label: "{_SAMPLE_VERSION}"\n'
        "    patches:\n"
        "      - name: render-720p\n"
        "        category: resolution\n"
        '        description: "Loads 720 instead of 1080 as the render height."\n'
        "        hunks:\n"
        + _hunk_yaml(text, sites["resolution"], _movz_w(8, 720))
        + "      - name: fps-30\n"
        "        category: framerate\n"
        '        description: "Loads 30 instead of 60 as the frame-rate target."\n'
        "        hunks:\n" + _hunk_yaml(text, sites["framerate"], _movz_w(9, 30))
    )


# --------------------------------------------------------------------------- driver


def _require_empty(directory: Path) -> None:
    if os.path.exists(fs_path(directory)):
        if not os.path.isdir(fs_path(directory)):
            raise OutputError(f"{directory} exists and is not a directory")
        if any(os.scandir(fs_path(directory))):
            raise OutputError(f"{directory} is not empty; choose a new folder for the sample")


def synthesize(
    root: str | os.PathLike[str],
    *,
    profiles_dir: str | os.PathLike[str] | None = None,
    seed: int = 1,
    title_id: str | None = None,
    name: str = "BEA Synthetic Sample",
    with_media: bool = False,
    tools: Toolbox | None = None,
) -> SynthResult:
    """Write the synthetic sample game to ``root`` (and its profile under ``profiles_dir``).

    Raises:
        OutputError: ``root`` (or the profile directory) exists and is not empty.
    """
    root_path = Path(os.path.abspath(os.fspath(root)))
    identity = title_id or synthetic_title_id(seed)
    profile_dir = (
        None if profiles_dir is None else Path(os.path.abspath(os.fspath(profiles_dir))) / identity
    )
    _require_empty(root_path)
    if profile_dir is not None:
        _require_empty(profile_dir)
    notes: list[str] = []
    rng = random.Random(f"assets-{seed}")

    exefs = root_path / "ExeFS"
    romfs = root_path / "RomFS"
    main, text, sites = _main_module(seed)
    atomic_write_bytes(exefs / "main", main)
    atomic_write_bytes(exefs / "sdk", _sdk_module(seed))
    atomic_write_bytes(exefs / "main.npdm", build_npdm(int(identity, 16)))
    atomic_write_bytes(
        root_path / "control.nacp",
        build_nacp(
            {0: (name, SAMPLE_PUBLISHER)},
            display_version=_SAMPLE_VERSION,
            application_id=int(identity, 16),
        ),
    )

    atomic_write_bytes(
        romfs / "ui" / "title_2560x1440.png", _encode(_picture(rng, 2560, 1440, alpha=True), "PNG")
    )
    atomic_write_bytes(
        romfs / "ui" / "panel_1920x1080.tga",
        _encode(_picture(rng, 1920, 1080, alpha=True), "TGA", compression="tga_rle"),
    )
    atomic_write_bytes(
        romfs / "ui" / "icon_256.png", _encode(_picture(rng, 256, 256, alpha=True), "PNG")
    )
    atomic_write_bytes(
        romfs / "textures" / "rock_2048_bc1.dds",
        _dds_with_mips(_picture(rng, 2048, 2048, alpha=False).convert("RGBA"), "BC1_UNORM"),
    )
    crate = _picture(rng, 1024, 1024, alpha=True)
    rgba8 = dds.format_by_name("R8G8B8A8_UNORM")
    atomic_write_bytes(
        romfs / "textures" / "crate_1024_rgba.dds",
        dds.build_dds(1024, 1024, rgba8, [[dds.encode_surface(crate, rgba8)]]),
    )
    bc7 = dds.format_by_name("BC7_UNORM")
    atomic_write_bytes(
        romfs / "textures" / "sky_2048_bc7.dds",
        dds.build_dds(2048, 2048, bc7, [[rng.randbytes(bc7.level_size(2048, 2048))]]),
    )
    atomic_write_bytes(
        romfs / "fx" / "grade_lut.png", _encode(_picture(rng, 4096, 16, alpha=False), "PNG")
    )
    atomic_write_bytes(romfs / "config" / "graphics.ini", _GRAPHICS_INI.encode("utf-8"))
    atomic_write_bytes(romfs / "config" / "settings.json", _SETTINGS_JSON.encode("utf-8"))
    atomic_write_bytes(romfs / "data" / "archive.bin", rng.randbytes(4096))

    if with_media:
        toolbox = tools or Toolbox()
        ffmpeg = toolbox.find("ffmpeg")
        if ffmpeg is None:
            notes.append("no video was generated: " + toolbox.missing_hint("ffmpeg"))
        else:
            movie = romfs / "movies" / "intro.mp4"
            movie.parent.mkdir(parents=True, exist_ok=True)
            problem = _video(ffmpeg, movie)
            if problem is not None:
                notes.append(f"no video was generated: ffmpeg failed ({problem})")
                if movie.exists():
                    movie.unlink()

    if profile_dir is not None:
        header = main[:0x100]
        build_id = header[0x40:0x60].rstrip(b"\x00").hex().upper()
        atomic_write_text(profile_dir / "profile.yaml", _profile_yaml(identity, name))
        atomic_write_text(
            profile_dir / "patches.yaml", _patches_yaml(identity, build_id, text, sites)
        )
        atomic_write_text(
            profile_dir / "NOTES.md",
            "# Notes\n\nThis profile belongs to the synthetic sample generated by `bea synth`.\n"
            "It describes generated files, not a game.\n",
        )

    files = sum(1 for _ in walk_files(root_path))
    return SynthResult(root_path, profile_dir, identity, files, tuple(notes))
