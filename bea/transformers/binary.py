"""The binary transformer: verified executable patches, written as IPS files.

A profile's ``patches.yaml`` declares, per build of a module, small byte replacements
(*hunks*) grouped in named *patches*. This transformer:

1. picks the build entry whose module name and build ID match a module of the user's
   game folder - nothing else is ever patched, and system software never;
2. verifies **every** hunk of **every** enabled patch against the original, decompressed
   module: the patched range and the verification window must each lie inside one
   segment, and the window must have the declared SHA-256 (or the range must hold the
   declared bytes);
3. only when all of that holds, writes ``<BUILD_ID>.ips`` (or, in ``replace`` mode, a
   patched copy of the module).

Two promises follow and are tested: BEA never patches a build the profile was not
written for, and never writes a partial patch set. A single failed check means no
output for that module, and the build is reported as not ok.

An IPS record offset is the module memory offset plus ``0x100`` (the size of the NSO
header, which loaders count but never patch). IPS files hold only the replacement
bytes, with one exception the format forces: a record that would start at the
terminator's offset starts one byte earlier and repeats that original byte. The build
report counts such bytes (``original_bytes_included``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from bea.errors import BeaError, PatchError
from bea.formats.ips import build_ips, parse_ips
from bea.formats.nso import HEADER_SIZE, NsoFile
from bea.hexutil import format_hex_bytes, is_application_id
from bea.model import EXEFS_PATCHES_PREFIX, EXEFS_PREFIX
from bea.profile.schema import BuildPatches, ExefsMode, Hunk
from bea.transformers.base import (
    Action,
    PlanContext,
    PlanItem,
    Skip,
    SkipKind,
    Transformer,
    TransformResult,
)

__all__ = ["BinaryTransformer", "HunkCheck", "hunks_to_params", "verify_build"]

_NO_BUILD_ID = "00"
_MAX_LISTED_PROBLEMS = 10


@dataclass(frozen=True, slots=True)
class HunkCheck:
    """The verification of one hunk."""

    patch: str
    #: Position of the hunk in its patch.
    index: int
    offset: int
    ok: bool
    problem: str | None = None


def _check_hunk(nso: NsoFile, hunk: Hunk) -> str | None:
    """Return why ``hunk`` does not fit ``nso``, or ``None`` when it does."""
    length = len(hunk.patch)
    if nso.find_segment(hunk.offset, length) is None:
        return (
            f"the patched range 0x{hunk.offset:X}..0x{hunk.offset + length:X} does not lie "
            "inside one segment of this module"
        )
    if hunk.expected_sha256 is not None:
        start, size = hunk.window_offset, hunk.window_length
        if nso.find_segment(start, size) is None:
            return (
                f"the verification window 0x{start:X}..0x{start + size:X} does not lie "
                "inside one segment of this module"
            )
        digest = hashlib.sha256(nso.read_memory(start, size)).hexdigest()
        if digest != hunk.expected_sha256:
            # The bytes themselves are not shown: the profile deliberately holds none.
            return (
                f"the {size} bytes at 0x{start:X} do not have the expected SHA-256 "
                "(this is not the code the patch was written for)"
            )
        return None
    if hunk.expected is not None:
        found = nso.read_memory(hunk.offset, length)
        if found != hunk.expected:
            return (
                f"at 0x{hunk.offset:X} expected {format_hex_bytes(hunk.expected)}, "
                f"found {format_hex_bytes(found)}"
            )
        return None
    return "the hunk declares no verification"


def verify_build(nso: NsoFile, build: BuildPatches) -> list[HunkCheck]:
    """Check every hunk of every enabled patch of ``build`` against the original module.

    Also reported: hunks of two different enabled patches that replace the same bytes
    (within one patch the loader already refuses overlaps). Disabled patches are not
    checked. One :class:`HunkCheck` per hunk, in declaration order.
    """
    checks: list[HunkCheck] = []
    claimed: list[tuple[int, int, str, int]] = []
    for patch in build.patches:
        if not patch.enabled:
            continue
        for index, hunk in enumerate(patch.hunks):
            problem = _check_hunk(nso, hunk)
            if problem is None:
                for start, end, other, other_index in claimed:
                    if other != patch.name and hunk.offset < end and start < hunk.end:
                        problem = (
                            f"replaces the same bytes as {other} hunk {other_index} "
                            f"(0x{max(start, hunk.offset):X})"
                        )
                        break
            claimed.append((hunk.offset, hunk.end, patch.name, index))
            checks.append(HunkCheck(patch.name, index, hunk.offset, problem is None, problem))
    return checks


def hunks_to_params(build: BuildPatches) -> list[dict[str, Any]]:
    """Return the JSON form of the enabled hunks of ``build`` (an action's parameters)."""
    return [
        {
            "patch": patch.name,
            "offset": hunk.offset,
            "bytes": hunk.patch.hex().upper(),
            "expected": None if hunk.expected is None else hunk.expected.hex().upper(),
            "sha256": hunk.expected_sha256,
            "window_offset": hunk.window_offset,
            "window_length": hunk.window_length,
        }
        for patch in build.patches
        if patch.enabled
        for hunk in patch.hunks
    ]


def _hunks_from_params(config: Mapping[str, Any]) -> list[tuple[str, Hunk]]:
    hunks = []
    for item in config["hunks"]:
        expected = item.get("expected")
        hunks.append(
            (
                str(item["patch"]),
                Hunk(
                    offset=int(item["offset"]),
                    patch=bytes.fromhex(item["bytes"]),
                    expected=None if expected is None else bytes.fromhex(expected),
                    expected_sha256=item.get("sha256"),
                    window_offset=int(item.get("window_offset", 0)),
                    window_length=int(item.get("window_length", 0)),
                ),
            )
        )
    return hunks


def _summarize(problems: Iterable[str]) -> str:
    listed = list(problems)
    shown = "; ".join(listed[:_MAX_LISTED_PROBLEMS])
    if len(listed) > _MAX_LISTED_PROBLEMS:
        shown += f"; ... and {len(listed) - _MAX_LISTED_PROBLEMS} more"
    return shown


class BinaryTransformer(Transformer):
    """Verifies declared executable patches and writes them as IPS (or a patched module)."""

    name: ClassVar[str] = "binary"
    version: ClassVar[int] = 1

    def supports(self, path: str) -> bool:
        return path.startswith(EXEFS_PREFIX)

    def plan(self, context: PlanContext) -> Iterable[PlanItem]:
        patches = context.profile.patches
        if patches is None or not patches.builds:
            return
        title_id = context.game.title.title_id
        if title_id is not None and not is_application_id(title_id):
            yield Skip(
                self.name,
                "exefs",
                SkipKind.MISMATCH,
                f"{title_id} is system software: BEA patches only applications",
            )
            return

        present = {module.name for module in context.game.modules}
        for module in context.game.modules:
            build = patches.for_module(module.name, module.build_id)
            if build is None:
                known = [entry for entry in patches.builds if entry.module == module.name]
                if known:
                    listing = ", ".join(
                        entry.build_id + (f" ({entry.label})" if entry.label else "")
                        for entry in known
                    )
                    yield Skip(
                        self.name,
                        module.logical,
                        SkipKind.UNSUPPORTED,
                        f"the profile has no patches for build {module.build_id} of "
                        f"{module.name}; it knows: {listing}",
                    )
                continue
            if not any(patch.enabled for patch in build.patches):
                continue
            if module.build_id == _NO_BUILD_ID:
                yield Skip(
                    self.name,
                    module.logical,
                    SkipKind.UNSUPPORTED,
                    f"{module.name} has no build ID, so no patch file can target it",
                )
                continue
            item = self._plan_module(context, module.name, module.logical, module.path, build)
            if item is not None:
                yield item

        for entry in patches.builds:
            if entry.module not in present:
                yield Skip(
                    self.name,
                    EXEFS_PREFIX + entry.module,
                    SkipKind.UNSUPPORTED,
                    f"the profile has patches for {entry.module}, which this game folder "
                    "does not contain",
                )

    def _plan_module(
        self, context: PlanContext, name: str, logical: str, path: Path, build: BuildPatches
    ) -> PlanItem | None:
        try:
            nso = NsoFile.read(path)
        except BeaError as exc:
            return Skip(self.name, logical, SkipKind.INVALID, str(exc))
        checks = verify_build(nso, build)
        failed = [check for check in checks if not check.ok]
        if failed:
            # All or nothing: one failed check and the module gets no output at all.
            return Skip(
                self.name,
                logical,
                SkipKind.MISMATCH,
                "the profile does not fit this build, nothing is patched: "
                + _summarize(
                    f"{check.patch} hunk {check.index}: {check.problem}" for check in failed
                ),
            )
        params_hunks = hunks_to_params(build)
        effective = [
            item
            for item in params_hunks
            if nso.read_memory(item["offset"], len(item["bytes"]) // 2)
            != bytes.fromhex(item["bytes"])
        ]
        if not effective:
            return None  # every hunk is a pure assertion: nothing to write
        enabled = [patch.name for patch in build.patches if patch.enabled]
        mode = context.profile.binary.mode
        output = (
            f"{EXEFS_PATCHES_PREFIX}{build.build_id}.ips"
            if mode is ExefsMode.IPS
            else f"{EXEFS_PREFIX}{name}"
        )
        patched_bytes = sum(len(item["bytes"]) // 2 for item in effective)
        return Action(
            transformer=self.name,
            source=logical,
            output=output,
            params={
                "mode": mode.value,
                "module": name,
                "build_id": build.build_id,
                "hunks": params_hunks,
                "patches": enabled,
            },
            summary=(
                f"{len(enabled)} patch(es), {len(effective)} hunk(s) -> "
                + ("IPS" if mode is ExefsMode.IPS else "patched module")
            ),
            details={"patches": enabled, "hunks": len(effective), "patched_bytes": patched_bytes},
        )

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        nso = NsoFile.read(src)
        hunks = _hunks_from_params(config)
        # The file may have changed since planning: verify again, against this read.
        problems = []
        for patch_name, hunk in hunks:
            problem = _check_hunk(nso, hunk)
            if problem is not None:
                problems.append(f"{patch_name}: {problem}")
        if problems:
            raise PatchError(
                f"{config['module']} no longer matches the profile, nothing is patched: "
                + _summarize(problems)
            )
        changes = [
            (hunk.offset, hunk.patch)
            for _name, hunk in hunks
            if nso.read_memory(hunk.offset, len(hunk.patch)) != hunk.patch
        ]
        patched = sum(len(data) for _offset, data in changes)
        result = TransformResult()

        if config["mode"] == ExefsMode.REPLACE.value:
            Path(dst).write_bytes(nso.with_patches(changes).to_bytes())
            result.details = {
                "format": "nso",
                "patched_bytes": patched,
                "contains_game_bytes": True,
            }
            result.notes.append(
                "this file is the whole executable of the game: keep it for yourself, never share it"
            )
            return result

        image = nso.image()
        data = build_ips(
            [(offset + HEADER_SIZE, payload) for offset, payload in changes],
            original_byte=lambda offset: image[offset - HEADER_SIZE],
        )
        Path(dst).write_bytes(data)
        written = parse_ips(data)
        included = sum(len(record.data) for record in written.records) - patched
        result.details = {
            "format": written.format.value,
            "records": len(written.records),
            "patched_bytes": patched,
            "original_bytes_included": included,
            "contains_game_bytes": included > 0,
        }
        if included:
            result.notes.append(
                f"{included} original byte(s) of the module are part of the patch file (the "
                "IPS format cannot start a record at its terminator's offset); do not share it"
            )
        return result
