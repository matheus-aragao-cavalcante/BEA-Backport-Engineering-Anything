"""The media transformer: downscale and frame-rate-cap videos with ffmpeg.

Game videos are ordinary containers (MP4, WebM, Matroska ...). When a media rule of the
profile selects one that is larger or faster than the rule allows, or asks for a bitrate
or a quality, BEA re-encodes it with the external ``ffmpeg`` program (``ffprobe`` tells
what the file contains). The file keeps its container and its codec family, because that
is what the game's player was built to open.

Rules
-----
* **Opt-in.** Only files selected by a media rule are looked at, and a selected file that
  already complies yields nothing: it is neither re-encoded nor copied.
* **Planning decides everything.** :meth:`MediaTransformer.plan` probes the file and
  resolves the rule into parameters that fully determine the ffmpeg command line
  (:func:`build_ffmpeg_command`), defaults included; :meth:`MediaTransformer.transform`
  only runs it. Same source and same parameters mean the same command.
* **Never guess.** A file that cannot be re-encoded faithfully is reported as a skip that
  says why: missing tools, a codec without a known encoder, a quality setting its encoder
  does not have, an alpha channel that re-encoding would drop.
* **No shell.** ffmpeg is started with an argument list, without standard input and with
  absolute paths: file names with spaces, quotes or non-ASCII characters pass through
  untouched and can never be taken for an option or a URL.

Limits of this version: one video stream per file is kept (the first that is not an
attached picture) with every audio stream; subtitle and data streams are dropped; the
picture is always written as 8-bit 4:2:0.
"""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Iterator, Mapping
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

from bea.errors import BeaError, InputError, ToolError
from bea.formats.mediainfo import VideoStream, probe_media, stderr_summary
from bea.fsutil import fs_path
from bea.model import ROMFS_PREFIX, FileEntry, GameInput
from bea.profile.schema import MediaParams
from bea.toolbox import Toolbox
from bea.transformers.base import (
    Action,
    PlanContext,
    PlanItem,
    Skip,
    SkipKind,
    Transformer,
    TransformResult,
)

__all__ = [
    "AUDIO_ENCODERS",
    "MEDIA_EXTENSIONS",
    "VIDEO_ENCODERS",
    "MediaTransformer",
    "build_ffmpeg_command",
    "output_size",
]

#: Lower-case extensions of the containers this transformer can process.
MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ogv"}
)

#: Video codec of the source, as ffprobe names it -> the ffmpeg encoder that keeps it.
VIDEO_ENCODERS: Mapping[str, str] = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
    "vp8": "libvpx",
    "mpeg4": "mpeg4",
    "theora": "libtheora",
}

#: Audio codec of the source -> the ffmpeg encoder used when a rule sets ``audio_bitrate``.
AUDIO_ENCODERS: Mapping[str, str] = {
    "aac": "aac",
    "opus": "libopus",
    "vorbis": "libvorbis",
    "mp3": "libmp3lame",
}

#: Constant-quality level used when a rule sets no quality, per encoder that has one.
_DEFAULT_CRF: Mapping[str, int] = {"libx264": 23, "libx265": 28, "libvpx-vp9": 32}
#: Bitrate used when a rule sets no quality, for the encoders that are driven by one.
_DEFAULT_BITRATE: Mapping[str, str] = {"libvpx": "1500k", "mpeg4": "2000k", "libtheora": "2000k"}
#: Encoders that accept ``-crf``. The others would ignore it without a word and encode at
#: ffmpeg's tiny default bitrate, so a rule asking them for a CRF is refused instead.
_CRF_ENCODERS = frozenset({"libx264", "libx265", "libvpx-vp9", "libvpx"})
#: Containers of the MP4 family: their index is moved to the front so playback can start
#: before the whole file has been read.
_FASTSTART_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov"})
#: Pixel formats with an alpha plane start with one of these.
_ALPHA_PIXEL_FORMATS = ("yuva", "gbrap", "rgba", "bgra", "argb", "abgr", "ayuv", "vuya", "ya8")
#: A source this little above the cap is the cap itself, written with rounding
#: (``30000/1001`` against 30, or a container that stores 30.003).
_FPS_TOLERANCE = 0.01
_REQUIRED_TOOLS = ("ffmpeg", "ffprobe")


def output_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Return the frame size a ``width x height`` video gets inside ``max_width x max_height``.

    * A frame that already fits is returned unchanged, odd dimensions included: no change
      means no re-encode.
    * Otherwise the frame is scaled down proportionally, never up, and both results are
      rounded *down* to even numbers of at least 2, because 4:2:0 video needs even
      dimensions.
    * The box can be filled by pinning the width to its limit or by pinning the height.
      Usually only one of the two fits; when the frame overshoots the box by less than the
      rounding itself both do, and the one whose shape is closest to the source wins:
      1281x721 in 1280x720 becomes 1280x720 rather than 1278x720.

    Raises:
        ValueError: a dimension or a limit is smaller than 1.
    """
    if min(width, height, max_width, max_height) < 1:
        raise ValueError(
            f"sizes must be at least 1, got {width}x{height} in a {max_width}x{max_height} box"
        )
    if width <= max_width and height <= max_height:
        return width, height
    limit_width = min(width, max_width)
    limit_height = min(height, max_height)
    # Integer arithmetic throughout: a float could turn an exact 720 into 719.999.
    by_width = (_even(limit_width), _even(height * limit_width // width))
    by_height = (_even(width * limit_height // height), _even(limit_height))
    fitting = [
        size for size in (by_width, by_height) if size[0] <= max_width and size[1] <= max_height
    ]
    if not fitting:
        # Only a box smaller than the 2-pixel minimum gets here: take the smaller frame.
        return min(by_width, by_height, key=lambda size: size[0] * size[1])

    def distortion(size: tuple[int, int]) -> Fraction:
        # How much more one side is scaled than the other: 1 for a perfect fit.
        across, down = size[0] * height, size[1] * width
        return Fraction(max(across, down), min(across, down))

    # When both candidates distort the picture equally, the larger one keeps more of it.
    return min(fitting, key=lambda size: (distortion(size), -size[0] * size[1]))


def build_ffmpeg_command(ffmpeg: str, src: str, dst: str, config: Mapping[str, Any]) -> list[str]:
    """Return the ffmpeg argument list that executes one media action.

    Pure: nothing is run or read. ``config`` is the ``params`` of an action planned by
    :meth:`MediaTransformer.plan`:

    ``width``, ``height``
        Output frame size.
    ``fps``
        Frame-rate cap, or ``None`` to keep the source rate.
    ``video_encoder``
        ffmpeg encoder name.
    ``crf``, ``video_bitrate``
        Constant-quality level and/or bitrate; at least one is set. Together they mean
        "this quality, but never above this bitrate".
    ``audio``
        ``{"mode": "copy"}`` or ``{"mode": "encode", "encoder": ..., "bitrate": ...}``.

    The command keeps the first real video stream and every audio stream, and drops
    subtitles and data streams::

        ffmpeg -hide_banner -nostdin -y -loglevel error -i SRC
               -map 0:V:0 -map 0:a? -sn -dn
               -vf scale=W:H:flags=lanczos[,fps=FPS]
               -c:v ENCODER [-crf N] [-b:v RATE]
               -pix_fmt yuv420p
               -c:a copy | -c:a ENCODER -b:a RATE
               [-movflags +faststart]
               DST

    ``src`` and ``dst`` should be absolute paths, so that ffmpeg cannot read them as
    options or URLs; the extension of ``dst`` selects the container.

    Raises:
        ValueError: ``config`` is not a complete set of media parameters.
    """
    width = _int_param(config, "width", minimum=1)
    height = _int_param(config, "height", minimum=1)
    encoder = _text_param(config, "video_encoder")
    crf = None if config.get("crf") is None else _int_param(config, "crf", minimum=0)
    bitrate = None if config.get("video_bitrate") is None else _text_param(config, "video_bitrate")
    if crf is None and bitrate is None:
        raise ValueError("media params: one of 'crf' and 'video_bitrate' must be set")

    filters = f"scale={width}:{height}:flags=lanczos"
    fps = config.get("fps")
    if fps is not None:
        if isinstance(fps, bool) or not isinstance(fps, int | float) or not 0 < fps < math.inf:
            raise ValueError(f"media params: 'fps' must be a positive number or None, got {fps!r}")
        filters += f",fps={_format_number(fps)}"

    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", src]
    # "V" is "v" minus attached pictures: the stream that planning looked at, even in a
    # file that lists its cover art first.
    command += ["-map", "0:V:0", "-map", "0:a?", "-sn", "-dn"]
    command += ["-vf", filters, "-c:v", encoder]
    if crf is not None:
        command += ["-crf", str(crf)]
    if bitrate is not None:
        command += ["-b:v", bitrate]
    elif encoder == "libvpx-vp9":
        # Without an explicit zero, libvpx-vp9 keeps its default bitrate as a ceiling and
        # the CRF is only a quality floor under it.
        command += ["-b:v", "0"]
    command += ["-pix_fmt", "yuv420p"]

    audio = config.get("audio")
    if not isinstance(audio, Mapping) or audio.get("mode") not in ("copy", "encode"):
        raise ValueError(
            "media params: 'audio' must be {'mode': 'copy'} or "
            f"{{'mode': 'encode', 'encoder': ..., 'bitrate': ...}}, got {audio!r}"
        )
    if audio["mode"] == "copy":
        command += ["-c:a", "copy"]
    else:
        command += ["-c:a", _text_param(audio, "encoder"), "-b:a", _text_param(audio, "bitrate")]

    if _extension(dst) in _FASTSTART_EXTENSIONS:
        command += ["-movflags", "+faststart"]
    command.append(dst)
    return command


class MediaTransformer(Transformer):
    """Downscales and frame-rate-caps the videos selected by the profile's media rules."""

    name = "media"
    version = 1

    def __init__(self, tools: Toolbox | None = None) -> None:
        """Create the transformer.

        ``tools`` locates ffmpeg for :meth:`transform`; without it a :class:`Toolbox` with
        the standard lookup rules is used. Planning uses the toolbox of its context.
        """
        self._tools = tools if tools is not None else Toolbox()

    def supports(self, path: str) -> bool:
        """Tell whether ``path`` is a RomFS file with one of :data:`MEDIA_EXTENSIONS`."""
        return (
            path.startswith(ROMFS_PREFIX) and PurePosixPath(path).suffix.lower() in MEDIA_EXTENSIONS
        )

    def plan(self, context: PlanContext) -> Iterator[PlanItem]:
        """Yield what to do with every video that a media rule selects.

        Nothing at all is yielded (and no tool is looked for) when the profile has no
        media rules or media is disabled. A selected video yields:

        * a :class:`Skip` of kind ``UNSUPPORTED`` when ffmpeg or ffprobe is missing, when
          the file has no video stream, or when it needs work that cannot be done
          faithfully (see the module docstring);
        * a :class:`Skip` of kind ``INVALID`` when ffprobe cannot read it;
        * nothing when it is within the rule's limits and the rule asks for no bitrate or
          quality;
        * otherwise an :class:`Action` whose params are those of
          :func:`build_ffmpeg_command`.

        Each selected file is probed with ffprobe (headers only); nothing is written.
        """
        settings = context.profile.media
        if not settings.enabled or not settings.rules:
            return
        target = context.profile.target
        tools_checked = False
        missing_tools: str | None = None
        for entry in context.game.romfs_files:
            if not self.supports(entry.logical):
                continue
            params = settings.resolve(entry.path, target)
            if params is None:
                continue
            if not tools_checked:
                # Once per plan, and only when a rule really selects a video.
                missing_tools = _missing_tools(context.tools)
                tools_checked = True
            if missing_tools is not None:
                yield Skip(self.name, entry.logical, SkipKind.UNSUPPORTED, missing_tools)
                continue
            item = self._plan_file(context.game, entry, params, context.tools.require("ffprobe"))
            if item is not None:
                yield item

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        """Run ffmpeg for one planned action: read ``src``, write the video ``dst``.

        The result's details hold ``bytes_in``, ``bytes_out`` and ``command``: the
        argument list that was run, with the two paths written ``<src>`` and ``<dst>``.

        Raises:
            ToolError: ffmpeg is not installed (the message says how to provide it), could
                not be started, failed (the message quotes its error output) or left no
                data in ``dst``.
            InputError: ``src`` cannot be read.
            ValueError: ``config`` was not produced by :meth:`plan`.
        """
        ffmpeg = self._tools.require("ffmpeg")
        source, destination = fs_path(src), fs_path(dst)
        command = build_ffmpeg_command(str(ffmpeg), source, destination, config)
        try:
            bytes_in = os.path.getsize(source)
        except OSError as exc:
            raise InputError(f"cannot read the video to transcode: {exc}") from exc
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ToolError(
                f"ffmpeg could not be started ({ffmpeg}): {exc}; "
                "check that this path is a working ffmpeg executable"
            ) from exc
        if completed.returncode != 0:
            said = stderr_summary(completed.stderr) or "it printed no error message"
            raise ToolError(
                f"ffmpeg failed on {os.fspath(src)} (exit status {completed.returncode}): {said}"
            )
        try:
            bytes_out = os.path.getsize(destination)
        except OSError:
            bytes_out = 0
        if bytes_out == 0:
            said = stderr_summary(completed.stderr)
            raise ToolError(
                f"ffmpeg reported success for {os.fspath(src)} but wrote no data to its output"
                + (f": {said}" if said else "")
            )

        # The report shows what was run without tying it to this machine's folders.
        placeholders = {source: "<src>", destination: "<dst>"}
        result = TransformResult(
            details={
                "bytes_in": bytes_in,
                "bytes_out": bytes_out,
                "command": [placeholders.get(argument, argument) for argument in command],
            }
        )
        if bytes_out > bytes_in:
            result.notes.append(
                f"the transcoded video is larger than the original ({bytes_in} -> {bytes_out} "
                "bytes): a higher crf or a lower video_bitrate in its media rule would shrink it"
            )
        return result

    # ------------------------------------------------------------------ internals

    def _plan_file(
        self, game: GameInput, entry: FileEntry, params: MediaParams, ffprobe: Path
    ) -> PlanItem | None:
        logical = entry.logical

        def skip(kind: SkipKind, reason: str) -> Skip:
            return Skip(self.name, logical, kind, reason)

        try:
            info = probe_media(game.resolve(logical), ffprobe)
        except (BeaError, OSError, ValueError) as exc:
            return skip(SkipKind.INVALID, str(exc))
        if not info.video:
            return skip(SkipKind.UNSUPPORTED, "no video stream: there is nothing to downscale")
        video = info.video[0]
        if video.width < 1 or video.height < 1:
            return skip(SkipKind.INVALID, "ffprobe reports no frame size for the video stream")

        size = output_size(video.width, video.height, params.max_width, params.max_height)
        cap = params.max_fps
        if cap is not None and (video.fps is None or video.fps <= cap + _FPS_TOLERANCE):
            cap = None
        # A bitrate for audio that does not exist asks for nothing.
        encode_audio = params.audio_bitrate is not None and bool(info.audio)
        asks_quality = params.crf is not None or params.video_bitrate is not None or encode_audio
        if size == (video.width, video.height) and cap is None and not asks_quality:
            return None

        encoder = VIDEO_ENCODERS.get(video.codec)
        if encoder is None:
            return skip(
                SkipKind.UNSUPPORTED,
                f"video codec {video.codec} cannot be re-encoded "
                f"(supported: {', '.join(VIDEO_ENCODERS)})",
            )
        if video.pix_fmt is not None and video.pix_fmt.startswith(_ALPHA_PIXEL_FORMATS):
            return skip(
                SkipKind.UNSUPPORTED,
                f"the video has an alpha channel ({video.pix_fmt}) that re-encoding would drop",
            )

        crf = params.crf
        bitrate = None if params.video_bitrate is None else str(params.video_bitrate)
        if crf is not None and encoder not in _CRF_ENCODERS:
            return skip(
                SkipKind.UNSUPPORTED,
                f"crf was requested, but {video.codec} video is encoded with {encoder}, which "
                "has no constant-quality mode: give these files a rule with video_bitrate",
            )
        if crf is None and bitrate is None:
            crf = _DEFAULT_CRF.get(encoder)
            if crf is None:
                bitrate = _DEFAULT_BITRATE[encoder]
        elif bitrate is None and encoder == "libvpx":
            # VP8 has no unconstrained quality mode: its CRF only works under a bitrate
            # ceiling, and without one libvpx falls back to a ceiling of 256 kbit/s.
            bitrate = _DEFAULT_BITRATE[encoder]

        audio: dict[str, str] = {"mode": "copy"}
        if encode_audio:
            codecs = list(dict.fromkeys(stream.codec for stream in info.audio))
            unknown = [codec for codec in codecs if codec not in AUDIO_ENCODERS]
            if unknown:
                return skip(
                    SkipKind.UNSUPPORTED,
                    f"audio_bitrate was requested, but audio codec {unknown[0]} cannot be "
                    f"re-encoded (supported: {', '.join(AUDIO_ENCODERS)})",
                )
            if len(codecs) > 1:
                return skip(
                    SkipKind.UNSUPPORTED,
                    "audio_bitrate was requested, but the audio streams use different codecs "
                    f"({', '.join(codecs)}) and one encoder cannot keep them all",
                )
            audio = {
                "mode": "encode",
                "encoder": AUDIO_ENCODERS[codecs[0]],
                "bitrate": str(params.audio_bitrate),
            }

        # Whatever the reason for re-encoding, 4:2:0 needs even dimensions; an odd frame
        # that fits the box only stays odd when the file is left alone.
        width, height = _even(size[0]), _even(size[1])
        fps_after = cap if cap is not None else video.fps
        return Action(
            transformer=self.name,
            source=logical,
            output=logical,
            params={
                "width": width,
                "height": height,
                "fps": cap,
                "video_encoder": encoder,
                "crf": crf,
                "video_bitrate": bitrate,
                "audio": dict(audio),
            },
            summary=_summary(video, width, height, fps_after, params, encode_audio),
            details={
                "before": info.as_dict(),
                "after": {
                    "width": width,
                    "height": height,
                    "fps": fps_after,
                    "video_codec": video.codec,
                    "video_encoder": encoder,
                    "crf": crf,
                    "video_bitrate": bitrate,
                    "audio": dict(audio),
                },
            },
        )


def _missing_tools(tools: Toolbox) -> str | None:
    """Return how to provide the tools media rules need and ``tools`` cannot find, if any."""
    hints = [tools.missing_hint(name) for name in _REQUIRED_TOOLS if tools.find(name) is None]
    return "; ".join(hints) if hints else None


def _summary(
    video: VideoStream,
    width: int,
    height: int,
    fps_after: float | None,
    params: MediaParams,
    encode_audio: bool,
) -> str:
    """Return the one-line description of an action: ``1920x1080@60 h264 -> 1280x720@30``.

    What the rule asked for explicitly is appended, as in ``(crf 20, audio 128k)``.
    """
    before = f"{video.width}x{video.height}{_at_fps(video.fps)} {video.codec}"
    after = f"{width}x{height}{_at_fps(fps_after)}"
    asked = []
    if params.crf is not None:
        asked.append(f"crf {params.crf}")
    if params.video_bitrate is not None:
        asked.append(f"video {params.video_bitrate}")
    if encode_audio:
        asked.append(f"audio {params.audio_bitrate}")
    return f"{before} -> {after}" + (f" ({', '.join(asked)})" if asked else "")


def _at_fps(fps: float | None) -> str:
    """Return ``"@<rate>"`` with at most three decimals, or ``""`` for an unknown rate."""
    if fps is None:
        return ""
    return "@" + f"{fps:.3f}".rstrip("0").rstrip(".")


def _even(value: int) -> int:
    """Round down to an even number, but never below 2."""
    return max(2, value - value % 2)


def _format_number(value: float) -> str:
    """Write a number the way a person would: ``30`` for 30.0, ``29.97`` for 29.97."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _extension(path: str) -> str:
    """Return the lower-case extension of a path written with either kind of separator."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _int_param(config: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"media params: {key!r} must be an integer >= {minimum}, got {value!r}")
    return value


def _text_param(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"media params: {key!r} must be non-empty text, got {value!r}")
    return value
