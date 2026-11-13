"""Read-only analysis of a game folder.

``analyze`` answers two questions before anything is modified:

* **What is in this game?** Executable modules and their build IDs, the RomFS inventory
  by file type, textures and their dimensions, videos, configuration files that look
  like they hold graphics settings, containers BEA cannot open yet, and a guess at the
  engine.
* **What would BEA do with it?** The same plan that ``build`` would execute with the
  profile in effect, summarized.

Everything is read from file headers; no pixel is decoded and nothing is written. The
result is a JSON-serializable report (``backport_report.json``).

Honesty rules of the report: facts read from the files are stated as facts; guesses are
labelled (``"confidence": "inferred"``); whatever could not be read or is not supported
is listed, never dropped; and ``limitations`` says what the analysis did not look at.
Strings that come from game files (paths, configuration keys and values) are data, not
instructions: a consumer must never act on their content.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from bea import __version__
from bea.errors import BeaError
from bea.formats import imageinfo
from bea.formats.mediainfo import probe_media
from bea.fsutil import fs_path
from bea.model import GameInput
from bea.pipeline import Plan, make_plan
from bea.profile.schema import Profile, scale_factor
from bea.toolbox import Toolbox
from bea.transformers.base import PlanContext, Skip, SkipKind, Transformer

__all__ = [
    "AUDIO_EXTENSIONS",
    "CONFIG_EXTENSIONS",
    "CONTAINER_KINDS",
    "REPORT_NAME",
    "VIDEO_EXTENSIONS",
    "analyze",
    "classify_setting",
    "guess_engine",
    "scan_config_text",
]

#: Default file name of the report.
REPORT_NAME = "backport_report.json"
REPORT_SCHEMA = 1

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ogv", ".ivf", ".bk2", ".bik", ".usm"}
)
AUDIO_EXTENSIONS = frozenset(
    {
        ".wav",
        ".ogg",
        ".opus",
        ".lopus",
        ".mp3",
        ".aac",
        ".flac",
        ".bwav",
        ".bfwav",
        ".bfstm",
        ".bcstm",
        ".bars",
        ".hca",
        ".adx",
        ".at9",
        ".fsb",
        ".bank",
        ".wem",
        ".bnk",
        ".awb",
        ".acb",
    }
)
CONFIG_EXTENSIONS = frozenset(
    {
        ".ini",
        ".cfg",
        ".conf",
        ".config",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".toml",
        ".txt",
        ".properties",
    }
)
#: Containers whose content this version of BEA does not process, by extension.
CONTAINER_KINDS: Mapping[str, str] = {
    ".bntx": "NX texture container",
    ".bfres": "NintendoWare model/texture resource",
    ".sarc": "SARC archive",
    ".szs": "compressed SARC archive",
    ".pack": "archive",
    ".arc": "archive",
    ".pak": "engine package",
    ".ucas": "Unreal Engine container",
    ".utoc": "Unreal Engine container index",
    ".uasset": "Unreal Engine asset",
    ".uexp": "Unreal Engine asset data",
    ".ubulk": "Unreal Engine bulk data",
    ".umap": "Unreal Engine map",
    ".assets": "Unity serialized assets",
    ".ress": "Unity streamed resources",
    ".resource": "Unity streamed resources",
    ".bundle": "Unity asset bundle",
    ".unity3d": "Unity data archive",
    ".pck": "Godot package",
    ".xnb": "XNA / MonoGame content",
    ".rpa": "Ren'Py archive",
    ".win": "GameMaker data",
    ".zip": "ZIP archive",
}

#: Largest configuration file that is scanned for setting names.
_CONFIG_SCAN_LIMIT = 1024 * 1024
_MAX_HITS_PER_FILE = 40
_MAX_HITS_TOTAL = 2000
_MAX_VALUE_LENGTH = 120
_MAX_LINE_LENGTH = 2000

# ----------------------------------------------------------------- engine heuristics

#: (engine, exact lower-case file names, lower-case extensions, lower-case path fragments)
_ENGINE_SIGNATURES: tuple[tuple[str, frozenset[str], frozenset[str], tuple[str, ...]], ...] = (
    (
        "unity",
        frozenset(
            {
                "globalgamemanagers",
                "globalgamemanagers.assets",
                "data.unity3d",
                "unity default resources",
                "global-metadata.dat",
                "boot.config",
            }
        ),
        frozenset({".unity3d", ".assets", ".ress"}),
        ("/managed/metadata/", "/streamingassets/"),
    ),
    (
        "unreal",
        frozenset(),
        frozenset({".pak", ".ucas", ".utoc", ".uasset", ".umap", ".uexp", ".ubulk"}),
        ("/content/paks/",),
    ),
    ("godot", frozenset(), frozenset({".pck"}), ()),
    ("gamemaker", frozenset({"game.win", "data.win", "game.unx"}), frozenset(), ()),
    ("monogame", frozenset(), frozenset({".xnb"}), ()),
    ("renpy", frozenset(), frozenset({".rpa", ".rpyc"}), ()),
    (
        "rpgmaker",
        frozenset({"rpg_core.js", "rpg_managers.js"}),
        frozenset({".rpgmvp", ".rpgmvo", ".rvdata2"}),
        (),
    ),
    (
        "nintendoware",
        frozenset(),
        frozenset({".bfres", ".bntx", ".byml", ".byaml", ".szs", ".sarc", ".bflyt", ".bflan"}),
        (),
    ),
)


def guess_engine(paths: Iterable[str]) -> dict[str, Any]:
    """Guess which engine or toolchain a RomFS tree comes from, by file names only.

    The result is a heuristic and says so. ``guess`` is ``None`` when nothing matched;
    ``candidates`` lists every engine that left a trace, strongest first, with a few of
    the paths that count as evidence.
    """
    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        lowered = "/" + path.lower()
        name = lowered.rsplit("/", 1)[-1]
        dot = name.rfind(".")
        extension = name[dot:] if dot > 0 else ""
        for engine, names, extensions, fragments in _ENGINE_SIGNATURES:
            if (
                name in names
                or extension in extensions
                or any(fragment in lowered for fragment in fragments)
            ):
                counts[engine] += 1
                if len(evidence[engine]) < 5:
                    evidence[engine].append(path)
    candidates = [
        {"engine": engine, "files": count, "evidence": evidence[engine]}
        for engine, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "guess": candidates[0]["engine"] if candidates else None,
        "confidence": "inferred",
        "method": "file names and extensions only",
        "candidates": candidates,
    }


# ----------------------------------------------------------- configuration heuristics

_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

#: category -> (single tokens, fragments searched in the squeezed lower-case key)
_SETTING_KEYWORDS: Mapping[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "resolution": (
        frozenset({"resolution", "res", "upscale", "upscaling"}),
        (
            "screenpercentage",
            "renderscale",
            "resolutionscale",
            "dynamicres",
            "screenwidth",
            "screenheight",
            "renderwidth",
            "renderheight",
            "displaywidth",
            "displayheight",
            "framebufferwidth",
            "framebufferheight",
            "backbuffer",
            "setres",
        ),
    ),
    "frame_rate": (
        frozenset({"fps", "vsync", "framerate"}),
        (
            "framerate",
            "framelimit",
            "maxfps",
            "targetfps",
            "vsync",
            "syncinterval",
            "refreshrate",
            "targetframe",
        ),
    ),
    "shadows": (frozenset({"shadow", "shadows"}), ("shadowquality", "shadowmap", "shadowres")),
    "level_of_detail": (
        frozenset({"lod", "lods"}),
        ("viewdistance", "drawdistance", "culldistance", "detaildistance", "lodbias"),
    ),
    "textures": (
        frozenset({"anisotropy", "anisotropic", "mipmap", "mipmaps"}),
        ("texturequality", "texturesize", "texturepool", "mipbias", "streamingpool", "poolsize"),
    ),
    "post_processing": (
        frozenset({"msaa", "fxaa", "taa", "smaa", "bloom", "ssao", "antialiasing"}),
        ("antialias", "motionblur", "depthoffield", "ambientocclusion", "postprocess"),
    ),
    "memory": (frozenset({"heap", "memory"}), ("heapsize", "memorybudget", "cachesize")),
}

_DIMENSIONS_VALUE = re.compile(r"(?<!\d)(\d{3,4})\s*[xX\u00d7]\s*(\d{3,4})(?!\d)")
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_+\-.!][\w.\-+\[\] ]{0,80}?)\s*[=:]\s*(\S.*?)\s*$")
# Scalar values only: an object or array value must not swallow the pairs nested in it.
_JSON_PAIR = re.compile(r'"([^"\\]{1,80})"\s*:\s*("(?:[^"\\]|\\.)*"|[-+.\w]+)')
_XML_ELEMENT = re.compile(r"<([A-Za-z_][\w.\-]{0,80})>([^<]{1,200})</\1>")
_XML_ATTRIBUTE = re.compile(r'([A-Za-z_][\w.\-]{0,80})\s*=\s*"([^"]{0,200})"')


def classify_setting(key: str, value: str = "") -> str | None:
    """Return the category a setting name seems to belong to, or ``None``.

    Categories: ``resolution``, ``frame_rate``, ``shadows``, ``level_of_detail``,
    ``textures``, ``post_processing``, ``memory``. The decision uses the words of the key
    (split on punctuation and camelCase) and, for resolutions, a ``WIDTHxHEIGHT`` value.
    """
    tokens = {token.lower() for token in _TOKEN.findall(key)}
    squeezed = re.sub(r"[^a-z0-9]", "", key.lower())
    for category, (words, fragments) in _SETTING_KEYWORDS.items():
        if tokens & words or any(fragment in squeezed for fragment in fragments):
            return category
    match = _DIMENSIONS_VALUE.search(value)
    if match and int(match.group(1)) >= 320 and int(match.group(2)) >= 240:
        return "resolution"
    return None


def scan_config_text(text: str) -> list[dict[str, Any]]:
    """Find lines of a configuration file whose key looks like a graphics setting.

    Understands ``key = value`` / ``key: value`` lines, JSON ``"key": value`` pairs and
    simple XML elements and attributes. Returns ``{"line", "key", "value", "category"}``
    items in file order, without duplicates on the same line.
    """
    hits: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > _MAX_LINE_LENGTH:
            continue
        stripped = line.strip()
        if not stripped or stripped[0] in ";#" or stripped.startswith("//"):
            continue
        pairs: list[tuple[str, str]] = [(key, value) for key, value in _JSON_PAIR.findall(line)]
        pairs += _XML_ELEMENT.findall(line)
        if not pairs and "<" in line:
            pairs += _XML_ATTRIBUTE.findall(line)
        if not pairs:
            assignment = _ASSIGNMENT.match(line)
            if assignment:
                pairs.append((assignment.group(1), assignment.group(2)))
        seen: set[str] = set()
        for key, value in pairs:
            key = key.strip()
            value = value.strip().strip('"')
            category = classify_setting(key, value)
            if category is None or key in seen:
                continue
            seen.add(key)
            hits.append(
                {
                    "line": number,
                    "key": key,
                    "value": value[:_MAX_VALUE_LENGTH],
                    "category": category,
                }
            )
    return hits


def _decode_for_scan(data: bytes) -> str | None:
    """Decode a configuration file leniently; ``None`` when it looks binary."""
    for bom, codec in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            try:
                return data[len(bom) :].decode(codec)
            except UnicodeDecodeError:
                return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ----------------------------------------------------------------------- the report


def analyze(
    game: GameInput,
    profile: Profile,
    *,
    tools: Toolbox,
    transformers: Sequence[Transformer],
    max_listed: int = 200,
) -> dict[str, Any]:
    """Analyze ``game`` and return the report.

    ``max_listed`` bounds the per-file lists (largest textures, videos); counts and
    totals always cover every file.
    """
    target = profile.target
    title = game.title
    limitations = [
        "Only file headers were read: no texture, video or archive content was decoded.",
        "Assets packed inside engine containers (see 'containers') are not inspected.",
        "The engine guess and the configuration hits are name-based heuristics.",
    ]

    plan = make_plan(PlanContext(game=game, profile=profile, tools=tools), transformers)

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "tool": {"name": "bea", "version": __version__},
        "command": "analyze",
        "input": {
            "path": str(game.root),
            "has_exefs": game.exefs_dir is not None,
            "has_romfs": game.romfs_dir is not None,
            "title_id": title.title_id,
            "name": title.name,
            "publisher": title.publisher,
            "display_version": title.display_version,
            "sources": dict(title.sources),
            "warnings": list(game.warnings),
        },
        "profile": dict(profile.describe()),
        "target": {"max_width": target.max_width, "max_height": target.max_height},
        "binaries": _binaries(game, profile),
        "romfs": _inventory(game),
        "engine": guess_engine(entry.path for entry in game.romfs_files),
        "textures": _textures(game, profile, max_listed),
        "media": _media(game, profile, tools, max_listed, limitations),
        "configs": _configs(game),
        "containers": _containers(game),
        "plan": _plan_summary(plan),
        "limitations": limitations,
    }
    return report


def _binaries(game: GameInput, profile: Profile) -> list[dict[str, Any]]:
    result = []
    for module in game.modules:
        entry: dict[str, Any] = {
            "name": module.name,
            "size": module.size,
            "build_id": module.build_id,
            "bss_size": module.bss_size,
            "segments": [
                {
                    "name": segment.name,
                    "memory_offset": segment.memory_offset,
                    "size": segment.size,
                    "file_size": segment.file_size,
                    "compressed": segment.compressed,
                }
                for segment in module.segments
            ],
            "patches": None,
        }
        if profile.patches is not None:
            build = profile.patches.for_module(module.name, module.build_id)
            known = sorted(
                other.build_id for other in profile.patches.builds if other.module == module.name
            )
            entry["patches"] = {
                "build_known": build is not None,
                "label": build.label if build else None,
                "enabled": [patch.name for patch in build.patches if patch.enabled]
                if build
                else [],
                "disabled": [patch.name for patch in build.patches if not patch.enabled]
                if build
                else [],
                "builds_in_profile": known,
            }
        result.append(entry)
    return result


def _inventory(game: GameInput) -> dict[str, Any]:
    by_extension: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total = 0
    for entry in game.romfs_files:
        bucket = by_extension[entry.ext]
        bucket[0] += 1
        bucket[1] += entry.size
        total += entry.size
    extensions = [
        {"extension": extension or "(none)", "files": files, "bytes": size}
        for extension, (files, size) in sorted(
            by_extension.items(), key=lambda item: (-item[1][1], item[0])
        )
    ]
    return {"files": len(game.romfs_files), "bytes": total, "extensions": extensions}


def _textures(game: GameInput, profile: Profile, max_listed: int) -> dict[str, Any]:
    target = profile.target
    readable: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    formats: Counter[str] = Counter()
    files = total_bytes = over_target = 0
    for entry in game.romfs_files:
        if entry.ext not in imageinfo.IMAGE_EXTENSIONS:
            continue
        files += 1
        total_bytes += entry.size
        try:
            info = imageinfo.probe(game.resolve(entry.logical))
        except (BeaError, OSError) as exc:
            unreadable.append({"path": entry.path, "reason": str(exc)})
            continue
        if info is None:
            unreadable.append({"path": entry.path, "reason": "not a recognized image file"})
            continue
        exceeds = scale_factor(info.width, info.height, target.max_width, target.max_height) < 1.0
        over_target += exceeds
        formats[f"{info.container}/{info.pixel_format or '?'}"] += 1
        readable.append(
            {
                "path": entry.path,
                "bytes": entry.size,
                "container": info.container,
                "pixel_format": info.pixel_format,
                "width": info.width,
                "height": info.height,
                "mip_count": info.mip_count,
                "layers": info.layers,
                "over_target": exceeds,
            }
        )
    readable.sort(key=lambda item: (-item["width"] * item["height"], item["path"]))
    return {
        "files": files,
        "bytes": total_bytes,
        "readable": len(readable),
        "over_target": over_target,
        "formats": dict(sorted(formats.items(), key=lambda item: (-item[1], item[0]))),
        "largest": readable[:max_listed],
        "listed": min(len(readable), max_listed),
        "unreadable": unreadable,
    }


def _media(
    game: GameInput, profile: Profile, tools: Toolbox, max_listed: int, limitations: list[str]
) -> dict[str, Any]:
    target = profile.target
    ffprobe = tools.find("ffprobe")
    if ffprobe is None:
        limitations.append(
            "ffprobe was not found, so video properties were not read ("
            + tools.missing_hint("ffprobe")
            + ")."
        )
    videos: list[dict[str, Any]] = []
    video_bytes = 0
    audio: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for entry in game.romfs_files:
        if entry.ext in AUDIO_EXTENSIONS:
            bucket = audio[entry.ext]
            bucket[0] += 1
            bucket[1] += entry.size
            continue
        if entry.ext not in VIDEO_EXTENSIONS:
            continue
        video_bytes += entry.size
        item: dict[str, Any] = {
            "path": entry.path,
            "bytes": entry.size,
            "info": None,
            "over_target": None,
            "error": None,
        }
        if ffprobe is not None:
            try:
                info = probe_media(fs_path(game.resolve(entry.logical)), ffprobe)
            except BeaError as exc:
                item["error"] = str(exc)
            else:
                item["info"] = info.as_dict()
                item["over_target"] = any(
                    stream.width > target.max_width or stream.height > target.max_height
                    for stream in info.video
                )
        videos.append(item)
    videos.sort(key=lambda item: (-item["bytes"], item["path"]))
    return {
        "probed": ffprobe is not None,
        "video_files": len(videos),
        "video_bytes": video_bytes,
        "videos": videos[:max_listed],
        "listed": min(len(videos), max_listed),
        "audio": [
            {"extension": extension, "files": files, "bytes": size}
            for extension, (files, size) in sorted(audio.items(), key=lambda item: -item[1][1])
        ],
    }


def _configs(game: GameInput) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    scanned = skipped_large = skipped_binary = 0
    truncated = False
    for entry in game.romfs_files:
        if entry.ext not in CONFIG_EXTENSIONS:
            continue
        if entry.size > _CONFIG_SCAN_LIMIT:
            skipped_large += 1
            continue
        try:
            with open(fs_path(game.resolve(entry.logical)), "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        text = _decode_for_scan(data)
        if text is None:
            skipped_binary += 1
            continue
        scanned += 1
        for hit in scan_config_text(text)[:_MAX_HITS_PER_FILE]:
            if len(hits) >= _MAX_HITS_TOTAL:
                truncated = True
                break
            hits.append({"path": entry.path, **hit})
    by_category = Counter(hit["category"] for hit in hits)
    return {
        "confidence": "inferred",
        "method": "setting names matched against graphics-related keywords",
        "files_scanned": scanned,
        "files_skipped_too_large": skipped_large,
        "files_skipped_binary": skipped_binary,
        "by_category": dict(sorted(by_category.items())),
        "hits": hits,
        "truncated": truncated,
    }


def _containers(game: GameInput) -> list[dict[str, Any]]:
    found: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for entry in game.romfs_files:
        if entry.ext in CONTAINER_KINDS:
            bucket = found[entry.ext]
            bucket[0] += 1
            bucket[1] += entry.size
    return [
        {
            "extension": extension,
            "kind": CONTAINER_KINDS[extension],
            "files": files,
            "bytes": size,
            "status": "not processed by this version",
        }
        for extension, (files, size) in sorted(found.items(), key=lambda item: -item[1][1])
    ]


def _plan_summary(plan: Plan) -> dict[str, Any]:
    by_transformer = Counter(action.transformer for action in plan.actions)
    skipped = Counter(skip.kind.value for skip in plan.skips)
    reasons: Counter[tuple[str, str]] = Counter()
    for skip in plan.skips:
        if skip.kind is SkipKind.UNSUPPORTED:
            reasons[(skip.transformer, skip.reason)] += 1
    return {
        "actions": len(plan.actions),
        "by_transformer": dict(sorted(by_transformer.items())),
        "skipped": {kind.value: skipped.get(kind.value, 0) for kind in SkipKind},
        "unsupported_reasons": [
            {"transformer": transformer, "reason": reason, "files": count}
            for (transformer, reason), count in reasons.most_common(20)
        ],
        "mismatches": [_skip_dict(skip) for skip in plan.skips if skip.is_error],
        "hint": "run `bea build --dry-run` for the per-file plan",
    }


def _skip_dict(skip: Skip) -> dict[str, str]:
    return {
        "transformer": skip.transformer,
        "source": skip.source,
        "kind": skip.kind.value,
        "reason": skip.reason,
    }
