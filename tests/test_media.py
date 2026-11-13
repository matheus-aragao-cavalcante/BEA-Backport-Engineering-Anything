"""Tests for media probing (bea.formats.mediainfo) and the media transformer.

Everything here is synthetic. ffprobe reports are written by hand, and wherever the logic
under test is BEA's own the external tools are replaced by stand-ins. The tests that
really run ffmpeg (marked ``ffmpeg``) generate their clips with ffmpeg's built-in test
sources and are skipped where the tools or an encoder are not installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from bea.errors import FormatError, InputError, ToolError
from bea.formats.mediainfo import (
    AudioStream,
    MediaInfo,
    VideoStream,
    parse_ffprobe_json,
    probe_media,
    stderr_summary,
)
from bea.fsutil import ensure_dir, fs_path
from bea.model import FileEntry, GameInput, TitleInfo
from bea.profile.schema import MediaRule, MediaSettings, Profile, Target
from bea.toolbox import Toolbox, ToolInfo
from bea.transformers import media
from bea.transformers.base import Action, PlanContext, PlanItem, Skip, SkipKind, Transformer
from bea.transformers.media import (
    AUDIO_ENCODERS,
    MEDIA_EXTENSIONS,
    VIDEO_ENCODERS,
    MediaTransformer,
    build_ffmpeg_command,
    output_size,
)

MP4 = "mov,mp4,m4a,3gp,3g2,mj2"
WEBM = "matroska,webm"

# ----------------------------------------------------------------------------------
# parse_ffprobe_json
# ----------------------------------------------------------------------------------

#: A report as ffprobe prints it for an ordinary MP4: numbers partly as text.
MP4_REPORT = """
{
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_long_name": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10",
            "profile": "High",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "60/1",
            "avg_frame_rate": "60/1",
            "duration": "12.500000",
            "bit_rate": "7999872",
            "nb_frames": "750",
            "disposition": {"default": 1, "attached_pic": 0},
            "tags": {"language": "und", "handler_name": "VideoHandler"}
        },
        {
            "index": 1,
            "codec_name": "aac",
            "profile": "LC",
            "codec_type": "audio",
            "sample_fmt": "fltp",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "r_frame_rate": "0/0",
            "avg_frame_rate": "0/0",
            "bit_rate": "128000",
            "disposition": {"default": 1, "attached_pic": 0}
        }
    ],
    "format": {
        "filename": "intro.mp4",
        "nb_streams": 2,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
        "start_time": "0.000000",
        "duration": "12.500000",
        "size": "12705000",
        "bit_rate": "8131200",
        "probe_score": 100
    }
}
"""

MP4_INFO = MediaInfo(
    container=MP4,
    duration=12.5,
    size=12705000,
    bit_rate=8131200,
    video=(
        VideoStream(
            codec="h264",
            width=1920,
            height=1080,
            fps=60.0,
            fps_text="60/1",
            bit_rate=7999872,
            pix_fmt="yuv420p",
        ),
    ),
    audio=(AudioStream(codec="aac", sample_rate=48000, channels=2, bit_rate=128000),),
)


def video_document(**fields: Any) -> dict[str, Any]:
    """Return a report with one video stream made of ``fields``."""
    return {"streams": [{"codec_type": "video", **fields}]}


def test_parse_typical_mp4() -> None:
    assert parse_ffprobe_json(json.loads(MP4_REPORT)) == MP4_INFO


def test_parse_typical_webm() -> None:
    # Matroska records no per-stream bit rate, and NTSC rates come as a fraction.
    document = {
        "streams": [
            {
                "index": 0,
                "codec_name": "vp9",
                "codec_type": "video",
                "width": 1280,
                "height": 720,
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
                "disposition": {"default": 1, "attached_pic": 0},
                "tags": {"DURATION": "00:00:04.004000000"},
            },
            {
                "index": 1,
                "codec_name": "opus",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 2,
                "r_frame_rate": "0/0",
                "avg_frame_rate": "0/0",
            },
        ],
        "format": {
            "format_name": "matroska,webm",
            "duration": "4.004000",
            "size": "512000",
            "bit_rate": "1022977",
        },
    }
    info = parse_ffprobe_json(document)
    assert info.container == WEBM
    assert (info.duration, info.size, info.bit_rate) == (4.004, 512000, 1022977)
    (stream,) = info.video
    assert (stream.codec, stream.width, stream.height) == ("vp9", 1280, 720)
    assert stream.fps == pytest.approx(29.97002997, abs=1e-8)  # 30000 / 1001
    assert stream.fps_text == "30000/1001"
    assert stream.bit_rate is None
    assert info.audio == (AudioStream(codec="opus", sample_rate=48000, channels=2, bit_rate=None),)


@pytest.mark.parametrize(
    ("average", "base", "fps", "text"),
    [
        ("30000/1001", "30000/1001", 29.97002997, "30000/1001"),
        ("60/1", "60/1", 60.0, "60/1"),
        # The average rate wins over the base rate ...
        ("24000/1001", "24/1", 23.976023976, "24000/1001"),
        # ... and the base rate is used when the average is unknown or absent.
        ("0/0", "25/1", 25.0, "25/1"),
        (None, "50/1", 50.0, "50/1"),
        ("garbage", "15/2", 7.5, "15/2"),
        # Unknown.
        ("0/0", "0/0", None, None),
        (None, None, None, None),
        ("0/1", "0/0", None, None),
        ("30/0", "0/0", None, None),
        ("-30/1", "", None, None),
        ("1/2/3", "/", None, None),
        ("nan/1", "inf/1", None, None),
        (True, ["30/1"], None, None),
        # Tolerated spellings: a plain number, as text or not, and stray blanks.
        ("25", "0/0", 25.0, "25"),
        (25, "0/0", 25.0, "25"),
        (" 15/2 ", "0/0", 7.5, "15/2"),
    ],
)
def test_parse_frame_rate(average: Any, base: Any, fps: float | None, text: str | None) -> None:
    fields = {"avg_frame_rate": average, "r_frame_rate": base}
    document = video_document(**{key: value for key, value in fields.items() if value is not None})
    (stream,) = parse_ffprobe_json(document).video
    assert stream.fps == (None if fps is None else pytest.approx(fps, abs=1e-8))
    assert stream.fps_text == text


def test_parse_skips_attached_pictures() -> None:
    cover = {"codec_type": "video", "codec_name": "mjpeg", "width": 500, "height": 500}
    movie = {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}
    document = {
        "streams": [
            {**cover, "disposition": {"default": 0, "attached_pic": 1}},
            {**movie, "disposition": {"default": 1, "attached_pic": 0}},
            {**cover, "codec_name": "png", "disposition": {"attached_pic": "1"}},
            {**cover, "codec_name": "bmp", "disposition": {"attached_pic": True}},
            # Not attached pictures: no flag, and a disposition that is not an object.
            {**movie, "codec_name": "vp8", "disposition": {"default": 0}},
            {**movie, "codec_name": "vp9", "disposition": "attached_pic"},
        ]
    }
    info = parse_ffprobe_json(document)
    assert [stream.codec for stream in info.video] == ["h264", "vp8", "vp9"]
    assert {(stream.width, stream.height) for stream in info.video} == {(640, 360)}


def test_parse_keeps_stream_order_and_ignores_other_kinds() -> None:
    document = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
            {"codec_type": "subtitle", "codec_name": "subrip"},
            {"codec_type": "video", "codec_name": "hevc", "width": 320, "height": 180},
            {"codec_type": "data", "codec_name": "bin_data"},
            {"codec_type": "audio", "codec_name": "opus"},
            {"codec_type": "attachment", "codec_name": "ttf"},
            {"codec_name": "h264", "width": 99, "height": 99},  # no codec_type at all
        ],
        "format": {"format_name": "matroska,webm"},
    }
    info = parse_ffprobe_json(document)
    assert [(s.codec, s.width, s.height) for s in info.video] == [
        ("h264", 640, 360),
        ("hevc", 320, 180),
    ]
    assert [stream.codec for stream in info.audio] == ["aac", "opus"]


def test_parse_accepts_numbers_as_text_or_numbers() -> None:
    document = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "mpeg4",
                "width": "1280",
                "height": "720.0",
                "bit_rate": 2500000,
                "avg_frame_rate": "25/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "mp3",
                "sample_rate": 44100.0,
                "channels": "2",
                "bit_rate": "128000.0",
            },
        ],
        "format": {"format_name": "avi", "duration": 3.5, "size": 1000, "bit_rate": 2285},
    }
    info = parse_ffprobe_json(document)
    assert info.video == (VideoStream("mpeg4", 1280, 720, 25.0, "25/1", 2500000, None),)
    assert info.audio == (AudioStream("mp3", 44100, 2, 128000),)
    assert (info.container, info.duration, info.size, info.bit_rate) == ("avi", 3.5, 1000, 2285)
    for number in (info.video[0].width, info.video[0].height, info.audio[0].sample_rate):
        assert type(number) is int


def test_parse_tolerates_missing_fields() -> None:
    info = parse_ffprobe_json({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
    assert info == MediaInfo(
        container="unknown",
        duration=None,
        size=None,
        bit_rate=None,
        video=(VideoStream("unknown", 0, 0, None, None, None, None),),
        audio=(AudioStream("unknown", None, None, None),),
    )
    assert parse_ffprobe_json({"streams": []}) == MediaInfo("unknown", None, None, None, (), ())


def test_parse_treats_unusable_values_as_unknown() -> None:
    document = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "",
                "width": "N/A",
                "height": -720,
                "bit_rate": "0",
                "pix_fmt": 7,
                "avg_frame_rate": "N/A",
            },
            {
                "codec_type": "audio",
                "codec_name": None,
                "sample_rate": "fast",
                "channels": True,
                "bit_rate": [128000],
            },
        ],
        "format": {"format_name": "  ", "duration": "N/A", "size": -1, "bit_rate": "nan"},
    }
    assert parse_ffprobe_json(document) == MediaInfo(
        container="unknown",
        duration=None,
        size=None,
        bit_rate=None,
        video=(VideoStream("unknown", 0, 0, None, None, None, None),),
        audio=(AudioStream("unknown", None, None, None),),
    )
    # A "format" that is not an object is no better than a missing one.
    assert parse_ffprobe_json({"streams": [], "format": "mp4"}).container == "unknown"


def test_parse_keeps_zero_and_huge_container_numbers_exact() -> None:
    empty = parse_ffprobe_json({"streams": [], "format": {"duration": "0.000000", "size": "0"}})
    assert (empty.duration, empty.size) == (0.0, 0)
    # 2**53 + 1 does not survive a detour through float.
    huge = parse_ffprobe_json({"streams": [], "format": {"size": "9007199254740993"}})
    assert huge.size == 9007199254740993
    absurd = parse_ffprobe_json({"streams": [], "format": {"size": 10**400, "bit_rate": "1e999"}})
    assert (absurd.size, absurd.bit_rate) == (10**400, None)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # ffprobe's own VP8/VP9 decoders drop the alpha plane and only leave a tag.
        ({"pix_fmt": "yuv420p", "tags": {"alpha_mode": "1"}}, "yuva420p"),
        ({"pix_fmt": "yuv420p", "tags": {"ALPHA_MODE": 1}}, "yuva420p"),
        ({"pix_fmt": "yuv444p10le", "tags": {"alpha_mode": "1"}}, "yuva444p10le"),
        ({"pix_fmt": "yuva420p", "tags": {"alpha_mode": "1"}}, "yuva420p"),
        ({"pix_fmt": "yuv420p", "tags": {"alpha_mode": "0"}}, "yuv420p"),
        ({"pix_fmt": "yuv420p", "tags": {"language": "eng"}}, "yuv420p"),
        ({"pix_fmt": "yuv420p", "tags": "alpha_mode"}, "yuv420p"),
        ({"pix_fmt": "yuv420p"}, "yuv420p"),
        ({"tags": {"alpha_mode": "1"}}, None),
    ],
)
def test_parse_reports_the_alpha_plane_in_the_pixel_format(
    fields: dict[str, Any], expected: str | None
) -> None:
    (stream,) = parse_ffprobe_json(video_document(codec_name="vp9", **fields)).video
    assert stream.pix_fmt == expected


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"format": {"format_name": "avi"}},
        {"streams": None},
        {"streams": {}},
        {"streams": "video"},
        {"streams": 3},
        {"streams": [{"codec_type": "video"}, "oops"]},
        [],
        "streams",
    ],
)
def test_parse_rejects_documents_that_are_not_reports(document: Any) -> None:
    with pytest.raises(FormatError, match="not an ffprobe report"):
        parse_ffprobe_json(document)


def test_media_info_as_dict_is_plain_json() -> None:
    expected = {
        "container": MP4,
        "duration": 12.5,
        "size": 12705000,
        "bit_rate": 8131200,
        "video": [
            {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 60.0,
                "fps_text": "60/1",
                "bit_rate": 7999872,
                "pix_fmt": "yuv420p",
            }
        ],
        "audio": [{"codec": "aac", "sample_rate": 48000, "channels": 2, "bit_rate": 128000}],
    }
    assert MP4_INFO.as_dict() == expected
    assert json.loads(json.dumps(MP4_INFO.as_dict())) == expected
    empty = MediaInfo("unknown", None, None, None, (), ())
    assert empty.as_dict() == {
        "container": "unknown",
        "duration": None,
        "size": None,
        "bit_rate": None,
        "video": [],
        "audio": [],
    }


def test_media_info_is_immutable() -> None:
    with pytest.raises(AttributeError):
        MP4_INFO.container = "avi"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        MP4_INFO.video[0].width = 1  # type: ignore[misc]
    assert hash(MP4_INFO) == hash(parse_ffprobe_json(json.loads(MP4_REPORT)))


# ----------------------------------------------------------------------------------
# stderr_summary
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("output", [None, b"", "", b"\r\n  \r\n", "\n\n"])
def test_stderr_summary_of_nothing_is_empty(output: bytes | str | None) -> None:
    assert stderr_summary(output) == ""


def test_stderr_summary_joins_lines_and_drops_log_addresses() -> None:
    # What a Windows build prints for a file that is not a video (CRLF, a hex address).
    output = (
        b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0000029a765f6240] moov atom not found\r\n"
        b"clip.mp4: Invalid data found when processing input\r\n"
    )
    assert stderr_summary(output) == (
        "[mov,mp4,m4a,3gp,3g2,mj2] moov atom not found | "
        "clip.mp4: Invalid data found when processing input"
    )
    # Other platforms write the address with a 0x prefix; nested contexts lose theirs too.
    assert (
        stderr_summary("[vost#0:0/libx264 @ 0x55d0c8a4e6c0] [enc:libx264 @ 0x55d0c8a50000] no\n")
        == "[vost#0:0/libx264] [enc:libx264] no"
    )
    # Only an address inside a context is noise: other "@" text is left alone.
    assert stderr_summary("mail me @ 0000 or [x @ home]") == "mail me @ 0000 or [x @ home]"


def test_stderr_summary_keeps_the_cause_and_the_last_lines() -> None:
    lines = [f"line {number}" for number in range(1, 10)]
    text = "\n\n".join(lines) + "\n"
    assert stderr_summary(text) == "line 1 | ... | line 6 | line 7 | line 8 | line 9"
    assert stderr_summary(text, lines=3) == "line 1 | ... | line 8 | line 9"
    assert stderr_summary(text, lines=9) == " | ".join(lines)
    assert stderr_summary(text, lines=50) == " | ".join(lines)
    # Fewer than two lines cannot hold a cause and a consequence.
    assert stderr_summary(text, lines=0) == "line 1 | ... | line 9"
    assert stderr_summary("only one\n", lines=1) == "only one"


def test_stderr_summary_survives_bytes_that_are_not_utf8() -> None:
    assert stderr_summary(b"caf\xc3\xa9.mp4: No such file\n") == "café.mp4: No such file"
    assert stderr_summary(b"\xff\xfe broken \x80\n") == "\ufffd\ufffd broken \ufffd"


# ----------------------------------------------------------------------------------
# probe_media (the process is a stand-in: these tests need no ffprobe)
# ----------------------------------------------------------------------------------


class FakeRun:
    """Stands in for ``subprocess.run``: records every call and plays a canned answer."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes | None = b"",
        stderr: bytes = b"",
        *,
        raises: BaseException | None = None,
        writes: bytes | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        #: Content written to the last argument, the way ffmpeg writes its output file.
        self.writes = writes
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: Sequence[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append((list(command), options))
        if self.raises is not None:
            raise self.raises
        if self.writes is not None:
            Path(command[-1]).write_bytes(self.writes)
        return subprocess.CompletedProcess(list(command), self.returncode, self.stdout, self.stderr)

    @property
    def command(self) -> list[str]:
        """The argument list of the only call."""
        ((command, _options),) = self.calls
        return command

    @property
    def options(self) -> dict[str, Any]:
        """The keyword arguments of the only call."""
        ((_command, options),) = self.calls
        return options


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def assert_started_safely(fake: FakeRun) -> None:
    """Check the process was given an argument list, no shell and no standard input."""
    assert all(type(argument) is str for argument in fake.command)
    assert fake.options.get("stdin") is subprocess.DEVNULL
    assert not fake.options.get("shell", False)


def test_probe_media_runs_ffprobe_and_parses_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeRun(stdout=MP4_REPORT.encode()))
    clip = tmp_path / "movies" / "intro.mp4"
    ffprobe = tmp_path / "bin" / "ffprobe"

    assert probe_media(clip, ffprobe) == MP4_INFO
    assert fake.command == [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        fs_path(clip),
    ]
    assert_started_safely(fake)
    assert fake.options["timeout"] == 60.0
    assert fake.options.get("capture_output") is True


def test_probe_media_accepts_text_paths_and_a_custom_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeRun(stdout=b'{"streams": []}'))
    clip = tmp_path / "it's a clip, \u6620\u50cf & more.webm"
    info = probe_media(str(clip), "ffprobe", timeout=2.5)
    assert info == MediaInfo("unknown", None, None, None, (), ())
    assert fake.command[0] == "ffprobe"
    assert fake.command[-1] == fs_path(clip)
    assert fake.options["timeout"] == 2.5
    assert_started_safely(fake)


def test_probe_media_never_passes_a_name_that_reads_as_an_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeRun(stdout=b'{"streams": []}'))
    monkeypatch.chdir(tmp_path)
    probe_media("-version.mp4", "ffprobe")
    argument = fake.command[-1]
    assert os.path.isabs(argument)
    assert not argument.startswith("-")
    assert os.path.basename(argument) == "-version.mp4"
    assert os.path.samefile(os.path.dirname(argument), tmp_path)


def test_probe_media_decodes_reports_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ffprobe writes UTF-8 whatever the console code page is; the names below would be
    # mangled if the report were decoded with a legacy encoding.
    report = {
        "streams": [{"codec_type": "audio", "codec_name": "\u6620\u50cf"}],
        "format": {"format_name": "sm\u00f8rbr\u00f8d", "filename": "caf\u00e9.ogv"},
    }
    encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
    install(monkeypatch, FakeRun(stdout=b"\r\n" + encoded + b"\r\n"))
    info = probe_media(tmp_path / "clip.ogv", "ffprobe")
    assert info.container == "sm\u00f8rbr\u00f8d"
    assert [stream.codec for stream in info.audio] == ["\u6620\u50cf"]


def test_probe_media_reports_a_rejected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stderr = (
        b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0000029a765f6240] moov atom not found\r\n"
        b"clip.mp4: Invalid data found when processing input\r\n"
    )
    install(monkeypatch, FakeRun(returncode=1, stdout=b"{\r\n\r\n}\r\n", stderr=stderr))
    clip = tmp_path / "clip.mp4"
    with pytest.raises(ToolError) as caught:
        probe_media(clip, "ffprobe")
    assert str(caught.value) == (
        f"ffprobe cannot read {clip} (exit status 1): "
        "[mov,mp4,m4a,3gp,3g2,mj2] moov atom not found | "
        "clip.mp4: Invalid data found when processing input"
    )


def test_probe_media_reports_a_silent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeRun(returncode=3, stdout=b""))
    with pytest.raises(ToolError, match=r"exit status 3\): it printed no error message"):
        probe_media(tmp_path / "clip.mp4", "ffprobe")


def test_probe_media_reports_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expired = subprocess.TimeoutExpired(["ffprobe"], 2.5, stderr=b"[in#0 @ 0x1f] still reading\n")
    install(monkeypatch, FakeRun(raises=expired))
    clip = tmp_path / "clip.mp4"
    with pytest.raises(ToolError) as caught:
        probe_media(clip, "ffprobe", timeout=2.5)
    assert str(caught.value) == (
        f"ffprobe gave no answer within 2.5 seconds for {clip}: [in#0] still reading"
    )
    # Without anything on stderr the message simply ends after the path.
    install(monkeypatch, FakeRun(raises=subprocess.TimeoutExpired(["ffprobe"], 60.0)))
    with pytest.raises(ToolError) as caught:
        probe_media(clip, "ffprobe")
    assert str(caught.value) == f"ffprobe gave no answer within 60 seconds for {clip}"


def test_probe_media_reports_a_tool_that_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeRun(raises=FileNotFoundError(2, "No such file or directory")))
    missing = tmp_path / "nowhere" / "ffprobe"
    with pytest.raises(ToolError) as caught:
        probe_media(tmp_path / "clip.mp4", missing)
    message = str(caught.value)
    assert message.startswith(f"ffprobe could not be started ({missing}): ")
    assert "No such file or directory" in message
    assert isinstance(caught.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize(
    ("stdout", "problem"),
    [
        (b"", "is not JSON"),
        (b"ffprobe version 9", "is not JSON"),
        (b'{"streams": [', "is not JSON"),
        (b"\xff\xfe\x00{", "is not JSON"),
        (b"[]", "expected an object, got list"),
        (b'"streams"', "expected an object, got str"),
        (b"{}", 'it has no "streams" list'),
        (b'{"streams": {"0": {}}}', 'it has no "streams" list'),
        (b'{"streams": [1]}', "entry 0 of streams is not an object"),
    ],
)
def test_probe_media_reports_output_it_cannot_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes, problem: str
) -> None:
    install(monkeypatch, FakeRun(stdout=stdout))
    clip = tmp_path / "clip.mp4"
    with pytest.raises(ToolError) as caught:
        probe_media(clip, "ffprobe")
    assert str(clip) in str(caught.value)
    assert problem in str(caught.value)


# ----------------------------------------------------------------------------------
# output_size
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "box", "expected"),
    [
        # Same shape as the box: an exact fit.
        ((1920, 1080), (1280, 720), (1280, 720)),
        ((3840, 2160), (1280, 720), (1280, 720)),
        ((2560, 1440), (1920, 1080), (1920, 1080)),
        # Wider than the box: 800 * 1280 / 1920 = 533.33 -> 533 -> even 532.
        ((1920, 800), (1280, 720), (1280, 532)),
        # Portrait: 720 * 720 / 1280 = 405 exactly -> even 404.
        ((720, 1280), (1280, 720), (404, 720)),
        # 4:3 in a 16:9 box: 1440 * 720 / 1080 = 960.
        ((1440, 1080), (1280, 720), (960, 720)),
        # 1088 coded rows: 1920 * 720 / 1088 = 1270.59 -> 1270.
        ((1920, 1088), (1280, 720), (1270, 720)),
        # One pixel too large each way. Pinning the height gives 1279.22 x 720 -> 1278x720;
        # pinning the width gives 1280 x 720.44 -> 1280x720, which also fits and is closer
        # to the source's shape (1.7767): 1.7778 against 1.7750.
        ((1281, 721), (1280, 720), (1280, 720)),
        ((721, 1281), (720, 1280), (720, 1280)),
        # A common laptop panel: 1280 x 719.65 -> 1280x718, or 1280.6 x 720 -> 1280x720.
        # The source is 1.7786 wide: 1.7778 beats 1.7827.
        ((1366, 768), (1280, 720), (1280, 720)),
        # Here pinning the height (1278.8 x 720 -> 1278x720, shape 1.7750) is the closer
        # one to 1.7761; pinning the width would give 1280x720 (1.7778).
        ((1920, 1081), (1280, 720), (1278, 720)),
        # Only one side too large: keeping the other side whole (1280x100, 12.80) is
        # closer to 12.81 than scaling it to 99.9 -> 98 rows (13.06).
        ((1281, 100), (1280, 720), (1280, 100)),
        ((100, 721), (1280, 720), (100, 720)),
        # A box with odd limits is honoured with even numbers below them:
        # 1080 * 1279 / 1920 = 719.4 -> 718.
        ((1920, 1080), (1279, 719), (1278, 718)),
        ((1920, 1080), (1281, 721), (1280, 720)),
    ],
)
def test_output_size_table(
    source: tuple[int, int], box: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert output_size(*source, *box) == expected


@pytest.mark.parametrize(
    ("source", "box"),
    [
        ((640, 360), (1280, 720)),
        ((1280, 720), (1280, 720)),
        ((1279, 719), (1280, 720)),  # odd, but inside: left alone
        ((853, 481), (1280, 720)),
        ((1, 1), (1280, 720)),
        ((3, 5), (4, 6)),
        ((1280, 2), (1280, 720)),
    ],
)
def test_output_size_leaves_a_frame_inside_the_box_alone(
    source: tuple[int, int], box: tuple[int, int]
) -> None:
    assert output_size(*source, *box) == source


@pytest.mark.parametrize(
    ("source", "box", "expected"),
    [
        ((4000, 2), (1280, 720), (1280, 2)),  # 2 * 1280 / 4000 = 0.64
        ((4000, 1), (1280, 720), (1280, 2)),
        ((10000, 10), (1280, 720), (1280, 2)),  # 1.28 rows
        ((1, 4000), (1280, 720), (2, 720)),
        ((5000, 5000), (3, 3), (2, 2)),
        ((100, 100), (1, 1), (2, 2)),  # even a box smaller than 2 gets a frame of 2x2
        ((3, 3), (2, 2), (2, 2)),
    ],
)
def test_output_size_is_never_below_two(
    source: tuple[int, int], box: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert output_size(*source, *box) == expected


@pytest.mark.parametrize(
    "arguments",
    [(0, 720, 1280, 720), (1280, 0, 1280, 720), (1280, 720, 0, 720), (1280, 720, 1280, -1)],
)
def test_output_size_rejects_sizes_below_one(arguments: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        output_size(*arguments)


def test_output_size_properties() -> None:
    generator = random.Random(20240229)
    sizes = [(1920, 1080), (1920, 1088), (4096, 1716), (1080, 1920), (1281, 721), (999, 1001)]
    sizes += [(generator.randint(16, 5000), generator.randint(16, 5000)) for _ in range(300)]
    boxes = [(1280, 720), (1920, 1080), (720, 1280), (854, 480), (641, 359), (320, 240)]
    resized = 0
    for width, height in sizes:
        for max_width, max_height in boxes:
            out_width, out_height = output_size(width, height, max_width, max_height)
            # Width and height play the same role.
            assert output_size(height, width, max_height, max_width) == (out_height, out_width)
            if width <= max_width and height <= max_height:
                assert (out_width, out_height) == (width, height)
                continue
            resized += 1
            assert out_width % 2 == 0 and out_height % 2 == 0
            assert 2 <= out_width <= min(width, max_width)
            assert 2 <= out_height <= min(height, max_height)
            # Proportional: both sides are below a common multiple of the source by less
            # than the 2 pixels that rounding down to even can take away.
            assert abs(out_width * height - out_height * width) < 2 * max(width, height)
            # As large as the box allows: one side is at its limit (rounded to even).
            assert (
                out_width >= min(width, max_width) - 1 or out_height >= min(height, max_height) - 1
            )
            # At most 2 pixels are lost against the exact fit, on either side.
            scale = min(Fraction(max_width, width), Fraction(max_height, height))
            assert out_width > width * scale - 2 and out_height > height * scale - 2
            # The result is stable: it already fits, so it is its own answer.
            assert output_size(out_width, out_height, max_width, max_height) == (
                out_width,
                out_height,
            )
    assert resized > 500


# ----------------------------------------------------------------------------------
# build_ffmpeg_command
# ----------------------------------------------------------------------------------

COPY_AUDIO = {"mode": "copy"}


def params(**overrides: Any) -> dict[str, Any]:
    """Return complete media params (H.264 at CRF 23, 1280x720, audio copied)."""
    config: dict[str, Any] = {
        "width": 1280,
        "height": 720,
        "fps": None,
        "video_encoder": "libx264",
        "crf": 23,
        "video_bitrate": None,
        "audio": dict(COPY_AUDIO),
    }
    config.update(overrides)
    return config


def test_command_for_h264_with_crf_fps_cap_and_copied_audio() -> None:
    assert build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", params(fps=30.0)) == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", "in.mp4",
        "-map", "0:V:0", "-map", "0:a?", "-sn", "-dn",
        "-vf", "scale=1280:720:flags=lanczos,fps=30",
        "-c:v", "libx264", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "out.mp4",
    ]  # fmt: skip


def test_command_for_vp9_with_crf_and_encoded_audio_in_webm() -> None:
    config = params(
        width=854,
        height=480,
        video_encoder="libvpx-vp9",
        crf=32,
        audio={"mode": "encode", "encoder": "libopus", "bitrate": "96k"},
    )
    source = "/game/romfs/movies/intro.webm"
    output = "/work/staging/romfs/movies/intro.bea-tmp-0a1b2c3d.webm"
    assert build_ffmpeg_command("/opt/ffmpeg/bin/ffmpeg", source, output, config) == [
        "/opt/ffmpeg/bin/ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", source,
        "-map", "0:V:0", "-map", "0:a?", "-sn", "-dn",
        "-vf", "scale=854:480:flags=lanczos",
        # libvpx-vp9 only honours a CRF when the bitrate ceiling is explicitly zero.
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "libopus", "-b:a", "96k",
        output,
    ]  # fmt: skip


def test_command_for_a_bitrate_in_avi() -> None:
    config = params(width=640, height=480, video_encoder="mpeg4", crf=None, video_bitrate="2000k")
    assert build_ffmpeg_command("ffmpeg", "in.avi", "out.avi", config) == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", "in.avi",
        "-map", "0:V:0", "-map", "0:a?", "-sn", "-dn",
        "-vf", "scale=640:480:flags=lanczos",
        "-c:v", "mpeg4", "-b:v", "2000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "out.avi",
    ]  # fmt: skip


def quality_arguments(config: Mapping[str, Any]) -> list[str]:
    """Return what the command says between the encoder name and the pixel format."""
    command = build_ffmpeg_command("ffmpeg", "in.mkv", "out.mkv", config)
    return command[command.index("-c:v") + 2 : command.index("-pix_fmt")]


@pytest.mark.parametrize(
    ("encoder", "crf", "bitrate", "expected"),
    [
        ("libx264", 18, None, ["-crf", "18"]),
        ("libx264", 0, None, ["-crf", "0"]),  # zero is a real CRF (lossless), not "unset"
        ("libx265", 28, None, ["-crf", "28"]),
        ("libx264", None, "2500k", ["-b:v", "2500k"]),
        ("libtheora", None, "2000k", ["-b:v", "2000k"]),
        ("libvpx-vp9", 32, None, ["-crf", "32", "-b:v", "0"]),
        # A bitrate for VP9 is a plain bitrate: no zero ceiling next to it.
        ("libvpx-vp9", None, "4M", ["-b:v", "4M"]),
        # Both: this quality, but never above this bitrate (how VP8 does quality).
        ("libvpx", 30, "1500k", ["-crf", "30", "-b:v", "1500k"]),
        ("libvpx-vp9", 30, "1M", ["-crf", "30", "-b:v", "1M"]),
        ("libvpx", None, "1500k", ["-b:v", "1500k"]),
    ],
)
def test_command_quality_arguments(
    encoder: str, crf: int | None, bitrate: str | None, expected: list[str]
) -> None:
    config = params(video_encoder=encoder, crf=crf, video_bitrate=bitrate)
    assert quality_arguments(config) == expected


@pytest.mark.parametrize(
    ("fps", "expected"),
    [
        (None, "scale=1280:720:flags=lanczos"),
        (30, "scale=1280:720:flags=lanczos,fps=30"),
        (30.0, "scale=1280:720:flags=lanczos,fps=30"),
        (29.97, "scale=1280:720:flags=lanczos,fps=29.97"),
        (23.976, "scale=1280:720:flags=lanczos,fps=23.976"),
        (59.94, "scale=1280:720:flags=lanczos,fps=59.94"),
        (12.5, "scale=1280:720:flags=lanczos,fps=12.5"),
    ],
)
def test_command_filter_graph(fps: float | None, expected: str) -> None:
    command = build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", params(fps=fps))
    assert command[command.index("-vf") + 1] == expected
    assert command.count("-vf") == 1


@pytest.mark.parametrize(
    ("output", "faststart"),
    [
        ("out.mp4", True),
        ("out.m4v", True),
        ("out.mov", True),
        ("OUT.MP4", True),
        ("clip.bea-tmp-1a2b3c4d.Mov", True),
        ("C:\\work\\staging\\clip.bea-tmp-1a2b3c4d.mp4", True),
        ("\\\\?\\C:\\very\\deep\\clip.m4v", True),
        ("/work/staging/clip.mp4", True),
        ("out.webm", False),
        ("out.mkv", False),
        ("out.avi", False),
        ("out.ogv", False),
        ("/work/dir.mp4/clip.webm", False),
        ("C:\\work\\dir.mov\\clip.ogv", False),
        ("mp4", False),
        (".mp4", False),
    ],
)
def test_command_moves_the_index_to_the_front_only_for_the_mp4_family(
    output: str, faststart: bool
) -> None:
    command = build_ffmpeg_command("ffmpeg", "in.bin", output, params())
    assert command[-1] == output
    assert (command[-3:-1] == ["-movflags", "+faststart"]) is faststart
    assert ("-movflags" in command) is faststart


def test_command_passes_awkward_names_through_untouched() -> None:
    tool = "C:\\Program Files\\ffmpeg tools\\ffmpeg.exe"
    source = (
        '/game dump/romfs/it\'s a "quoted" clip; $HOME `x` & \u6620\u50cf sm\u00f8rbr\u00f8d.mp4'
    )
    output = "/work dir/out 100% (final) [v2] 'new'.mp4"
    command = build_ffmpeg_command(tool, source, output, params())
    assert command[0] == tool
    assert command[command.index("-i") + 1] == source
    assert command[-1] == output
    # One argument per value: nothing was quoted, escaped, split or joined.
    assert len(command) == len(build_ffmpeg_command("ffmpeg", "a.mp4", "b.mp4", params()))


def test_command_is_a_pure_function_of_json_params() -> None:
    config = params(
        fps=29.97,
        video_encoder="libvpx",
        crf=30,
        video_bitrate="1500k",
        audio={"mode": "encode", "encoder": "libvorbis", "bitrate": "96k"},
    )
    reference = json.dumps(config, sort_keys=True)
    first = build_ffmpeg_command("ffmpeg", "in.webm", "out.webm", config)
    assert json.dumps(config, sort_keys=True) == reference  # the params were not modified
    assert build_ffmpeg_command("ffmpeg", "in.webm", "out.webm", config) == first
    # The pipeline caches params as JSON: a round trip must give the same command.
    assert build_ffmpeg_command("ffmpeg", "in.webm", "out.webm", json.loads(reference)) == first
    # Keys the command does not use are ignored.
    assert build_ffmpeg_command("ffmpeg", "in.webm", "out.webm", {**config, "note": 1}) == first


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"width": None}, "'width'"),
        ({"width": 0}, "'width'"),
        ({"width": "1280"}, "'width'"),
        ({"width": 1280.0}, "'width'"),
        ({"height": True}, "'height'"),
        ({"height": -2}, "'height'"),
        ({"fps": 0}, "'fps'"),
        ({"fps": -30.0}, "'fps'"),
        ({"fps": "30"}, "'fps'"),
        ({"fps": True}, "'fps'"),
        ({"fps": float("nan")}, "'fps'"),
        ({"fps": float("inf")}, "'fps'"),
        ({"video_encoder": None}, "'video_encoder'"),
        ({"video_encoder": ""}, "'video_encoder'"),
        ({"video_encoder": 264}, "'video_encoder'"),
        ({"crf": None}, "'crf' and 'video_bitrate'"),
        ({"crf": -1}, "'crf'"),
        ({"crf": "23"}, "'crf'"),
        ({"crf": 23.0}, "'crf'"),
        ({"crf": None, "video_bitrate": ""}, "'video_bitrate'"),
        ({"crf": None, "video_bitrate": 2500000}, "'video_bitrate'"),
        ({"audio": None}, "'audio'"),
        ({"audio": "copy"}, "'audio'"),
        ({"audio": {}}, "'audio'"),
        ({"audio": {"mode": "drop"}}, "'audio'"),
        ({"audio": {"mode": "encode"}}, "'encoder'"),
        ({"audio": {"mode": "encode", "encoder": "aac"}}, "'bitrate'"),
        ({"audio": {"mode": "encode", "encoder": "aac", "bitrate": 128}}, "'bitrate'"),
    ],
)
def test_command_rejects_incomplete_params(change: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValueError, match="media params") as caught:
        build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", params(**change))
    assert complaint in str(caught.value)


def test_command_rejects_params_with_missing_keys() -> None:
    for key in ("width", "height", "video_encoder", "audio"):
        config = params()
        del config[key]
        with pytest.raises(ValueError, match=f"media params: '{key}'"):
            build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", config)
    # "fps" may be absent (no cap); the two quality keys may not both be.
    config = params()
    del config["fps"]
    assert "-vf" in build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", config)
    del config["crf"], config["video_bitrate"]
    with pytest.raises(ValueError, match="one of 'crf' and 'video_bitrate'"):
        build_ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", config)


# ----------------------------------------------------------------------------------
# MediaTransformer: constants and supports()
# ----------------------------------------------------------------------------------


def test_declared_tables() -> None:
    assert sorted(MEDIA_EXTENSIONS) == [".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ogv", ".webm"]
    assert isinstance(MEDIA_EXTENSIONS, frozenset)
    assert dict(VIDEO_ENCODERS) == {
        "h264": "libx264",
        "hevc": "libx265",
        "vp9": "libvpx-vp9",
        "vp8": "libvpx",
        "mpeg4": "mpeg4",
        "theora": "libtheora",
    }
    assert dict(AUDIO_ENCODERS) == {
        "aac": "aac",
        "opus": "libopus",
        "vorbis": "libvorbis",
        "mp3": "libmp3lame",
    }


def test_transformer_identity() -> None:
    transformer = MediaTransformer()
    assert isinstance(transformer, Transformer)
    assert (transformer.name, transformer.version) == ("media", 1)
    assert (MediaTransformer.name, MediaTransformer.version) == ("media", 1)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("romfs/movies/intro.mp4", True),
        ("romfs/intro.m4v", True),
        ("romfs/a/b/c/clip.mov", True),
        ("romfs/clip.webm", True),
        ("romfs/clip.mkv", True),
        ("romfs/clip.avi", True),
        ("romfs/clip.ogv", True),
        ("romfs/Movies/INTRO.MP4", True),
        ("romfs/clip.final.WebM", True),
        ("romfs/name with spaces & \u6620\u50cf.mkv", True),
        # Not a container this transformer processes.
        ("romfs/clip.ivf", False),
        ("romfs/clip.ogg", False),
        ("romfs/clip.mp4.bak", False),
        ("romfs/clip", False),
        ("romfs/mp4", False),
        ("romfs/.mp4", False),
        ("romfs/dir.mp4/readme.txt", False),
        # Not a RomFS file.
        ("exefs/main.mp4", False),
        ("exefs_patches/intro.mp4", False),
        ("movies/intro.mp4", False),
        ("romfsx/intro.mp4", False),
        ("", False),
    ],
)
def test_supports(path: str, expected: bool) -> None:
    assert MediaTransformer().supports(path) is expected


# ----------------------------------------------------------------------------------
# MediaTransformer.plan (ffprobe is a stand-in: these tests need no tools)
# ----------------------------------------------------------------------------------


def make_video(
    codec: str = "h264",
    width: int = 1920,
    height: int = 1080,
    fps: float | None = 60.0,
    pix_fmt: str | None = "yuv420p",
) -> VideoStream:
    text = None if fps is None else f"{fps:g}/1"
    return VideoStream(codec, width, height, fps, text, None, pix_fmt)


def make_audio(codec: str = "aac") -> AudioStream:
    return AudioStream(codec, 48000, 2, 128000)


def make_info(*streams: VideoStream | AudioStream, container: str = MP4) -> MediaInfo:
    return MediaInfo(
        container=container,
        duration=10.0,
        size=1000000,
        bit_rate=800000,
        video=tuple(stream for stream in streams if isinstance(stream, VideoStream)),
        audio=tuple(stream for stream in streams if isinstance(stream, AudioStream)),
    )


FULL_HD = make_info(make_video(), make_audio())


def make_game(root: Path, files: Sequence[str]) -> GameInput:
    """Return a game whose RomFS holds ``files`` (placeholders unless they already exist)."""
    romfs = root / "romfs"
    romfs.mkdir(parents=True, exist_ok=True)
    entries = []
    for relative in files:
        path = romfs.joinpath(*relative.split("/"))
        if not os.path.exists(fs_path(path)):
            ensure_dir(path.parent)
            Path(fs_path(path)).write_bytes(b"placeholder: ffprobe is a stand-in here")
        status = os.stat(fs_path(path))
        entries.append(FileEntry(relative, status.st_size, status.st_mtime_ns))
    return GameInput(
        root=root,
        exefs_dir=None,
        romfs_dir=romfs,
        nacp_path=None,
        title=TitleInfo(),
        modules=(),
        exefs_files=(),
        romfs_files=tuple(entries),
    )


def make_context(
    game: GameInput,
    rules: Sequence[MediaRule],
    tools: Toolbox,
    *,
    enabled: bool = True,
    target: Target | None = None,
) -> PlanContext:
    profile = Profile(
        title_id=None,
        target=target or Target(),
        media=MediaSettings(enabled=enabled, rules=tuple(rules)),
    )
    return PlanContext(game=game, profile=profile, tools=tools)


def stand_in_tools(directory: Path, *names: str) -> Toolbox:
    """Return a toolbox that finds exactly ``names``: empty files that are never run."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        executable = directory / name
        executable.write_bytes(b"")
        executable.chmod(0o755)
    # An empty PATH and no BEA_* variable: the machine's real tools stay invisible.
    return Toolbox(directory, environ={"PATH": ""})


class ForbiddenToolbox(Toolbox):
    """A toolbox that fails the test as soon as a tool is looked for."""

    def locate(self, name: str) -> ToolInfo:
        raise AssertionError(f"{name} was looked for although no media rule selects a file")


class FakeProbe:
    """Stands in for ``probe_media``: answers by file name and records what it was asked."""

    def __init__(self, answers: Mapping[str, MediaInfo | Exception]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[Path, Path]] = []

    def __call__(
        self, path: str | os.PathLike[str], ffprobe: str | os.PathLike[str], **options: Any
    ) -> MediaInfo:
        assert not options, "plan is expected to probe with the default timeout"
        self.calls.append((Path(path), Path(ffprobe)))
        answer = self.answers[Path(path).name]
        if isinstance(answer, Exception):
            raise answer
        return answer


def snapshot(root: Path) -> dict[str, bytes]:
    """Return every file below ``root`` with its content."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


PlanOne = Callable[..., list[PlanItem]]


@pytest.fixture
def plan_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlanOne:
    """Plan a game holding one video that ffprobe describes as ``answer``."""

    def run(
        answer: MediaInfo | Exception,
        rule: MediaRule | None = None,
        *,
        path: str = "movies/intro.mp4",
        target: Target | None = None,
    ) -> list[PlanItem]:
        game = make_game(tmp_path / "game", [path])
        monkeypatch.setattr(media, "probe_media", FakeProbe({path.rsplit("/", 1)[-1]: answer}))
        tools = stand_in_tools(tmp_path / "tools", "ffmpeg", "ffprobe")
        context = make_context(game, [rule or MediaRule(match=("**",))], tools, target=target)
        return list(MediaTransformer(tools).plan(context))

    return run


def only_action(items: list[PlanItem]) -> Action:
    (item,) = items
    assert isinstance(item, Action), item
    return item


def only_skip(items: list[PlanItem]) -> Skip:
    (item,) = items
    assert isinstance(item, Skip), item
    return item


def test_plan_downscales_and_caps_a_video(plan_one: PlanOne) -> None:
    action = only_action(plan_one(FULL_HD, MediaRule(match=("movies/**",), max_fps=30.0)))
    assert action.transformer == "media"
    assert action.source == action.output == "romfs/movies/intro.mp4"
    assert action.params == {
        "width": 1280,
        "height": 720,
        "fps": 30.0,
        "video_encoder": "libx264",
        "crf": 23,
        "video_bitrate": None,
        "audio": {"mode": "copy"},
    }
    assert action.summary == "1920x1080@60 h264 -> 1280x720@30"
    assert action.details == {
        "before": {
            "container": MP4,
            "duration": 10.0,
            "size": 1000000,
            "bit_rate": 800000,
            "video": [
                {
                    "codec": "h264",
                    "width": 1920,
                    "height": 1080,
                    "fps": 60.0,
                    "fps_text": "60/1",
                    "bit_rate": None,
                    "pix_fmt": "yuv420p",
                }
            ],
            "audio": [{"codec": "aac", "sample_rate": 48000, "channels": 2, "bit_rate": 128000}],
        },
        "after": {
            "width": 1280,
            "height": 720,
            "fps": 30.0,
            "video_codec": "h264",
            "video_encoder": "libx264",
            "crf": 23,
            "video_bitrate": None,
            "audio": {"mode": "copy"},
        },
    }
    # Params and details are what the cache and the reports store: plain JSON, and
    # enough to rebuild the command after a round trip.
    stored = json.loads(json.dumps(action.params))
    assert stored == action.params
    assert json.loads(json.dumps(action.details)) == action.details
    assert build_ffmpeg_command("ffmpeg", "a.mp4", "b.mp4", stored) == build_ffmpeg_command(
        "ffmpeg", "a.mp4", "b.mp4", action.params
    )


def test_plan_probes_the_file_on_disk_with_the_context_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = "movies/op & ed's sm\u00f8rbr\u00f8d \u6620\u50cf (v2).mp4"
    game = make_game(tmp_path / "game dump", [relative])
    probe = FakeProbe({relative.rsplit("/", 1)[-1]: FULL_HD})
    monkeypatch.setattr(media, "probe_media", probe)
    tools = stand_in_tools(tmp_path / "tools", "ffmpeg", "ffprobe")
    before = snapshot(tmp_path)

    # The transformer's own toolbox is for transform(); planning must use the context's.
    transformer = MediaTransformer(ForbiddenToolbox(tmp_path / "unused"))
    action = only_action(list(transformer.plan(make_context(game, [MediaRule(("**",))], tools))))

    assert action.source == f"romfs/{relative}"
    expected_file = tmp_path / "game dump" / "romfs" / "movies" / relative.rsplit("/", 1)[-1]
    assert probe.calls == [(expected_file, tools.require("ffprobe"))]
    assert snapshot(tmp_path) == before  # planning writes nothing


def test_plan_yields_nothing_without_media_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = make_game(tmp_path / "game", ["movies/intro.mp4", "movies/outro.webm"])
    probe = FakeProbe({})
    monkeypatch.setattr(media, "probe_media", probe)
    tools = ForbiddenToolbox(tmp_path / "tools")
    rule = MediaRule(match=("**",), max_fps=30.0)

    no_rules = make_context(game, [], tools)
    disabled = make_context(game, [rule], tools, enabled=False)
    assert list(MediaTransformer(tools).plan(no_rules)) == []
    assert list(MediaTransformer(tools).plan(disabled)) == []
    assert probe.calls == []


def test_plan_only_looks_at_videos_selected_by_a_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        "audio/theme.ogg",
        "movies/intro.mp4",
        "movies/keep/logo.mp4",
        "movies/notes.txt",
        "movies/outro.MP4",
        "other/clip.mp4",
        "ui/menu.webm",
    ]
    game = make_game(tmp_path / "game", files)
    probe = FakeProbe({"intro.mp4": FULL_HD, "outro.MP4": FULL_HD})
    monkeypatch.setattr(media, "probe_media", probe)
    tools = stand_in_tools(tmp_path / "tools", "ffmpeg", "ffprobe")
    rules = [
        MediaRule(match=("movies/keep/**",), skip=True),
        MediaRule(match=("movies/**",), exclude=("*.webm",)),
        MediaRule(match=("audio/**", "*.txt")),  # selects files that are not videos
    ]
    items = list(MediaTransformer(tools).plan(make_context(game, rules, tools)))

    # Only the two selected videos were probed and planned, in RomFS order.
    assert [path.name for path, _ffprobe in probe.calls] == ["intro.mp4", "outro.MP4"]
    assert [item.source for item in items] == ["romfs/movies/intro.mp4", "romfs/movies/outro.MP4"]
    assert all(isinstance(item, Action) for item in items)


def test_plan_with_rules_that_select_nothing_needs_no_tools(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", ["movies/intro.mp4", "data/table.bin"])
    tools = ForbiddenToolbox(tmp_path / "tools")
    rules = [MediaRule(match=("cutscenes/**",)), MediaRule(match=("*.bin",))]
    assert list(MediaTransformer(tools).plan(make_context(game, rules, tools))) == []


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        (("ffprobe",), ("ffmpeg",)),
        (("ffmpeg",), ("ffprobe",)),
        ((), ("ffmpeg", "ffprobe")),
    ],
)
def test_plan_reports_missing_tools_for_every_selected_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
    missing: tuple[str, ...],
) -> None:
    game = make_game(tmp_path / "game", ["a.mp4", "b.webm", "c.txt"])
    probe = FakeProbe({})
    monkeypatch.setattr(media, "probe_media", probe)
    tools = stand_in_tools(tmp_path / "tools", *present)
    items = list(MediaTransformer(tools).plan(make_context(game, [MediaRule(("**",))], tools)))

    reason = "; ".join(tools.missing_hint(name) for name in missing)
    assert items == [
        Skip("media", "romfs/a.mp4", SkipKind.UNSUPPORTED, reason),
        Skip("media", "romfs/b.webm", SkipKind.UNSUPPORTED, reason),
    ]
    for name in missing:
        assert f"BEA_{name.upper()}" in reason  # the hint says how to provide the tool
    assert not any(item.is_error for item in items if isinstance(item, Skip))
    assert probe.calls == []


def test_plan_reports_a_file_ffprobe_cannot_read(plan_one: PlanOne) -> None:
    failure = ToolError("ffprobe cannot read intro.mp4 (exit status 1): moov atom not found")
    skip = only_skip(plan_one(failure))
    assert skip == Skip("media", "romfs/movies/intro.mp4", SkipKind.INVALID, str(failure))
    # Trouble with the file system is the file's problem too, not a crash.
    skip = only_skip(plan_one(PermissionError(13, "Permission denied")))
    assert skip.kind is SkipKind.INVALID
    assert "Permission denied" in skip.reason


def test_plan_reports_a_path_it_cannot_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = make_game(tmp_path / "game", ["ok.mp4"])
    entries = (FileEntry("movies/../escape.mp4", 1, 1), *game.romfs_files)
    game = GameInput(
        root=game.root,
        exefs_dir=None,
        romfs_dir=game.romfs_dir,
        nacp_path=None,
        title=TitleInfo(),
        modules=(),
        exefs_files=(),
        romfs_files=entries,
    )
    probe = FakeProbe({"ok.mp4": FULL_HD})
    monkeypatch.setattr(media, "probe_media", probe)
    tools = stand_in_tools(tmp_path / "tools", "ffmpeg", "ffprobe")
    first, second = MediaTransformer(tools).plan(make_context(game, [MediaRule(("**",))], tools))

    assert isinstance(first, Skip)
    assert (first.source, first.kind) == ("romfs/movies/../escape.mp4", SkipKind.INVALID)
    assert "movies/../escape.mp4" in first.reason
    assert isinstance(second, Action)  # one bad entry does not stop the others
    assert [path.name for path, _ffprobe in probe.calls] == ["ok.mp4"]


def test_plan_reports_a_file_without_video(plan_one: PlanOne) -> None:
    skip = only_skip(plan_one(make_info(make_audio()), MediaRule(("**",), audio_bitrate="96k")))
    assert (skip.source, skip.kind) == ("romfs/movies/intro.mp4", SkipKind.UNSUPPORTED)
    assert "no video stream" in skip.reason
    assert only_skip(plan_one(make_info())).kind is SkipKind.UNSUPPORTED


@pytest.mark.parametrize(("width", "height"), [(0, 0), (1920, 0), (0, 1080)])
def test_plan_reports_a_video_without_frame_size(
    plan_one: PlanOne, width: int, height: int
) -> None:
    skip = only_skip(plan_one(make_info(make_video(width=width, height=height))))
    assert skip.kind is SkipKind.INVALID
    assert "frame size" in skip.reason


@pytest.mark.parametrize("codec", ["av1", "mpeg2video", "prores", "wmv3", "unknown"])
def test_plan_reports_a_video_codec_it_cannot_encode(plan_one: PlanOne, codec: str) -> None:
    skip = only_skip(plan_one(make_info(make_video(codec=codec), make_audio())))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert f"video codec {codec} " in skip.reason
    # The same reason for every file of that codec, so that reports can group them.
    assert "intro" not in skip.reason


@pytest.mark.parametrize("codec", ["ac3", "pcm_s16le", "flac", "unknown"])
def test_plan_reports_an_audio_codec_it_cannot_encode(plan_one: PlanOne, codec: str) -> None:
    file = make_info(make_video(width=640, height=360, fps=30.0), make_audio(codec))
    skip = only_skip(plan_one(file, MediaRule(("**",), audio_bitrate="128k")))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert f"audio codec {codec} " in skip.reason
    assert "audio_bitrate" in skip.reason
    # Without audio_bitrate the audio is copied, whatever its codec: nothing to refuse.
    assert plan_one(file) == []
    action = only_action(plan_one(file, MediaRule(("**",), max_width=320)))
    assert action.params["audio"] == {"mode": "copy"}


def test_plan_reports_audio_streams_of_different_codecs(plan_one: PlanOne) -> None:
    rule = MediaRule(("**",), audio_bitrate="128k")
    mixed = make_info(make_video(), make_audio("aac"), make_audio("opus"), make_audio("aac"))
    skip = only_skip(plan_one(mixed, rule))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert "different codecs (aac, opus)" in skip.reason
    # An unsupported codec is named even when a supported one comes first.
    worse = make_info(make_video(), make_audio("aac"), make_audio("ac3"))
    assert "audio codec ac3 " in only_skip(plan_one(worse, rule)).reason
    # Several streams of one codec are fine.
    same = make_info(make_video(), make_audio("opus"), make_audio("opus"), container=WEBM)
    assert only_action(plan_one(same, rule)).params["audio"] == {
        "mode": "encode",
        "encoder": "libopus",
        "bitrate": "128k",
    }


@pytest.mark.parametrize(
    ("codec", "encoder", "crf", "bitrate"),
    [
        ("h264", "libx264", 23, None),
        ("hevc", "libx265", 28, None),
        ("vp9", "libvpx-vp9", 32, None),
        ("vp8", "libvpx", None, "1500k"),
        ("mpeg4", "mpeg4", None, "2000k"),
        ("theora", "libtheora", None, "2000k"),
    ],
)
def test_plan_chooses_the_encoder_and_its_default_quality(
    plan_one: PlanOne, codec: str, encoder: str, crf: int | None, bitrate: str | None
) -> None:
    action = only_action(plan_one(make_info(make_video(codec=codec), make_audio())))
    assert action.params["video_encoder"] == encoder
    assert (action.params["crf"], action.params["video_bitrate"]) == (crf, bitrate)
    assert action.summary == f"1920x1080@60 {codec} -> 1280x720@60"
    # The params alone determine the command, default quality included.
    command = build_ffmpeg_command("ffmpeg", "in.mkv", "out.mkv", action.params)
    assert command[command.index("-c:v") + 1] == encoder


def test_every_declared_video_codec_has_a_default_quality(plan_one: PlanOne) -> None:
    for codec in VIDEO_ENCODERS:
        chosen = only_action(plan_one(make_info(make_video(codec=codec)))).params
        assert (chosen["crf"] is None) != (chosen["video_bitrate"] is None), codec


def test_plan_passes_requested_quality_through(plan_one: PlanOne) -> None:
    small = make_info(make_video(width=1280, height=720, fps=30.0), make_audio())

    quality = only_action(plan_one(small, MediaRule(("**",), crf=18)))
    assert (quality.params["crf"], quality.params["video_bitrate"]) == (18, None)
    assert (quality.params["width"], quality.params["height"]) == (1280, 720)
    assert quality.params["fps"] is None
    assert quality.summary == "1280x720@30 h264 -> 1280x720@30 (crf 18)"

    lossless = only_action(plan_one(small, MediaRule(("**",), crf=0)))
    assert lossless.params["crf"] == 0  # zero is a request, not "unset"

    rate = only_action(plan_one(small, MediaRule(("**",), video_bitrate="2500k")))
    assert (rate.params["crf"], rate.params["video_bitrate"]) == (None, "2500k")
    assert rate.summary == "1280x720@30 h264 -> 1280x720@30 (video 2500k)"

    sound = only_action(plan_one(small, MediaRule(("**",), audio_bitrate="96k")))
    assert sound.params["audio"] == {"mode": "encode", "encoder": "aac", "bitrate": "96k"}
    assert (sound.params["crf"], sound.params["video_bitrate"]) == (23, None)
    assert sound.summary == "1280x720@30 h264 -> 1280x720@30 (audio 96k)"

    everything = MediaRule(("**",), max_height=360, max_fps=24.0, crf=20, audio_bitrate="64k")
    action = only_action(plan_one(small, everything))
    assert action.params == {
        "width": 640,
        "height": 360,
        "fps": 24.0,
        "video_encoder": "libx264",
        "crf": 20,
        "video_bitrate": None,
        "audio": {"mode": "encode", "encoder": "aac", "bitrate": "64k"},
    }
    assert action.summary == "1280x720@30 h264 -> 640x360@24 (crf 20, audio 64k)"


@pytest.mark.parametrize(
    ("audio_codec", "encoder"),
    [("aac", "aac"), ("opus", "libopus"), ("vorbis", "libvorbis"), ("mp3", "libmp3lame")],
)
def test_plan_keeps_the_audio_codec_when_a_bitrate_is_requested(
    plan_one: PlanOne, audio_codec: str, encoder: str
) -> None:
    file = make_info(make_video(), make_audio(audio_codec))
    action = only_action(plan_one(file, MediaRule(("**",), audio_bitrate="128k")))
    assert action.params["audio"] == {"mode": "encode", "encoder": encoder, "bitrate": "128k"}


def test_plan_gives_vp8_quality_a_bitrate_ceiling(plan_one: PlanOne) -> None:
    file = make_info(make_video(codec="vp8"), make_audio("vorbis"), container=WEBM)
    action = only_action(plan_one(file, MediaRule(("**",), crf=30), path="movies/intro.webm"))
    # libvpx has no unconstrained quality mode for VP8: a CRF alone would be capped at
    # its 256 kbit/s default, so the default VP8 bitrate becomes the ceiling.
    assert (action.params["crf"], action.params["video_bitrate"]) == (30, "1500k")
    assert quality_arguments(action.params) == ["-crf", "30", "-b:v", "1500k"]
    # A requested bitrate is used as it is.
    rate = only_action(plan_one(file, MediaRule(("**",), video_bitrate="900k")))
    assert (rate.params["crf"], rate.params["video_bitrate"]) == (None, "900k")


@pytest.mark.parametrize(("codec", "encoder"), [("mpeg4", "mpeg4"), ("theora", "libtheora")])
def test_plan_refuses_a_crf_the_encoder_does_not_have(
    plan_one: PlanOne, codec: str, encoder: str
) -> None:
    file = make_info(make_video(codec=codec), make_audio("mp3"))
    skip = only_skip(plan_one(file, MediaRule(("**",), crf=23)))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert "crf" in skip.reason and codec in skip.reason and encoder in skip.reason
    assert "video_bitrate" in skip.reason  # what to do about it
    assert (
        only_action(plan_one(file, MediaRule(("**",), video_bitrate="1800k"))).params[
            "video_bitrate"
        ]
        == "1800k"
    )


@pytest.mark.parametrize("pix_fmt", ["yuva420p", "yuva444p10le", "rgba", "gbrap", "bgra"])
def test_plan_refuses_to_drop_an_alpha_channel(plan_one: PlanOne, pix_fmt: str) -> None:
    file = make_info(make_video(codec="vp9", pix_fmt=pix_fmt), container=WEBM)
    skip = only_skip(plan_one(file))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert "alpha" in skip.reason and pix_fmt in skip.reason
    # A transparent video that already complies is simply left alone.
    small = make_info(make_video(codec="vp9", width=640, height=360, pix_fmt=pix_fmt))
    assert plan_one(small) == []


@pytest.mark.parametrize("pix_fmt", ["yuv420p", "yuv420p10le", "yuvj420p", "gbrp", "rgb24", None])
def test_plan_accepts_opaque_pixel_formats(plan_one: PlanOne, pix_fmt: str | None) -> None:
    only_action(plan_one(make_info(make_video(pix_fmt=pix_fmt))))


@pytest.mark.parametrize(
    ("fps", "max_fps", "capped"),
    [
        (60.0, 30.0, True),
        (60.0, 59.94, True),
        (30.02, 30.0, True),
        (50.0, 25, True),
        # NTSC 29.97 and tiny container rounding are not "above 30".
        (30000 / 1001, 30.0, False),
        (30.005, 30.0, False),
        (30.0, 30.0, False),
        (60000 / 1001, 59.94, False),
        (24.0, 30.0, False),
        # An unknown rate cannot be said to exceed anything.
        (None, 30.0, False),
        # No cap requested.
        (120.0, None, False),
    ],
)
def test_plan_frame_rate_cap(
    plan_one: PlanOne, fps: float | None, max_fps: float | None, capped: bool
) -> None:
    file = make_info(make_video(fps=fps), make_audio())
    action = only_action(plan_one(file, MediaRule(("**",), max_fps=max_fps)))
    assert action.params["fps"] == (max_fps if capped else None)
    assert action.details["after"]["fps"] == (max_fps if capped else fps)
    # The same decision on a file that needs nothing else: a cap alone is work to do,
    # the absence of one is not.
    small = make_info(make_video(width=640, height=360, fps=fps), make_audio())
    items = plan_one(small, MediaRule(("**",), max_fps=max_fps))
    assert len(items) == (1 if capped else 0)
    if capped:
        assert only_action(items).params["width"] == 640


@pytest.mark.parametrize(
    ("fps", "max_fps", "summary"),
    [
        (60.0, 30.0, "1920x1080@60 h264 -> 1280x720@30"),
        (60000 / 1001, 30000 / 1001, "1920x1080@59.94 h264 -> 1280x720@29.97"),
        (24000 / 1001, None, "1920x1080@23.976 h264 -> 1280x720@23.976"),
        (25.0, 30.0, "1920x1080@25 h264 -> 1280x720@25"),
        (None, 30.0, "1920x1080 h264 -> 1280x720"),
        (100.0, 50.0, "1920x1080@100 h264 -> 1280x720@50"),
    ],
)
def test_plan_summary(
    plan_one: PlanOne, fps: float | None, max_fps: float | None, summary: str
) -> None:
    file = make_info(make_video(fps=fps), make_audio())
    assert only_action(plan_one(file, MediaRule(("**",), max_fps=max_fps))).summary == summary


@pytest.mark.parametrize(
    ("size", "rule_box", "target", "expected"),
    [
        # The profile's target is the default box ...
        ((1920, 1080), (None, None), (1280, 720), (1280, 720)),
        ((3840, 2160), (None, None), (1920, 1080), (1920, 1080)),
        # ... and a rule overrides either side of it.
        ((3840, 2160), (None, 540), (1920, 1080), (960, 540)),
        ((3840, 2160), (1024, None), (1920, 1080), (1024, 576)),
        ((1920, 1080), (1920, 1080), (1280, 720), None),
        # 1920x800 in 1280x720: 533.33 rows -> 532.
        ((1920, 800), (None, None), (1280, 720), (1280, 532)),
        ((640, 360), (None, None), (1280, 720), None),
    ],
)
def test_plan_uses_the_rule_box_then_the_target(
    plan_one: PlanOne,
    size: tuple[int, int],
    rule_box: tuple[int | None, int | None],
    target: tuple[int, int],
    expected: tuple[int, int] | None,
) -> None:
    file = make_info(make_video(width=size[0], height=size[1], fps=30.0), make_audio())
    rule = MediaRule(("**",), max_width=rule_box[0], max_height=rule_box[1])
    items = plan_one(file, rule, target=Target(*target))
    if expected is None:
        assert items == []
    else:
        chosen = only_action(items).params
        assert (chosen["width"], chosen["height"]) == expected


def test_plan_leaves_a_complying_video_alone(plan_one: PlanOne) -> None:
    rule = MediaRule(("**",), max_fps=30.0)
    assert (
        plan_one(make_info(make_video(width=1280, height=720, fps=30.0), make_audio()), rule) == []
    )
    # Odd dimensions inside the box are no reason to re-encode ...
    odd = make_info(make_video(width=853, height=481, fps=30.0), make_audio())
    assert plan_one(odd, rule) == []
    # ... nor is a codec BEA could not encode, as long as nothing has to change ...
    foreign = make_info(make_video(codec="av1", width=640, height=360, fps=24.0), make_audio("ac3"))
    assert plan_one(foreign, rule) == []
    # ... nor an audio bitrate for a file without audio.
    silent = make_info(make_video(width=640, height=360, fps=30.0))
    assert plan_one(silent, MediaRule(("**",), max_fps=30.0, audio_bitrate="96k")) == []


def test_plan_ignores_an_audio_bitrate_when_there_is_no_audio(plan_one: PlanOne) -> None:
    silent = make_info(make_video())
    action = only_action(plan_one(silent, MediaRule(("**",), audio_bitrate="96k")))
    assert action.params["audio"] == {"mode": "copy"}
    assert action.summary == "1920x1080@60 h264 -> 1280x720@60"


def test_plan_makes_odd_dimensions_even_when_it_re_encodes_anyway(plan_one: PlanOne) -> None:
    # 853x481 fits the box, so its size is "unchanged"; but the frame-rate cap forces a
    # re-encode, and 4:2:0 encoders reject odd dimensions.
    odd = make_info(make_video(width=853, height=481, fps=60.0), make_audio())
    action = only_action(plan_one(odd, MediaRule(("**",), max_fps=30.0)))
    assert (action.params["width"], action.params["height"]) == (852, 480)
    assert action.summary == "853x481@60 h264 -> 852x480@30"
    tiny = make_info(make_video(width=1, height=3, fps=30.0))
    action = only_action(plan_one(tiny, MediaRule(("**",), crf=30)))
    assert (action.params["width"], action.params["height"]) == (2, 2)


def test_plan_uses_the_first_real_video_stream(plan_one: PlanOne) -> None:
    file = make_info(
        make_video(codec="vp9", width=640, height=360, fps=30.0),
        make_video(codec="av1", width=3840, height=2160, fps=120.0),
        container=WEBM,
    )
    assert plan_one(file, MediaRule(("**",), max_fps=30.0)) == []


def test_plan_reports_each_file_on_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = ["m/a.mp4", "m/b.mp4", "m/c.webm", "m/d.mkv", "m/e.avi", "m/f.mov"]
    game = make_game(tmp_path / "game", files)
    probe = FakeProbe(
        {
            "a.mp4": FULL_HD,
            "b.mp4": ToolError("ffprobe cannot read b.mp4 (exit status 1): broken"),
            "c.webm": make_info(make_video(codec="vp9", width=640, height=360, fps=30.0)),
            "d.mkv": make_info(make_video(codec="av1")),
            "e.avi": make_info(make_audio("mp3"), container="avi"),
            "f.mov": make_info(make_video(codec="hevc", width=2560, height=1440, fps=30.0)),
        }
    )
    monkeypatch.setattr(media, "probe_media", probe)
    tools = stand_in_tools(tmp_path / "tools", "ffmpeg", "ffprobe")
    items = list(MediaTransformer(tools).plan(make_context(game, [MediaRule(("m/**",))], tools)))

    outcome = [
        (item.source, "action" if isinstance(item, Action) else item.kind.value) for item in items
    ]
    assert outcome == [
        ("romfs/m/a.mp4", "action"),
        ("romfs/m/b.mp4", "invalid"),
        # c.webm complies: nothing at all.
        ("romfs/m/d.mkv", "unsupported"),
        ("romfs/m/e.avi", "unsupported"),
        ("romfs/m/f.mov", "action"),
    ]
    assert len(probe.calls) == 6


# ----------------------------------------------------------------------------------
# MediaTransformer.transform (ffmpeg is a stand-in: these tests need no tools)
# ----------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, Toolbox]:
    """Return ``(source, destination, tools)``: a 4000-byte "video" and where to write."""
    source = tmp_path / "game dump" / "romfs" / "it's an intro \u6620\u50cf.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"v" * 4000)
    destination = tmp_path / "work" / "staging" / "it's an intro \u6620\u50cf.bea-tmp-1a2b3c4d.mp4"
    destination.parent.mkdir(parents=True)
    return source, destination, stand_in_tools(tmp_path / "tools", "ffmpeg")


def test_transform_runs_the_planned_command(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    fake = install(monkeypatch, FakeRun(stderr=b"", stdout=None, writes=b"o" * 1500))
    config = params(fps=30.0)

    result = MediaTransformer(tools).transform(source, destination, config)

    ffmpeg = str(tools.require("ffmpeg"))
    assert fake.command == build_ffmpeg_command(
        ffmpeg, fs_path(source), fs_path(destination), config
    )
    assert_started_safely(fake)
    assert "timeout" not in fake.options  # a long video may take as long as it needs
    assert destination.read_bytes() == b"o" * 1500
    assert source.read_bytes() == b"v" * 4000
    assert result.notes == []
    reported_command = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", "<src>",
        "-map", "0:V:0", "-map", "0:a?", "-sn", "-dn",
        "-vf", "scale=1280:720:flags=lanczos,fps=30",
        "-c:v", "libx264", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "<dst>",
    ]  # fmt: skip
    assert result.details == {"bytes_in": 4000, "bytes_out": 1500, "command": reported_command}
    assert json.loads(json.dumps(result.details)) == result.details


def test_transform_notes_an_output_larger_than_the_source(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    install(monkeypatch, FakeRun(writes=b"o" * 4001))
    result = MediaTransformer(tools).transform(source, destination, params())
    assert (result.details["bytes_in"], result.details["bytes_out"]) == (4000, 4001)
    (note,) = result.notes
    assert "larger than the original (4000 -> 4001 bytes)" in note
    # The same size is not "larger".
    install(monkeypatch, FakeRun(writes=b"o" * 4000))
    assert MediaTransformer(tools).transform(source, destination, params()).notes == []


def test_transform_reports_a_failed_ffmpeg(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    stderr = (
        b"[libx264 @ 00000277ad9fe900] width not divisible by 2 (321x180)\r\n"
        b"[vost#0:0/libx264 @ 00000277ad9fe3c0] Error while opening encoder\r\n"
        b"[vf#0:0 @ 00000277add50700] Error sending frames to consumers\r\n"
        b"[vf#0:0 @ 00000277add50700] Task finished with error code: -22\r\n"
        b"[vost#0:0/libx264 @ 00000277ad9fe3c0] Could not open encoder before EOF\r\n"
        b"[out#0/mp4 @ 00000277adf4e940] Nothing was written into output file\r\n"
    )
    install(monkeypatch, FakeRun(returncode=234, stderr=stderr))
    with pytest.raises(ToolError) as caught:
        MediaTransformer(tools).transform(source, destination, params())
    # The first line is the cause; the tail shows where it ended.
    assert str(caught.value) == (
        f"ffmpeg failed on {source} (exit status 234): "
        "[libx264] width not divisible by 2 (321x180) | ... | "
        "[vf#0:0] Error sending frames to consumers | "
        "[vf#0:0] Task finished with error code: -22 | "
        "[vost#0:0/libx264] Could not open encoder before EOF | "
        "[out#0/mp4] Nothing was written into output file"
    )
    install(monkeypatch, FakeRun(returncode=1))
    with pytest.raises(ToolError, match=r"exit status 1\): it printed no error message"):
        MediaTransformer(tools).transform(source, destination, params())


@pytest.mark.parametrize("written", [None, b""])
def test_transform_reports_a_missing_or_empty_output(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch, written: bytes | None
) -> None:
    source, destination, tools = workspace
    install(monkeypatch, FakeRun(writes=written, stderr=b"[out#0/mp4 @ 0x7f] nothing to do\n"))
    with pytest.raises(ToolError) as caught:
        MediaTransformer(tools).transform(source, destination, params())
    assert str(caught.value) == (
        f"ffmpeg reported success for {source} but wrote no data to its output: "
        "[out#0/mp4] nothing to do"
    )


def test_transform_reports_a_missing_ffmpeg(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, destination, _tools = workspace
    fake = install(monkeypatch, FakeRun(writes=b"never"))
    empty = stand_in_tools(tmp_path / "no tools")
    with pytest.raises(ToolError) as caught:
        MediaTransformer(empty).transform(source, destination, params())
    assert str(caught.value) == empty.missing_hint("ffmpeg")
    assert fake.calls == []
    assert not destination.exists()


def test_transform_reports_an_ffmpeg_that_cannot_start(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    install(monkeypatch, FakeRun(raises=PermissionError(13, "Permission denied")))
    with pytest.raises(ToolError) as caught:
        MediaTransformer(tools).transform(source, destination, params())
    message = str(caught.value)
    assert message.startswith(f"ffmpeg could not be started ({tools.require('ffmpeg')}): ")
    assert "Permission denied" in message


def test_transform_reports_an_unreadable_source(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    fake = install(monkeypatch, FakeRun(writes=b"never"))
    missing = source.with_name("gone.mp4")
    with pytest.raises(InputError, match="cannot read the video to transcode") as caught:
        MediaTransformer(tools).transform(missing, destination, params())
    assert "gone.mp4" in str(caught.value)
    assert fake.calls == []


def test_transform_rejects_params_that_plan_did_not_produce(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination, tools = workspace
    fake = install(monkeypatch, FakeRun(writes=b"never"))
    with pytest.raises(ValueError, match="media params"):
        MediaTransformer(tools).transform(source, destination, {"width": 1280})
    assert fake.calls == []


def test_transform_finds_ffmpeg_with_the_standard_rules_by_default(
    workspace: tuple[Path, Path, Toolbox], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, destination, tools = workspace
    stand_in = tools.require("ffmpeg")
    # No toolbox given: the default one honours the BEA_FFMPEG override.
    monkeypatch.setenv("BEA_FFMPEG", str(stand_in))
    monkeypatch.delenv("BEA_TOOLS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    fake = install(monkeypatch, FakeRun(writes=b"o" * 10))
    result = MediaTransformer().transform(source, destination, params())
    assert fake.command[0] == str(stand_in)
    assert result.details["command"][0] == str(stand_in)


def test_transform_uses_long_path_safe_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deeper than the 260 characters classic Windows APIs accept.
    deep = tmp_path.joinpath(*["segment_" + "x" * 40] * 6)
    ensure_dir(deep)
    source = deep / "intro.mkv"
    destination = deep / "intro.bea-tmp-1a2b3c4d.mkv"
    Path(fs_path(source)).write_bytes(b"v" * 100)
    assert len(str(source)) > 300
    tools = stand_in_tools(tmp_path / "tools", "ffmpeg")
    fake = install(monkeypatch, FakeRun(writes=b"o" * 40))

    result = MediaTransformer(tools).transform(source, destination, params())

    command = fake.command
    assert command[command.index("-i") + 1] == fs_path(source)
    assert command[-1] == fs_path(destination)
    assert os.path.isabs(command[-1])
    assert (result.details["bytes_in"], result.details["bytes_out"]) == (100, 40)
    assert result.details["command"].count("<src>") == 1
    assert result.details["command"][-1] == "<dst>"


# ----------------------------------------------------------------------------------
# With the real tools (skipped where ffmpeg / ffprobe or an encoder is missing)
# ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_tools() -> Toolbox:
    tools = Toolbox()
    for name in ("ffmpeg", "ffprobe"):
        if tools.find(name) is None:
            pytest.skip(tools.missing_hint(name))
    return tools


@pytest.fixture(scope="module")
def encoders(real_tools: Toolbox) -> frozenset[str]:
    """Return the names of the encoders this ffmpeg was built with."""
    listing = subprocess.run(
        [str(real_tools.require("ffmpeg")), "-hide_banner", "-loglevel", "error", "-encoders"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    names = set()
    for line in listing.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            names.add(fields[1])
    return frozenset(names)


def require_encoders(available: frozenset[str], *needed: str) -> None:
    absent = [name for name in needed if name not in available]
    if absent:
        pytest.skip(f"this ffmpeg has no {', '.join(absent)} encoder")


def synthesize_clip(
    tools: Toolbox,
    path: Path,
    *,
    video_encoder: str,
    audio_encoder: str | None,
    size: str = "640x360",
    rate: int = 60,
) -> None:
    """Write a one-second test pattern (with a sine tone) made by ffmpeg itself."""
    ensure_dir(path.parent)
    command = [
        str(tools.require("ffmpeg")), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration=1",
    ]  # fmt: skip
    if audio_encoder is not None:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
    command += ["-c:v", video_encoder, "-pix_fmt", "yuv420p"]
    if audio_encoder is not None:
        command += ["-c:a", audio_encoder, "-shortest"]
    command.append(fs_path(path))
    made = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if made.returncode != 0 or not os.path.isfile(fs_path(path)):
        pytest.skip(f"this ffmpeg cannot synthesize the sample clip: {stderr_summary(made.stderr)}")


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(fs_path(path)).read_bytes()).hexdigest()


@pytest.mark.ffmpeg
def test_real_downscale_and_frame_rate_cap(
    tmp_path: Path, real_tools: Toolbox, encoders: frozenset[str]
) -> None:
    require_encoders(encoders, "libx264", "aac")
    # Spaces, quotes, shell metacharacters and non-ASCII letters, in folders and names.
    relative = "movies/op & ed's sm\u00f8rbr\u00f8d \u6620\u50cf (v2) [100%];$x.mp4"
    root = tmp_path / "game dump (it's mine)"
    clip = root / "romfs" / "movies" / relative.rsplit("/", 1)[-1]
    synthesize_clip(real_tools, clip, video_encoder="libx264", audio_encoder="aac")
    game = make_game(root, [relative])
    fingerprint = sha256(clip)
    ffprobe = real_tools.require("ffprobe")

    rule = MediaRule(match=("movies/**",), max_width=320, max_height=180, max_fps=30.0)
    transformer = MediaTransformer(real_tools)
    action = only_action(list(transformer.plan(make_context(game, [rule], real_tools))))
    assert action.source == action.output == f"romfs/{relative}"
    assert action.params == {
        "width": 320,
        "height": 180,
        "fps": 30.0,
        "video_encoder": "libx264",
        "crf": 23,
        "video_bitrate": None,
        "audio": {"mode": "copy"},
    }
    assert action.summary == "640x360@60 h264 -> 320x180@30"

    source = game.resolve(action.source)
    destination = tmp_path / "work \u00f8" / "it's 50% done; \u6620\u50cf.bea-tmp-1a2b3c4d.mp4"
    destination.parent.mkdir()
    result = transformer.transform(source, destination, action.params)

    before = probe_media(source, ffprobe)
    after = probe_media(destination, ffprobe)
    (picture,) = after.video
    assert (picture.width, picture.height) == (320, 180)
    assert picture.fps == pytest.approx(30.0, abs=0.5)
    assert picture.codec == "h264" == before.video[0].codec
    assert picture.pix_fmt == "yuv420p"
    assert after.container == before.container == MP4
    assert [stream.codec for stream in after.audio] == ["aac"]
    assert after.audio[0].sample_rate == 48000
    assert after.duration == pytest.approx(1.0, abs=0.25)

    assert result.details["bytes_in"] == clip.stat().st_size == before.size
    assert result.details["bytes_out"] == destination.stat().st_size == after.size
    assert result.details["command"][0] == str(real_tools.require("ffmpeg"))
    assert result.details["command"].count("<src>") == 1
    assert result.details["command"][-1] == "<dst>"
    assert sha256(clip) == fingerprint  # the input is never written to

    # The result complies with the rule that produced it: planning it again is a no-op.
    shutil.copyfile(destination, clip.with_name("done.mp4"))
    again = make_game(root, ["movies/done.mp4"])
    assert list(transformer.plan(make_context(again, [rule], real_tools))) == []


@pytest.mark.ffmpeg
@pytest.mark.parametrize(
    ("name", "video_codec", "video_encoder", "audio_codec", "audio_encoder"),
    [
        ("clip.mov", "h264", "libx264", "aac", "aac"),
        ("clip.m4v", "h264", "libx264", "aac", "aac"),
        ("clip.mkv", "hevc", "libx265", "aac", "aac"),
        ("clip.webm", "vp9", "libvpx-vp9", "opus", "libopus"),
        ("clip.webm", "vp8", "libvpx", "vorbis", "libvorbis"),
        ("clip.avi", "mpeg4", "mpeg4", "mp3", "libmp3lame"),
        ("clip.mp4", "mpeg4", "mpeg4", "aac", "aac"),
        ("clip.ogv", "theora", "libtheora", "vorbis", "libvorbis"),
    ],
)
def test_real_round_trip_keeps_container_and_codecs(
    tmp_path: Path,
    real_tools: Toolbox,
    encoders: frozenset[str],
    name: str,
    video_codec: str,
    video_encoder: str,
    audio_codec: str,
    audio_encoder: str,
) -> None:
    require_encoders(encoders, video_encoder, audio_encoder)
    root = tmp_path / "game"
    clip = root / "romfs" / name
    synthesize_clip(
        real_tools,
        clip,
        video_encoder=video_encoder,
        audio_encoder=audio_encoder,
        size="320x180",
        rate=50,
    )
    game = make_game(root, [name])
    ffprobe = real_tools.require("ffprobe")
    before = probe_media(clip, ffprobe)
    assert (before.video[0].codec, before.audio[0].codec) == (video_codec, audio_codec)

    # Every knob at once: a smaller box, a frame-rate cap and re-encoded audio, with the
    # default quality of the codec family.
    rule = MediaRule(("**",), max_width=160, max_height=90, max_fps=25.0, audio_bitrate="64k")
    transformer = MediaTransformer(real_tools)
    action = only_action(list(transformer.plan(make_context(game, [rule], real_tools))))
    assert action.params["video_encoder"] == VIDEO_ENCODERS[video_codec]
    assert action.params["audio"] == {
        "mode": "encode",
        "encoder": AUDIO_ENCODERS[audio_codec],
        "bitrate": "64k",
    }
    destination = tmp_path / "staging" / f"clip.bea-tmp-1a2b3c4d{clip.suffix}"
    destination.parent.mkdir()
    transformer.transform(clip, destination, action.params)

    after = probe_media(destination, ffprobe)
    (picture,) = after.video
    assert (picture.width, picture.height) == (160, 90)
    assert picture.fps == pytest.approx(25.0, abs=0.5)
    assert picture.codec == video_codec
    assert [stream.codec for stream in after.audio] == [audio_codec]
    assert after.container == before.container

    shutil.copyfile(destination, clip.with_name("done" + clip.suffix))
    again = make_game(root, ["done" + clip.suffix])
    no_sound = MediaRule(("**",), max_width=160, max_height=90, max_fps=25.0)
    assert list(transformer.plan(make_context(again, [no_sound], real_tools))) == []


@pytest.mark.ffmpeg
def test_real_tools_accept_paths_longer_than_260_characters(
    tmp_path: Path, real_tools: Toolbox, encoders: frozenset[str]
) -> None:
    require_encoders(encoders, "libx264")
    folders = "/".join(["segment_" + "x" * 40] * 6)
    relative = f"{folders}/deep \u6620\u50cf.mp4"
    root = tmp_path / "game"
    clip = root.joinpath("romfs", *relative.split("/"))
    assert len(str(clip)) > 300
    synthesize_clip(real_tools, clip, video_encoder="libx264", audio_encoder=None, size="320x180")
    game = make_game(root, [relative])

    transformer = MediaTransformer(real_tools)
    rule = MediaRule(("**",), max_width=160, max_height=90)
    action = only_action(list(transformer.plan(make_context(game, [rule], real_tools))))
    assert action.summary == "320x180@60 h264 -> 160x90@60"
    destination = clip.with_name("deep \u6620\u50cf.bea-tmp-1a2b3c4d.mp4")
    result = transformer.transform(game.resolve(action.source), destination, action.params)

    after = probe_media(destination, real_tools.require("ffprobe"))
    assert (after.video[0].width, after.video[0].height) == (160, 90)
    assert after.audio == ()
    assert result.details["bytes_out"] == os.path.getsize(fs_path(destination)) > 0


@pytest.mark.ffmpeg
def test_real_transparent_video_is_recognized_and_left_alone(
    tmp_path: Path, real_tools: Toolbox, encoders: frozenset[str]
) -> None:
    require_encoders(encoders, "libvpx-vp9")
    root = tmp_path / "game"
    clip = root / "romfs" / "ui" / "overlay.webm"
    clip.parent.mkdir(parents=True)
    command = [
        str(real_tools.require("ffmpeg")), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red@0.5:size=320x180:rate=30:duration=1,format=yuva420p",
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        fs_path(clip),
    ]  # fmt: skip
    made = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if made.returncode != 0 or not clip.is_file():
        pytest.skip(f"this ffmpeg cannot write VP9 with alpha: {stderr_summary(made.stderr)}")

    # ffprobe itself says "yuv420p" for this file; the alpha plane only shows in a tag.
    info = probe_media(clip, real_tools.require("ffprobe"))
    assert (info.video[0].codec, info.video[0].pix_fmt) == ("vp9", "yuva420p")

    game = make_game(root, ["ui/overlay.webm"])
    transformer = MediaTransformer(real_tools)
    shrink = MediaRule(("**",), max_width=160, max_height=90)
    skip = only_skip(list(transformer.plan(make_context(game, [shrink], real_tools))))
    assert skip.kind is SkipKind.UNSUPPORTED
    assert "alpha channel (yuva420p)" in skip.reason
    # Within the limits there is nothing to refuse.
    assert list(transformer.plan(make_context(game, [MediaRule(("**",))], real_tools))) == []


@pytest.mark.ffmpeg
def test_real_tools_report_files_that_are_not_videos(tmp_path: Path, real_tools: Toolbox) -> None:
    game = make_game(tmp_path / "game", ["movies/not a video.mp4"])
    garbage = game.resolve("romfs/movies/not a video.mp4")
    ffprobe = real_tools.require("ffprobe")

    with pytest.raises(ToolError, match="ffprobe cannot read") as caught:
        probe_media(garbage, ffprobe)
    assert str(garbage) in str(caught.value)
    assert "exit status" in str(caught.value)
    with pytest.raises(ToolError, match="ffprobe cannot read"):
        probe_media(tmp_path / "no such file.webm", ffprobe)

    transformer = MediaTransformer(real_tools)
    skip = only_skip(list(transformer.plan(make_context(game, [MediaRule(("**",))], real_tools))))
    assert (skip.source, skip.kind) == ("romfs/movies/not a video.mp4", SkipKind.INVALID)
    assert "ffprobe cannot read" in skip.reason

    destination = tmp_path / "out.bea-tmp-1a2b3c4d.mp4"
    with pytest.raises(ToolError, match="ffmpeg failed on") as failure:
        transformer.transform(garbage, destination, params())
    assert str(garbage) in str(failure.value)
    assert "exit status" in str(failure.value)
