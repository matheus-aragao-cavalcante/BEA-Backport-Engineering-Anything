"""Starting a profile for a game: ``bea profile init``.

Writes a commented skeleton (``profile.yaml``, ``patches.yaml``, ``NOTES.md``) for the
game folder at hand, with the identifiers BEA can read from it already filled in: the
title ID, the display name when a ``control.nacp`` is present, and the build ID of the
main executable. Everything else is left for the author; the skeleton is valid as it
stands and does nothing beyond the generic texture pass.

The skeleton contains identifiers only, never bytes of the game.
"""

from __future__ import annotations

import json
from pathlib import Path

from bea.errors import InputError, OutputError
from bea.fsutil import atomic_write_text, ensure_dir
from bea.model import GameInput
from bea.profile.schema import DEFAULT_TARGET_HEIGHT, DEFAULT_TARGET_WIDTH, SCHEMA_VERSION

__all__ = [
    "NOTES_FILENAME",
    "render_notes",
    "render_patches_yaml",
    "render_profile_yaml",
    "write_profile_skeleton",
]

NOTES_FILENAME = "NOTES.md"
_PROFILE_FILENAME = "profile.yaml"
_PATCHES_FILENAME = "patches.yaml"


def _quoted(text: str) -> str:
    """Return ``text`` as a double-quoted YAML scalar (JSON string syntax is valid YAML)."""
    return json.dumps(text, ensure_ascii=False)


def _require_title_id(game: GameInput) -> str:
    title_id = game.title.title_id
    if title_id is None:
        raise InputError(
            "the title ID of this game could not be determined; pass it with --title-id"
        )
    return title_id


def render_profile_yaml(game: GameInput) -> str:
    """Return the text of a starter ``profile.yaml`` for ``game``."""
    title_id = _require_title_id(game)
    lines = [
        "# BEA profile. Every key is described in docs/PROFILES.md.",
        "# Check it with:  bea profile validate   and   bea build <game folder> --dry-run",
        f"schema: {SCHEMA_VERSION}",
        f"title_id: {_quoted(title_id)}",
    ]
    if game.title.name:
        lines.append(f"name: {_quoted(game.title.name)}")
    lines += [
        "# description: >",
        "#   What this profile changes and what it was tested on.",
        "",
        "target:",
        f"  max_width: {DEFAULT_TARGET_WIDTH}",
        f"  max_height: {DEFAULT_TARGET_HEIGHT}",
        "",
        "textures:",
        "  rules: []",
        "  # First match wins. Examples:",
        '  # - match: ["ui/**"]',
        "  #   max_width: 1920",
        "  #   max_height: 1080",
        '  # - match: ["**/*_lut.*", "fonts/**"]',
        "  #   skip: true",
        "",
        "# media:",
        "#   rules:",
        '#     - match: "movies/**/*.mp4"',
        "#       max_height: 720",
        "#       max_fps: 30",
        "",
        "# configs:",
        '#   - match: "config/graphics.ini"',
        "#     format: ini",
        "#     edits:",
        '#       - {section: "Rendering", key: "ResolutionScale", value: "70"}',
        "",
    ]
    return "\n".join(lines)


def render_patches_yaml(game: GameInput) -> str:
    """Return the text of a starter ``patches.yaml`` for ``game``."""
    title_id = _require_title_id(game)
    lines = [
        "# Executable patches, per build. Every key is described in docs/PROFILES.md.",
        "# A patch applies only to the exact build it was written for, and only when the",
        "# verification window of every hunk matches the user's own executable.",
        f"schema: {SCHEMA_VERSION}",
        f"title_id: {_quoted(title_id)}",
    ]
    main = game.module("main")
    if main is None:
        lines.append("builds: []")
    else:
        label = game.title.display_version or "unknown version"
        lines += [
            "builds:",
            f"  - build_id: {_quoted(main.build_id)}",
            "    module: main",
            f"    label: {_quoted(label)}",
            "    patches: []",
            "    # - name: example-patch",
            "    #   category: resolution",
            '    #   description: "What it changes, in your own words."',
            "    #   hunks:",
            "    #     # Generate each hunk with:",
            '    #     #   bea nso hunk <ExeFS/main> --offset 0x... --patch "..."',
            "    #     - offset: 0x0",
            '    #       patch_bytes: "1F 20 03 D5"',
            '    #       expected_sha256: "<64 hex digits printed by bea nso hunk>"',
            "    #       window_offset: 0x0",
            "    #       window_length: 32",
        ]
    others = [module for module in game.modules if module.name != "main"]
    if others:
        lines.append("")
        lines.append("# Other executable modules of this dump (rarely patched):")
        lines += [f"#   {module.name}: build {module.build_id}" for module in others]
    lines.append("")
    return "\n".join(lines)


def render_notes(game: GameInput) -> str:
    """Return the text of a starter ``NOTES.md``: where an author records what was tested."""
    title_id = _require_title_id(game)
    version = game.title.display_version or "(fill in)"
    return "\n".join(
        [
            f"# Notes for {title_id}",
            "",
            "Record here what a reader needs in order to trust this profile. Text only:",
            "no screenshots, no listings, nothing copied from the game.",
            "",
            f"- Game version this profile was written for: {version}",
            "- Hardware and system software it was tested on:",
            "- What was tested (scenes, duration) and what was measured:",
            "- Known problems:",
            "- How each patch site was found (method, in your own words):",
            "",
        ]
    )


def write_profile_skeleton(game: GameInput, profiles_dir: Path, *, force: bool = False) -> Path:
    """Create ``<profiles_dir>/<TitleID>/`` with a starter profile and return its path.

    Raises:
        InputError: the title ID of the game is unknown.
        OutputError: a profile already exists there and ``force`` is false.
    """
    title_id = _require_title_id(game)
    directory = profiles_dir / title_id
    profile_path = directory / _PROFILE_FILENAME
    if profile_path.exists() and not force:
        raise OutputError(f"{profile_path} already exists; pass --force to overwrite it")
    ensure_dir(directory)
    atomic_write_text(profile_path, render_profile_yaml(game))
    atomic_write_text(directory / _PATCHES_FILENAME, render_patches_yaml(game))
    notes = directory / NOTES_FILENAME
    if force or not notes.exists():
        atomic_write_text(notes, render_notes(game))
    return directory
