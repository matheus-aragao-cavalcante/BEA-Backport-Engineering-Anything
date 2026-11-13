"""The ``bea`` command line.

Conventions shared by every command:

* results go to standard output, progress and diagnostics to standard error;
* ``--json`` prints exactly one JSON document on standard output (ASCII-safe);
* exit status: ``0`` success, ``1`` the command ran but found problems (a build with
  failures, an invalid profile), ``2`` it could not run (bad input, bad options),
  ``130`` interrupted.

This module only parses arguments, calls the library and formats text. It holds no
pipeline logic.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from bea import __version__
from bea.analyze import REPORT_NAME as ANALYZE_REPORT_NAME
from bea.analyze import analyze
from bea.errors import BeaError, InputError, ProfileError
from bea.formats.nso import NsoFile
from bea.fsutil import atomic_write_bytes, atomic_write_text
from bea.hexutil import format_hex_bytes, normalize_title_id, parse_hex_bytes, parse_int
from bea.ingest import open_input
from bea.model import GameInput
from bea.packagers.layeredfs import LayeredFSPackager, Layout
from bea.pipeline import (
    REPORT_NAME as BUILD_REPORT_NAME,
)
from bea.pipeline import (
    ActionOutcome,
    ActionStatus,
    BuildOptions,
    BuildResult,
    build,
)
from bea.profile.loader import (
    PROFILE_FILENAME,
    find_profile_dir,
    list_profile_dirs,
    load_profile,
    validate_profile_dir,
)
from bea.profile.schema import (
    DEFAULT_TARGET_HEIGHT,
    DEFAULT_TARGET_WIDTH,
    PatchSet,
    Profile,
    Target,
    generic_profile,
)
from bea.scaffold import write_profile_skeleton
from bea.synth import synthesize
from bea.toolbox import KNOWN_TOOLS, Toolbox
from bea.transformers import default_transformers

__all__ = ["main"]

_ENV_PROFILES_DIR = "BEA_PROFILES_DIR"
_PROFILES_DIRNAME = "profiles"
_EXIT_OK = 0
_EXIT_PROBLEMS = 1
_EXIT_ERROR = 2
_EXIT_INTERRUPTED = 130


# ------------------------------------------------------------------------- helpers


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _print_json(document: Any) -> None:
    # ASCII output survives any console encoding; files are written as UTF-8 elsewhere.
    print(json.dumps(document, indent=2, ensure_ascii=True, sort_keys=False))


def _size(count: int | float) -> str:
    """Format a byte count for people (binary units)."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _short_id(build_id: str) -> str:
    return build_id if len(build_id) <= 16 else f"{build_id[:8]}…{build_id[-4:]}"


def _default_profiles_dir() -> Path | None:
    """Return where profiles live when the user did not say: env, ``./profiles``, checkout."""
    configured = os.environ.get(_ENV_PROFILES_DIR)
    if configured:
        return Path(configured)
    local = Path.cwd() / _PROFILES_DIRNAME
    if local.is_dir():
        return local
    # Running from a source checkout: the data directory sits next to the package.
    beside_package = Path(__file__).resolve().parent.parent / _PROFILES_DIRNAME
    if beside_package.is_dir():
        return beside_package
    return None


def _profiles_dir(args: argparse.Namespace) -> Path | None:
    explicit = getattr(args, "profiles_dir", None)
    return Path(explicit) if explicit else _default_profiles_dir()


def _toolbox(args: argparse.Namespace) -> Toolbox:
    return Toolbox(getattr(args, "tools_dir", None))


def _open_game(args: argparse.Namespace) -> GameInput:
    return open_input(
        Path(args.input),
        title_id=args.title_id,
        exefs_dir=Path(args.exefs) if args.exefs else None,
        romfs_dir=Path(args.romfs) if args.romfs else None,
    )


def _resolve_profile(args: argparse.Namespace, game: GameInput) -> tuple[GameInput, Profile]:
    """Pick the profile for ``game`` according to the command-line options.

    Returns the game too, because an explicitly given profile can supply the title ID of
    an input that does not reveal it (a RomFS-only folder).
    """
    max_width: int | None = args.max_width
    max_height: int | None = args.max_height
    title_id = game.title.title_id

    profile: Profile
    if args.no_profile:
        profile = generic_profile()
    elif args.profile:
        location = Path(args.profile)
        directory = location.parent if location.name == PROFILE_FILENAME else location
        profile = load_profile(directory)
        if title_id is None and profile.title_id is not None:
            title = dataclasses.replace(
                game.title,
                title_id=profile.title_id,
                sources={**game.title.sources, "title_id": "profile"},
            )
            game = dataclasses.replace(game, title=title)
        elif profile.title_id is not None and profile.title_id != title_id:
            raise ProfileError(
                f"the profile in {directory} is for title {profile.title_id}, "
                f"but this game is {title_id}"
            )
    else:
        directory_found = None
        profiles_dir = _profiles_dir(args)
        if title_id is not None and profiles_dir is not None:
            directory_found = find_profile_dir(profiles_dir, title_id)
        profile = load_profile(directory_found) if directory_found else generic_profile()

    if max_width is not None or max_height is not None:
        profile = dataclasses.replace(
            profile,
            target=Target(
                max_width=max_width if max_width is not None else profile.target.max_width,
                max_height=max_height if max_height is not None else profile.target.max_height,
            ),
        )
    profile = _apply_patch_switches(
        profile,
        getattr(args, "enable_patch", None) or [],
        getattr(args, "disable_patch", None) or [],
    )
    return game, profile


def _apply_patch_switches(
    profile: Profile, enable: Sequence[str], disable: Sequence[str]
) -> Profile:
    """Return ``profile`` with the named patches switched on or off."""
    if not enable and not disable:
        return profile
    patches = profile.patches
    known = (
        {patch.name for build_entry in patches.builds for patch in build_entry.patches}
        if patches
        else set()
    )
    unknown = sorted((set(enable) | set(disable)) - known)
    if unknown:
        listing = ", ".join(sorted(known)) or "none"
        raise ProfileError(
            f"unknown patch name(s): {', '.join(unknown)} (this profile declares: {listing})"
        )
    both = sorted(set(enable) & set(disable))
    if both:
        raise ProfileError(f"patch(es) both enabled and disabled: {', '.join(both)}")
    assert patches is not None
    builds = tuple(
        dataclasses.replace(
            build_entry,
            patches=tuple(
                dataclasses.replace(
                    patch,
                    enabled=True
                    if patch.name in enable
                    else False
                    if patch.name in disable
                    else patch.enabled,
                )
                for patch in build_entry.patches
            ),
        )
        for build_entry in patches.builds
    )
    return dataclasses.replace(profile, patches=PatchSet(title_id=patches.title_id, builds=builds))


def _describe_profile(profile: Profile) -> str:
    if profile.is_generic:
        return "none (generic texture downsizing only)"
    name = f' "{profile.name}"' if profile.name else ""
    where = f" in {profile.source_dir}" if profile.source_dir else ""
    return f"{profile.title_id}{name}{where}"


def _print_warnings(game: GameInput) -> None:
    for warning in game.warnings:
        _err(f"warning: {warning}")


# ------------------------------------------------------------------------- inspect


def _inspect_document(game: GameInput, profile: Profile) -> dict[str, Any]:
    title = game.title
    romfs_bytes = sum(entry.size for entry in game.romfs_files)
    modules = []
    for module in game.modules:
        build_entry = (
            profile.patches.for_module(module.name, module.build_id) if profile.patches else None
        )
        modules.append(
            {
                "name": module.name,
                "size": module.size,
                "build_id": module.build_id,
                "profile_build": None
                if build_entry is None
                else {
                    "label": build_entry.label,
                    "patches_enabled": [p.name for p in build_entry.patches if p.enabled],
                    "patches_disabled": [p.name for p in build_entry.patches if not p.enabled],
                },
            }
        )
    return {
        "tool": {"name": "bea", "version": __version__},
        "command": "inspect",
        "input": {
            "path": str(game.root),
            "exefs": str(game.exefs_dir) if game.exefs_dir else None,
            "romfs": str(game.romfs_dir) if game.romfs_dir else None,
            "nacp": str(game.nacp_path) if game.nacp_path else None,
            "warnings": list(game.warnings),
        },
        "title": {
            "title_id": title.title_id,
            "name": title.name,
            "publisher": title.publisher,
            "display_version": title.display_version,
            "sources": dict(title.sources),
        },
        "modules": modules,
        "exefs_files": list(game.exefs_files),
        "romfs": {"files": len(game.romfs_files), "bytes": romfs_bytes},
        "profile": dict(profile.describe()),
    }


def _cmd_inspect(args: argparse.Namespace) -> int:
    game = _open_game(args)
    game, profile = _resolve_profile(args, game)
    document = _inspect_document(game, profile)
    if args.json:
        _print_json(document)
        return _EXIT_OK
    _print_warnings(game)
    title = game.title
    source = title.sources.get("title_id")
    print(f"Title ID   {title.title_id or 'unknown'}" + (f"  (from {source})" if source else ""))
    if title.name:
        version = f"  v{title.display_version}" if title.display_version else ""
        print(f"Name       {title.name}{version}")
    if title.publisher:
        print(f"Publisher  {title.publisher}")
    print(f"Input      {game.root}")
    print(f"ExeFS      {game.exefs_dir or 'not found'}")
    romfs = document["romfs"]
    print(
        f"RomFS      {game.romfs_dir or 'not found'}"
        + (f"  ({romfs['files']} files, {_size(romfs['bytes'])})" if game.romfs_dir else "")
    )
    print(f"Profile    {_describe_profile(profile)}")
    if game.modules:
        print("\nModules")
        for module, described in zip(game.modules, document["modules"], strict=True):
            entry = described["profile_build"]
            if profile.patches is None:
                note = ""
            elif entry is None:
                note = "  no patches for this build in the profile"
            else:
                label = f' "{entry["label"]}"' if entry["label"] else ""
                note = f"  profile build{label}: {len(entry['patches_enabled'])} patch(es) enabled"
            print(f"  {module.name:<9} {_size(module.size):>10}  build {module.build_id}{note}")
    return _EXIT_OK


# ------------------------------------------------------------------------- analyze


def _cmd_analyze(args: argparse.Namespace) -> int:
    game = _open_game(args)
    game, profile = _resolve_profile(args, game)
    tools = _toolbox(args)
    report = analyze(game, profile, tools=tools, transformers=default_transformers(tools))
    if args.json and not args.output:
        _print_json(report)
        return _EXIT_OK
    output = Path(args.output) if args.output else Path(ANALYZE_REPORT_NAME)
    atomic_write_text(output, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.json:
        _print_json(report)
        return _EXIT_OK

    _print_warnings(game)
    title = game.title
    print(f"Title      {title.title_id or 'unknown'}  {title.name or ''}".rstrip())
    print(f"Profile    {_describe_profile(profile)}")
    print(f"Target     {profile.target.max_width}x{profile.target.max_height}")
    romfs = report["romfs"]
    print(f"\nRomFS      {romfs['files']} files, {_size(romfs['bytes'])}")
    for row in romfs["extensions"][:8]:
        print(f"  {row['extension']:<10} {row['files']:>7} files  {_size(row['bytes']):>10}")
    engine = report["engine"]
    if engine["guess"]:
        print(f"\nEngine     {engine['guess']} (a guess from file names)")
    textures = report["textures"]
    print(
        f"\nTextures   {textures['files']} files, {_size(textures['bytes'])}; "
        f"{textures['over_target']} larger than the target"
        + (f"; {len(textures['unreadable'])} unreadable" if textures["unreadable"] else "")
    )
    for item in textures["largest"][:5]:
        print(
            f"  {item['width']}x{item['height']:<6} {item['pixel_format'] or '?':<18} {item['path']}"
        )
    media = report["media"]
    if media["video_files"]:
        probed = "" if media["probed"] else " (not probed: ffprobe missing)"
        print(f"\nVideos     {media['video_files']} files, {_size(media['video_bytes'])}{probed}")
    configs = report["configs"]
    if configs["hits"]:
        categories = ", ".join(f"{name} {count}" for name, count in configs["by_category"].items())
        print(f"\nSettings   {len(configs['hits'])} graphics-looking keys ({categories})")
    if report["containers"]:
        print("\nContainers not processed by this version")
        for row in report["containers"][:8]:
            print(
                f"  {row['extension']:<10} {row['files']:>7} files  {_size(row['bytes']):>10}  {row['kind']}"
            )
    plan = report["plan"]
    by_transformer = ", ".join(f"{name} {count}" for name, count in plan["by_transformer"].items())
    print(
        f"\nPlan       {plan['actions']} action(s)"
        + (f": {by_transformer}" if by_transformer else "")
    )
    skipped = {kind: count for kind, count in plan["skipped"].items() if count}
    if skipped:
        print("  skipped  " + ", ".join(f"{count} {kind}" for kind, count in skipped.items()))
    for mismatch in plan["mismatches"]:
        print(f"  MISMATCH {mismatch['source']}: {mismatch['reason']}")
    print(f"\nReport written to {output}")
    return _EXIT_PROBLEMS if plan["mismatches"] else _EXIT_OK


# --------------------------------------------------------------------------- build


def _progress_printer(verbose: bool) -> Callable[[ActionOutcome, int, int], None]:
    interactive = sys.stderr.isatty()

    def report(outcome: ActionOutcome, done: int, total: int) -> None:
        if outcome.status is ActionStatus.FAILED:
            _err(f"failed: {outcome.action.source}: {outcome.error}")
        elif verbose:
            _err(
                f"[{done}/{total}] {outcome.status.value:<6} {outcome.action.source}  "
                f"{outcome.action.summary}"
            )
        elif interactive:
            end = "\n" if done == total else "\r"
            print(f"  {done}/{total}", end=end, file=sys.stderr, flush=True)

    return report


def _print_build_summary(result: BuildResult, layout: Layout) -> None:
    options = result.options
    counts = result.status_counts()
    skipped = result.skip_counts()
    before, after = result.asset_bytes()
    heading = "Plan for" if options.dry_run else "Built"
    print(f"{heading} {result.game.title.title_id}  ->  {options.out_dir}  ({layout.value} layout)")
    print(f"Profile    {_describe_profile(result.profile)}")

    by_transformer: dict[str, dict[str, int]] = {}
    for outcome in result.outcomes:
        bucket = by_transformer.setdefault(outcome.action.transformer, {})
        bucket[outcome.status.value] = bucket.get(outcome.status.value, 0) + 1
    for name, statuses in sorted(by_transformer.items()):
        described = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
        print(f"  {name:<9} {described}")
    if not result.outcomes:
        print("  nothing to do: no file needs a change")
    if after or before:
        print(f"  assets    {_size(before)} -> {_size(after)}")
    notable = {kind: count for kind, count in skipped.items() if count}
    if notable:
        print("  skipped   " + ", ".join(f"{count} {kind}" for kind, count in notable.items()))

    if options.dry_run:
        for outcome in result.outcomes:
            print(
                f"    {outcome.action.transformer:<8} {outcome.action.source}  {outcome.action.summary}"
            )
    for skip in result.plan.skips:
        if skip.is_error:
            print(f"  MISMATCH  {skip.source}: {skip.reason}")
    for outcome in result.outcomes:
        for note in outcome.notes:
            print(f"  note      {outcome.action.source}: {note}")
    if result.package is not None:
        for warning in result.package.warnings:
            print(f"  warning   {warning}")
    if counts[ActionStatus.FAILED.value]:
        print(f"  FAILED    {counts[ActionStatus.FAILED.value]} action(s); see the messages above")

    if options.dry_run:
        print("Dry run: nothing was written.")
    elif result.ok:
        if layout is Layout.ATMOSPHERE:
            print(f"Next: copy {options.out_dir / 'atmosphere'} to the root of your SD card.")
        else:
            print(
                f"Next: copy the folder inside {options.out_dir} into your mod loader's folder for this title."
            )
        print("The output is derived from your copy of the game: keep it for yourself.")
    else:
        print(f"The build is incomplete; details are in {options.out_dir / BUILD_REPORT_NAME}.")


def _cmd_build(args: argparse.Namespace) -> int:
    game = _open_game(args)
    game, profile = _resolve_profile(args, game)
    title_id = game.title.title_id
    if title_id is None:
        raise InputError(
            "the title ID of this game could not be determined "
            "(no main.npdm, no control.nacp); pass it with --title-id"
        )
    if args.jobs < 1:
        raise InputError("--jobs must be at least 1")
    tools = _toolbox(args)
    layout = Layout(args.layout)
    options = BuildOptions(
        out_dir=Path(args.out) / title_id,
        work_dir=Path(args.work) / title_id,
        dry_run=args.dry_run,
        force=args.force,
        jobs=args.jobs,
    )
    if not args.json:
        _print_warnings(game)
    result = build(
        game,
        profile,
        transformers=default_transformers(tools),
        packager=LayeredFSPackager(layout),
        tools=tools,
        options=options,
        on_outcome=None if args.json or args.quiet else _progress_printer(args.verbose),
    )
    if args.json:
        _print_json(result.to_report())
    else:
        _print_build_summary(result, layout)
    return _EXIT_OK if result.ok else _EXIT_PROBLEMS


# ------------------------------------------------------------------------- profile


def _cmd_profile_validate(args: argparse.Namespace) -> int:
    directories: list[Path]
    if args.paths:
        directories = [
            path.parent if path.name == PROFILE_FILENAME else path
            for path in (Path(text) for text in args.paths)
        ]
    else:
        profiles_dir = _profiles_dir(args)
        if profiles_dir is None:
            raise InputError(
                "no profile directory found; pass paths, --profiles-dir or set BEA_PROFILES_DIR"
            )
        directories = list_profile_dirs(profiles_dir)
        template = profiles_dir / "_template"
        if (template / PROFILE_FILENAME).is_file():
            directories.append(template)
    results = {str(directory): validate_profile_dir(directory) for directory in directories}
    invalid = {name: problems for name, problems in results.items() if problems}
    if args.json:
        _print_json({"command": "profile validate", "profiles": results, "ok": not invalid})
    else:
        for name, problems in results.items():
            print(f"{'INVALID' if problems else 'ok':<8} {name}")
            for problem in problems:
                print(f"         {problem}")
        print(f"{len(results) - len(invalid)} valid, {len(invalid)} invalid")
    return _EXIT_PROBLEMS if invalid else _EXIT_OK


def _cmd_profile_init(args: argparse.Namespace) -> int:
    game = _open_game(args)
    profiles_dir = _profiles_dir(args) or Path.cwd() / _PROFILES_DIRNAME
    directory = write_profile_skeleton(game, profiles_dir, force=args.force)
    print(f"Created {directory}")
    print(
        "Edit profile.yaml / patches.yaml, then run `bea profile validate` and `bea build --dry-run`."
    )
    return _EXIT_OK


# ----------------------------------------------------------------------------- nso


def _nso_document(path: Path, nso: NsoFile) -> dict[str, Any]:
    header = nso.header
    return {
        "path": str(path),
        "build_id": header.build_id,
        "build_id_full": header.build_id_full,
        "flags": header.flags,
        "bss_size": header.bss_size,
        "image_size": header.image_size,
        "segments": [
            {
                "name": segment.name,
                "memory_offset": segment.memory_offset,
                "size": segment.size,
                "stored_size": segment.stored_size,
                "compressed": segment.compressed,
                "hash_ok": nso.hash_ok[segment.name],
            }
            for segment in header.segments
        ],
        "warnings": list(header.warnings()),
    }


def _cmd_nso_info(args: argparse.Namespace) -> int:
    path = Path(args.file)
    nso = NsoFile.read(path, verify_hashes=False)
    document = _nso_document(path, nso)
    if args.json:
        _print_json(document)
        return _EXIT_OK
    print(f"Build ID   {document['build_id']}")
    print(f"Image      {_size(document['image_size'])} (+ {_size(document['bss_size'])} bss)")
    for segment in document["segments"]:
        stored = "compressed" if segment["compressed"] else "raw"
        check = "hash ok" if segment["hash_ok"] else "HASH MISMATCH"
        print(
            f"  {segment['name']:<7} memory 0x{segment['memory_offset']:08X}  "
            f"size 0x{segment['size']:08X}  {stored}, {check}"
        )
    for warning in document["warnings"]:
        print(f"  warning: {warning}")
    return _EXIT_OK


def _cmd_nso_image(args: argparse.Namespace) -> int:
    nso = NsoFile.read(Path(args.file), verify_hashes=False)
    image = nso.image()
    atomic_write_bytes(Path(args.output), image)
    print(
        f"Wrote the memory image of build {nso.header.build_id} to {args.output} ({_size(len(image))})."
    )
    print("File offset equals module memory offset. The file is game code: keep it local.")
    return _EXIT_OK


def _cmd_nso_hunk(args: argparse.Namespace) -> int:
    """Print a ready-to-paste hunk (with the hash of its verification window)."""
    nso = NsoFile.read(Path(args.file), verify_hashes=False)
    base = parse_int(args.image_base) if args.image_base else 0
    position = parse_int(args.offset)
    offset = position - base if args.image_base else position
    patch = parse_hex_bytes(args.patch)
    if offset < 0:
        raise InputError("the address is below the image base")
    segment = nso.find_segment(offset, len(patch))
    if segment is None:
        raise InputError(
            f"0x{offset:X} (+{len(patch)} bytes) is not inside one segment of this module"
        )
    # A window of at least 16 bytes that contains the patched range and stays in the segment.
    length = max(args.window, len(patch))
    start = min(offset - (offset % 4), segment.memory_end - length)
    start = max(segment.memory_offset, min(start, offset))
    length = min(length, segment.memory_end - start)
    window = nso.read_memory(start, length)
    current = nso.read_memory(offset, len(patch))
    print(f"# module build {nso.header.build_id}, {segment.name} segment")
    print(f"- offset: 0x{offset:X}")
    print(f'  patch_bytes: "{format_hex_bytes(patch)}"')
    print(f'  expected_sha256: "{hashlib.sha256(window).hexdigest()}"')
    print(f"  window_offset: 0x{start:X}")
    print(f"  window_length: {length}")
    _err(f"bytes there now (do not commit them): {format_hex_bytes(current)}")
    return _EXIT_OK


# ------------------------------------------------------------------- synth / doctor


def _cmd_synth(args: argparse.Namespace) -> int:
    result = synthesize(
        Path(args.directory),
        profiles_dir=Path(args.profiles_dir) if args.profiles_dir else None,
        seed=args.seed,
        with_media=args.with_media,
        tools=_toolbox(args),
    )
    print(
        f"Synthetic game written to {result.root} (title {result.title_id}, {result.files} files)."
    )
    if result.profile_dir is not None:
        print(f"Matching profile written to {result.profile_dir}.")
        print(f"Try:  bea build {result.root} --profiles-dir {result.profile_dir.parent} --dry-run")
    for note in result.notes:
        print(f"note: {note}")
    return _EXIT_OK


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _cmd_doctor(args: argparse.Namespace) -> int:
    tools = _toolbox(args)
    profiles_dir = _profiles_dir(args)
    packages = {name: _package_version(name) for name in ("pillow", "pyyaml", "lz4")}
    described = tools.describe()
    document = {
        "bea": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "packages": packages,
        "tools": [
            {
                "name": info.name,
                "available": info.available,
                "path": str(info.path) if info.path else None,
                "origin": info.origin,
                "version": info.version,
                "enables": KNOWN_TOOLS.get(info.name),
            }
            for info in described
        ],
        "profiles_dir": str(profiles_dir) if profiles_dir else None,
        "profiles": len(list_profile_dirs(profiles_dir)) if profiles_dir else 0,
    }
    if args.json:
        _print_json(document)
        return _EXIT_OK
    print(f"bea        {__version__}")
    print(f"python     {document['python']} ({sys.platform})")
    for name, version in packages.items():
        print(f"{name:<10} {version or 'NOT INSTALLED'}")
    print("\nOptional external tools")
    for info in described:
        if info.available:
            print(f"  ok   {info.name:<8} {info.path}  [{info.version or 'version unknown'}]")
        else:
            print(f"  --   {info.name:<8} not found: needed for {KNOWN_TOOLS.get(info.name)}")
            print(f"       {tools.missing_hint(info.name)}")
    print(
        f"\nprofiles   {profiles_dir or 'no directory found'}"
        + (f" ({document['profiles']} game profile(s))" if profiles_dir else "")
    )
    return _EXIT_OK


# -------------------------------------------------------------------------- parser


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _title_id(text: str) -> str:
    try:
        return normalize_title_id(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_input_options(parser: argparse.ArgumentParser, *, profile: bool = True) -> None:
    parser.add_argument("input", help="extracted game folder (contains ExeFS/ and/or RomFS/)")
    group = parser.add_argument_group("input")
    group.add_argument(
        "--title-id",
        type=_title_id,
        metavar="ID",
        help="title ID, when the folder does not reveal it (16 hex digits)",
    )
    group.add_argument("--exefs", metavar="DIR", help="use this folder as the ExeFS")
    group.add_argument("--romfs", metavar="DIR", help="use this folder as the RomFS")
    if not profile:
        return
    chosen = parser.add_argument_group("profile")
    chosen.add_argument(
        "--profiles-dir",
        metavar="DIR",
        help="directory holding <TitleID>/profile.yaml folders (default: $BEA_PROFILES_DIR, then ./profiles)",
    )
    chosen.add_argument(
        "--profile", metavar="PATH", help="use this profile directory instead of looking one up"
    )
    chosen.add_argument(
        "--no-profile", action="store_true", help="ignore profiles: generic texture downsizing only"
    )
    chosen.add_argument(
        "--max-width",
        type=_positive_int,
        metavar="N",
        help=f"override the target width (default {DEFAULT_TARGET_WIDTH})",
    )
    chosen.add_argument(
        "--max-height",
        type=_positive_int,
        metavar="N",
        help=f"override the target height (default {DEFAULT_TARGET_HEIGHT})",
    )
    chosen.add_argument(
        "--tools-dir",
        metavar="DIR",
        help="where external tools are looked up first (default: $BEA_TOOLS_DIR, then ./tools)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bea",
        description="BEA (Backport Engineering Anything): adapt the assets, settings and "
        "executables of a game folder you extracted yourself, and package the result as a "
        "LayeredFS mod. BEA never downloads, decrypts or dumps games and needs no keys.",
    )
    parser.add_argument("--version", action="version", version=f"bea {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    inspect = commands.add_parser(
        "inspect", help="identify a game folder: title, modules and build IDs, profile"
    )
    _add_input_options(inspect)
    inspect.add_argument("--json", action="store_true", help="print JSON")
    inspect.set_defaults(handler=_cmd_inspect)

    analyze_parser = commands.add_parser(
        "analyze", help="read-only report: what is in the game and what BEA would do"
    )
    _add_input_options(analyze_parser)
    analyze_parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help=f"where to write the report (default ./{ANALYZE_REPORT_NAME})",
    )
    analyze_parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON (and write no file unless -o is given)",
    )
    analyze_parser.set_defaults(handler=_cmd_analyze)

    build_parser = commands.add_parser(
        "build", help="apply the profile and write a LayeredFS mod folder"
    )
    _add_input_options(build_parser)
    output = build_parser.add_argument_group("output")
    output.add_argument(
        "--out",
        default="out",
        metavar="DIR",
        help="output root; the mod goes to <DIR>/<TitleID> (default: out)",
    )
    output.add_argument(
        "--work",
        default="work",
        metavar="DIR",
        help="work root for staged files and cache state (default: work)",
    )
    output.add_argument(
        "--layout",
        choices=[layout.value for layout in Layout],
        default=Layout.ATMOSPHERE.value,
        help="folder layout: atmosphere (SD card) or flat (generic mod folder)",
    )
    run = build_parser.add_argument_group("run")
    run.add_argument("--dry-run", action="store_true", help="show the plan; write nothing")
    run.add_argument("--force", action="store_true", help="ignore the cache and redo every action")
    run.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(8, os.cpu_count() or 1),
        metavar="N",
        help="actions run in parallel (default: CPU count, at most 8)",
    )
    run.add_argument(
        "--enable-patch",
        action="append",
        metavar="NAME",
        help="switch on a patch that the profile disables (repeatable)",
    )
    run.add_argument(
        "--disable-patch",
        action="append",
        metavar="NAME",
        help="switch off a patch that the profile enables (repeatable)",
    )
    run.add_argument("-v", "--verbose", action="store_true", help="print every action")
    run.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    run.add_argument("--json", action="store_true", help="print the build report as JSON")
    build_parser.set_defaults(handler=_cmd_build)

    profile = commands.add_parser("profile", help="validate profiles or start a new one")
    profile_commands = profile.add_subparsers(
        dest="profile_command", metavar="<action>", required=True
    )
    validate = profile_commands.add_parser(
        "validate", help="check profile.yaml / patches.yaml files"
    )
    validate.add_argument(
        "paths",
        nargs="*",
        help="profile directories (default: every profile in the profile directory)",
    )
    validate.add_argument(
        "--profiles-dir", metavar="DIR", help="profile directory to scan when no path is given"
    )
    validate.add_argument("--json", action="store_true", help="print JSON")
    validate.set_defaults(handler=_cmd_profile_validate)
    init = profile_commands.add_parser("init", help="create a profile skeleton for a game folder")
    _add_input_options(init, profile=False)
    init.add_argument(
        "--profiles-dir",
        metavar="DIR",
        help="where to create <TitleID>/ (default: $BEA_PROFILES_DIR, then ./profiles)",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing skeleton")
    init.set_defaults(handler=_cmd_profile_init)

    nso = commands.add_parser("nso", help="look inside one executable module (for writing patches)")
    nso_commands = nso.add_subparsers(dest="nso_command", metavar="<action>", required=True)
    info = nso_commands.add_parser("info", help="build ID, segments, hashes")
    info.add_argument("file", help="an ExeFS module (main, sdk, ...)")
    info.add_argument("--json", action="store_true", help="print JSON")
    info.set_defaults(handler=_cmd_nso_info)
    image = nso_commands.add_parser(
        "image",
        help="write the decompressed memory image (file offset = memory offset) for a disassembler",
    )
    image.add_argument("file", help="an ExeFS module")
    image.add_argument(
        "-o",
        "--output",
        required=True,
        metavar="FILE",
        help="where to write the image (keep it out of any repository)",
    )
    image.set_defaults(handler=_cmd_nso_image)
    hunk = nso_commands.add_parser(
        "hunk", help="print a patches.yaml hunk with the hash of its verification window"
    )
    hunk.add_argument("file", help="an ExeFS module")
    hunk.add_argument(
        "--offset",
        required=True,
        metavar="N",
        help="module memory offset of the patch (or an address when --image-base is given)",
    )
    hunk.add_argument(
        "--patch", required=True, metavar="HEX", help='replacement bytes, e.g. "1F 20 03 D5"'
    )
    hunk.add_argument(
        "--image-base",
        metavar="N",
        help="base address of your disassembler, subtracted from --offset",
    )
    hunk.add_argument(
        "--window",
        type=_positive_int,
        default=32,
        metavar="N",
        help="length of the verification window in bytes (default 32)",
    )
    hunk.set_defaults(handler=_cmd_nso_hunk)

    synth = commands.add_parser(
        "synth", help="generate a synthetic, copyright-free sample game folder to try BEA on"
    )
    synth.add_argument("directory", help="where to create the sample game folder")
    synth.add_argument(
        "--profiles-dir", metavar="DIR", help="also write a matching profile under this directory"
    )
    synth.add_argument("--seed", type=int, default=1, help="seed of the generator (default 1)")
    synth.add_argument(
        "--with-media", action="store_true", help="include a short generated video (needs ffmpeg)"
    )
    synth.add_argument(
        "--tools-dir", metavar="DIR", help="where external tools are looked up first"
    )
    synth.set_defaults(handler=_cmd_synth)

    doctor = commands.add_parser(
        "doctor", help="show versions, optional tools and the profile directory"
    )
    doctor.add_argument("--profiles-dir", metavar="DIR", help="profile directory to report on")
    doctor.add_argument(
        "--tools-dir", metavar="DIR", help="where external tools are looked up first"
    )
    doctor.add_argument("--json", action="store_true", help="print JSON")
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def _safe_streams() -> None:
    """Make printing robust on consoles whose encoding lacks a character."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return the process exit status."""
    _safe_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except BeaError as exc:
        if getattr(args, "json", False):
            _print_json({"error": str(exc), "kind": type(exc).__name__})
        _err(f"error: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _err("interrupted")
        return _EXIT_INTERRUPTED


def _run() -> NoReturn:
    sys.exit(main())


# Kept importable for tests that want to check argument parsing without running anything.
def parse_arguments(argv: Sequence[str]) -> Mapping[str, Any]:
    """Parse ``argv`` and return the options as a plain mapping (no command is run)."""
    return vars(_build_parser().parse_args(argv))


if __name__ == "__main__":
    _run()
