"""Media properties (container, video and audio streams) as reported by ffprobe.

BEA does not parse media containers itself: the external ``ffprobe`` program does, and
this module turns its JSON report into plain, frozen data. The work is split in two so
that everything but the process call can be used and tested without the tool:

:func:`parse_ffprobe_json`
    Pure. Understands the object printed by
    ``ffprobe -v error -print_format json -show_format -show_streams <file>``.

:func:`probe_media`
    Runs ffprobe on one file. The executable is an argument: locating external tools is
    the job of a higher layer.

Rules
-----
* ffprobe reports are irregular: a number may arrive as a number or as text, and any
  field may be absent. A value that is missing or unusable becomes ``None`` (``0`` for a
  frame size and ``"unknown"`` for a name, which are never optional); only a report
  without a ``streams`` list is rejected.
* Attached pictures (cover art) are listed by ffprobe as video streams but are not
  videos; they are left out.
* A frame rate is the average rate when ffprobe knows it, else the base rate it derives
  from the timestamps. ``0/0`` is how ffprobe writes "unknown".
* The tool is started with an argument list (never through a shell), without standard
  input and with an absolute path, so that no file name can be taken for an option, a
  URL or a shell construct. Every failure of the tool is a
  :class:`~bea.errors.ToolError` that quotes what the tool printed.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bea.errors import FormatError, ToolError
from bea.fsutil import fs_path

__all__ = [
    "AudioStream",
    "MediaInfo",
    "VideoStream",
    "parse_ffprobe_json",
    "probe_media",
    "stderr_summary",
]

#: Stands for a codec or container name that the report does not give.
_UNKNOWN = "unknown"
# ffmpeg prefixes its messages with the logging context and its address
# ("[libx264 @ 0x55d0c8] ..."). The address changes on every run and tells the user
# nothing, so it is removed: the same failure then always reads the same.
_LOG_ADDRESS = re.compile(r" @ (?:0x)?[0-9A-Fa-f]+(?=\])")


@dataclass(frozen=True, slots=True)
class VideoStream:
    """One video stream of a media file."""

    #: Codec name as ffprobe spells it (``"h264"``, ``"vp9"`` ...); ``"unknown"`` if absent.
    codec: str
    #: Frame width in pixels; ``0`` when ffprobe could not tell.
    width: int
    #: Frame height in pixels; ``0`` when ffprobe could not tell.
    height: int
    #: Frames per second, or ``None`` when unknown.
    fps: float | None
    #: The same rate exactly as ffprobe wrote it (``"30000/1001"``); ``None`` when unknown.
    fps_text: str | None
    #: Bits per second; ``None`` when the container does not record it (WebM never does).
    bit_rate: int | None
    #: Pixel format of the picture (``"yuv420p"`` ...), its alpha plane included: see
    #: :func:`parse_ffprobe_json`.
    pix_fmt: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in reports)."""
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fps_text": self.fps_text,
            "bit_rate": self.bit_rate,
            "pix_fmt": self.pix_fmt,
        }


@dataclass(frozen=True, slots=True)
class AudioStream:
    """One audio stream of a media file."""

    #: Codec name as ffprobe spells it (``"aac"``, ``"opus"`` ...); ``"unknown"`` if absent.
    codec: str
    #: Samples per second.
    sample_rate: int | None
    #: Number of channels.
    channels: int | None
    #: Bits per second; ``None`` when the container does not record it.
    bit_rate: int | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in reports)."""
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bit_rate": self.bit_rate,
        }


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What ffprobe says about one media file."""

    #: Container, named after ffprobe's demuxer (``"mov,mp4,m4a,3gp,3g2,mj2"``,
    #: ``"matroska,webm"`` ...); ``"unknown"`` if absent.
    container: str
    #: Length in seconds.
    duration: float | None
    #: File size in bytes.
    size: int | None
    #: Overall bits per second.
    bit_rate: int | None
    #: Video streams in file order, attached pictures excluded.
    video: tuple[VideoStream, ...]
    #: Audio streams in file order.
    audio: tuple[AudioStream, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form (used in reports)."""
        return {
            "container": self.container,
            "duration": self.duration,
            "size": self.size,
            "bit_rate": self.bit_rate,
            "video": [stream.as_dict() for stream in self.video],
            "audio": [stream.as_dict() for stream in self.audio],
        }


def parse_ffprobe_json(document: Mapping[str, Any]) -> MediaInfo:
    """Turn an ffprobe report into a :class:`MediaInfo`.

    ``document`` is the object printed by
    ``ffprobe -v error -print_format json -show_format -show_streams <file>``, already
    decoded from JSON. Streams keep their order; anything that is neither video nor audio
    (subtitles, data, attachments) is ignored, and so are attached pictures.

    One value is interpreted rather than copied: ffprobe's own VP8 / VP9 decoders ignore
    the alpha plane a WebM file may carry and only leave an ``alpha_mode`` tag behind, so
    such a stream is reported with the pixel format it really has (``yuva420p``, not
    ``yuv420p``).

    Raises:
        FormatError: the document has no ``streams`` list, or one of its entries is not
            an object: it is not an ffprobe report.
    """
    streams = document.get("streams") if isinstance(document, Mapping) else None
    if not isinstance(streams, list | tuple):
        raise FormatError(
            'not an ffprobe report: it has no "streams" list (expected the JSON printed by '
            "ffprobe -print_format json -show_format -show_streams)"
        )
    video: list[VideoStream] = []
    audio: list[AudioStream] = []
    for index, stream in enumerate(streams):
        if not isinstance(stream, Mapping):
            raise FormatError(f"not an ffprobe report: entry {index} of streams is not an object")
        kind = stream.get("codec_type")
        if kind == "video":
            if not _is_attached_picture(stream):
                video.append(_video_stream(stream))
        elif kind == "audio":
            audio.append(_audio_stream(stream))

    container = document.get("format")
    if not isinstance(container, Mapping):
        container = {}
    duration = _number(container.get("duration"))
    size = _integer(container.get("size"))
    return MediaInfo(
        container=_text(container.get("format_name")) or _UNKNOWN,
        # Unlike a rate, a duration or a size of zero is a statement, not "unknown".
        duration=duration if duration is not None and duration >= 0 else None,
        size=size if size is not None and size >= 0 else None,
        bit_rate=_positive(container.get("bit_rate")),
        video=tuple(video),
        audio=tuple(audio),
    )


def probe_media(
    path: str | os.PathLike[str], ffprobe: str | os.PathLike[str], *, timeout: float = 60.0
) -> MediaInfo:
    """Ask ffprobe what the media file at ``path`` contains.

    ``ffprobe`` is the executable to run and ``timeout`` the number of seconds it gets:
    probing reads headers only, so a tool that takes longer is stuck.

    Raises:
        ToolError: ffprobe could not be started, did not answer in time, rejected the file
            (missing, not a media file, damaged) or printed something that is not a
            report. The message ends with what ffprobe wrote on its error output.
    """
    shown = os.fspath(path)
    command = [
        os.fspath(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        # Absolute (and long-path safe): never mistaken for an option or a URL scheme.
        fs_path(path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        said = stderr_summary(exc.stderr)
        raise ToolError(
            f"ffprobe gave no answer within {timeout:g} seconds for {shown}"
            + (f": {said}" if said else "")
        ) from exc
    except OSError as exc:
        raise ToolError(
            f"ffprobe could not be started ({command[0]}): {exc}; "
            "check that this path is a working ffprobe executable"
        ) from exc

    if completed.returncode != 0:
        said = stderr_summary(completed.stderr) or "it printed no error message"
        raise ToolError(f"ffprobe cannot read {shown} (exit status {completed.returncode}): {said}")
    try:
        document = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ToolError(f"ffprobe printed something that is not JSON for {shown}: {exc}") from exc
    if not isinstance(document, dict):
        raise ToolError(
            f"ffprobe printed JSON that is not a report for {shown}: "
            f"expected an object, got {type(document).__name__}"
        )
    try:
        return parse_ffprobe_json(document)
    except FormatError as exc:
        raise ToolError(f"ffprobe printed an unusable report for {shown}: {exc}") from exc


def stderr_summary(output: bytes | str | None, *, lines: int = 5) -> str:
    """Condense the error output of a failed ffmpeg / ffprobe run into one line.

    The first message usually names the cause and the last ones its consequences, so the
    result keeps the first non-empty line and the last ``lines - 1`` (all of them when
    the output is that short), joined by ``" | "``, with ``"..."`` standing for what was
    dropped. The address ffmpeg prints next to each logging context is removed.

    Returns ``""`` when nothing was printed.
    """
    if not output:
        return ""
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    kept = [_LOG_ADDRESS.sub("", line.strip()) for line in text.splitlines() if line.strip()]
    limit = max(lines, 2)
    if len(kept) > limit:
        kept = [kept[0], "...", *kept[1 - limit :]]
    return " | ".join(kept)


# ------------------------------------------------------------------------ internals


def _video_stream(stream: Mapping[str, Any]) -> VideoStream:
    rate = _frame_rate(stream.get("avg_frame_rate")) or _frame_rate(stream.get("r_frame_rate"))
    return VideoStream(
        codec=_text(stream.get("codec_name")) or _UNKNOWN,
        width=_positive(stream.get("width")) or 0,
        height=_positive(stream.get("height")) or 0,
        fps=rate[0] if rate is not None else None,
        fps_text=rate[1] if rate is not None else None,
        bit_rate=_positive(stream.get("bit_rate")),
        pix_fmt=_pixel_format(stream),
    )


def _audio_stream(stream: Mapping[str, Any]) -> AudioStream:
    return AudioStream(
        codec=_text(stream.get("codec_name")) or _UNKNOWN,
        sample_rate=_positive(stream.get("sample_rate")),
        channels=_positive(stream.get("channels")),
        bit_rate=_positive(stream.get("bit_rate")),
    )


def _is_attached_picture(stream: Mapping[str, Any]) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping):
        return False
    flag = disposition.get("attached_pic")
    return flag is True or _integer(flag) == 1


def _pixel_format(stream: Mapping[str, Any]) -> str | None:
    pix_fmt = _text(stream.get("pix_fmt"))
    if pix_fmt is None or not pix_fmt.startswith("yuv") or pix_fmt.startswith("yuva"):
        return pix_fmt
    tags = stream.get("tags")
    if isinstance(tags, Mapping):
        for key, value in tags.items():
            # Matroska's AlphaMode: non-zero when an alpha plane is stored beside the picture.
            if isinstance(key, str) and key.lower() == "alpha_mode" and _positive(value):
                return "yuva" + pix_fmt[3:]
    return pix_fmt


def _frame_rate(value: object) -> tuple[float, str] | None:
    """Parse a rate written ``"num/den"`` (or as a plain number) into ``(fps, text)``.

    ``None`` when the value says nothing: missing, malformed, zero or ``0/0``.
    """
    if isinstance(value, str):
        text = value.strip()
        numerator_text, slash, denominator_text = text.partition("/")
        numerator = _number(numerator_text)
        denominator = _number(denominator_text) if slash else 1.0
    else:
        numerator = _number(value)
        denominator = 1.0
        text = str(value)
    if numerator is None or denominator is None or numerator <= 0 or denominator <= 0:
        return None
    rate = numerator / denominator
    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate, text


def _text(value: object) -> str | None:
    """Return ``value`` when it is non-blank text (stripped), else ``None``."""
    if isinstance(value, str):
        return value.strip() or None
    return None


def _number(value: object) -> float | None:
    """Return a finite number given as a number or as numeric text, else ``None``."""
    # bool is an int subclass, but ``true`` is never a measurement.
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    """Return an integer given as a number or as numeric text, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            # Exact for sizes beyond what a float holds.
            return int(value)
        except ValueError:
            pass
    number = _number(value)
    return int(number) if number is not None else None


def _positive(value: object) -> int | None:
    """Return a strictly positive integer, else ``None`` (ffprobe's zero means "unknown")."""
    number = _integer(value)
    return number if number is not None and number > 0 else None
