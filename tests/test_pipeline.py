"""Tests for bea.pipeline: planning, cached execution, reconciliation, packaging.

The pipeline is exercised with tiny fake transformers and a fake packager, so these
tests pin down the orchestration itself and nothing about any file format.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from bea.errors import BeaError, InputError
from bea.fsutil import walk_files
from bea.model import FileEntry, GameInput, TitleInfo
from bea.packagers.base import PackageContext, Packager, PackageResult, StagedFile
from bea.pipeline import (
    REPORT_NAME,
    ActionStatus,
    BuildOptions,
    BuildResult,
    build,
    default_patchset_name,
    make_plan,
)
from bea.profile.schema import BinarySettings, Profile, generic_profile
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

TITLE_ID = f"{(0x0100 << 48) | 0xBEA0BEA0A000:016X}"


class ShoutTransformer(Transformer):
    """Upper-cases ``.txt`` files and appends a suffix; refuses ``.bad``; fails on demand."""

    name: ClassVar[str] = "shout"
    version: ClassVar[int] = 1

    def __init__(self, suffix: str = "!") -> None:
        self.suffix = suffix
        self.calls: list[str] = []

    def supports(self, path: str) -> bool:
        return path.endswith(".txt")

    def plan(self, context: PlanContext) -> Iterable[PlanItem]:
        for entry in context.game.romfs_files:
            if entry.ext == ".txt":
                yield Action(
                    transformer=self.name,
                    source=entry.logical,
                    output=entry.logical,
                    params={"suffix": self.suffix},
                    summary="shout",
                    details={"planned": True},
                )
            elif entry.ext == ".bad":
                yield Skip(self.name, entry.logical, SkipKind.UNSUPPORTED, "cannot shout this")

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        self.calls.append(Path(src).name)
        text = Path(src).read_bytes()
        if b"explode" in text:
            raise BeaError("this file explodes")
        if b"crash" in text:
            raise RuntimeError("unexpected bug")
        if b"silent" in text:
            return TransformResult()  # writes nothing: the pipeline must notice
        Path(dst).write_bytes(text.upper() + str(config["suffix"]).encode())
        return TransformResult(notes=["shouted"], details={"length": len(text)})


class EchoTransformer(Transformer):
    """Claims the same outputs as :class:`ShoutTransformer` (to provoke a collision)."""

    name: ClassVar[str] = "echo"

    def supports(self, path: str) -> bool:
        return path.endswith(".txt")

    def plan(self, context: PlanContext) -> Iterable[PlanItem]:
        for entry in context.game.romfs_files:
            if entry.name == "a.txt":
                yield Action(self.name, entry.logical, "romfs/A.TXT", {}, "echo")

    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        Path(dst).write_bytes(Path(src).read_bytes())
        return TransformResult()


class CopyPackager(Packager):
    """Copies staged files to ``out_dir/<logical>`` and remembers what it was given."""

    name: ClassVar[str] = "copy"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], PackageContext]] = []

    def destination(self, logical: str, context: PackageContext) -> str:
        return logical

    def package(
        self, staged: Sequence[StagedFile], out_dir: Path, context: PackageContext
    ) -> PackageResult:
        self.calls.append(([item.logical for item in staged], context))
        result = PackageResult(root=out_dir)
        for item in staged:
            target = out_dir / item.logical
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.path.read_bytes())
            result.files.append(item.logical)
            result.copied += 1
        return result


def make_game(
    root: Path, files: Mapping[str, bytes], *, title_id: str | None = TITLE_ID
) -> GameInput:
    romfs = root / "RomFS"
    for relative, content in files.items():
        path = romfs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    romfs.mkdir(parents=True, exist_ok=True)
    entries = tuple(
        FileEntry(found.path, found.size, found.mtime_ns) for found in walk_files(romfs)
    )
    return GameInput(
        root=root,
        exefs_dir=None,
        romfs_dir=romfs,
        nacp_path=None,
        title=TitleInfo(title_id=title_id, name="Synthetic", sources={"title_id": "option"}),
        modules=(),
        exefs_files=(),
        romfs_files=entries,
    )


def options(tmp_path: Path, **overrides: Any) -> BuildOptions:
    values: dict[str, Any] = {"out_dir": tmp_path / "out", "work_dir": tmp_path / "work"}
    values.update(overrides)
    return BuildOptions(**values)


def run(
    tmp_path: Path,
    game: GameInput,
    transformers: Sequence[Transformer],
    packager: Packager | None = None,
    profile: Profile | None = None,
    **overrides: Any,
) -> BuildResult:
    return build(
        game,
        profile or generic_profile(),
        transformers=transformers,
        packager=packager or CopyPackager(),
        tools=Toolbox(tmp_path / "no-tools", environ={}),
        options=options(tmp_path, **overrides),
    )


def statuses(result: BuildResult) -> dict[str, str]:
    return {outcome.action.output: outcome.status.value for outcome in result.outcomes}


FILES = {"a.txt": b"alpha", "sub/b.txt": b"beta", "c.bad": b"nope", "d.bin": b"ignored"}


def test_make_plan_is_sorted_and_keeps_skips(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    context = PlanContext(game=game, profile=generic_profile(), tools=Toolbox(environ={}))
    plan = make_plan(context, [ShoutTransformer()])
    assert [action.output for action in plan.actions] == ["romfs/a.txt", "romfs/sub/b.txt"]
    assert [(skip.source, skip.kind) for skip in plan.skips] == [
        ("romfs/c.bad", SkipKind.UNSUPPORTED)
    ]
    assert not plan.has_errors


def test_output_collision_is_a_profile_mismatch(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    context = PlanContext(game=game, profile=generic_profile(), tools=Toolbox(environ={}))
    plan = make_plan(context, [ShoutTransformer(), EchoTransformer()])
    # "romfs/A.TXT" collides with "romfs/a.txt": packages live on case-insensitive media.
    assert [action.transformer for action in plan.actions] == ["shout", "shout"]
    mismatches = [skip for skip in plan.skips if skip.is_error]
    assert len(mismatches) == 1
    assert mismatches[0].transformer == "echo"
    assert "already produced by the 'shout' transformer" in mismatches[0].reason
    assert plan.has_errors

    result = run(tmp_path, game, [ShoutTransformer(), EchoTransformer()])
    assert not result.ok  # a mismatch makes the build incomplete even if every action ran
    assert set(statuses(result).values()) == {"done"}


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    transformer = ShoutTransformer()
    packager = CopyPackager()
    result = run(tmp_path, game, [transformer], packager, dry_run=True)
    assert statuses(result) == {"romfs/a.txt": "planned", "romfs/sub/b.txt": "planned"}
    assert [outcome.bytes_in for outcome in result.outcomes] == [5, 4]
    assert result.package is None
    assert transformer.calls == [] and packager.calls == []
    assert not (tmp_path / "out").exists() and not (tmp_path / "work").exists()
    assert result.ok
    assert "Dry run: nothing was written." in result.limitations()


def test_build_stages_packages_and_reports(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    packager = CopyPackager()
    result = run(tmp_path, game, [ShoutTransformer("?")], packager)

    assert result.ok
    assert statuses(result) == {"romfs/a.txt": "done", "romfs/sub/b.txt": "done"}
    assert (tmp_path / "work" / "staging" / "romfs" / "a.txt").read_bytes() == b"ALPHA?"
    assert (tmp_path / "out" / "romfs" / "sub" / "b.txt").read_bytes() == b"BETA?"
    assert (game.romfs_dir / "a.txt").read_bytes() == b"alpha"  # type: ignore[operator]

    staged, context = packager.calls[0]
    assert staged == ["romfs/a.txt", "romfs/sub/b.txt"]
    assert context == PackageContext(title_id=TITLE_ID, patchset_name=f"bea-{TITLE_ID}")

    outcome = result.outcomes[0]
    assert (outcome.bytes_in, outcome.bytes_out, outcome.notes) == (5, 6, ["shouted"])
    assert outcome.details == {"length": 5}
    assert result.asset_bytes() == (9, 11)
    assert result.status_counts() == {"planned": 0, "done": 2, "cached": 0, "failed": 0}
    assert result.skip_counts()["unsupported"] == 1

    report = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is True and report["command"] == "build" and report["dry_run"] is False
    assert report["input"]["title_id"] == TITLE_ID
    assert report["packager"] == "copy"
    assert [item["output"] for item in report["actions"]] == ["romfs/a.txt", "romfs/sub/b.txt"]
    assert report["actions"][0]["details"] == {"planned": True, "length": 5}
    assert report["skipped"] == [
        {
            "transformer": "shout",
            "source": "romfs/c.bad",
            "kind": "unsupported",
            "reason": "cannot shout this",
        }
    ]
    assert report["package"]["files"] == ["romfs/a.txt", "romfs/sub/b.txt"]
    assert report["limitations"]


def test_second_build_is_fully_cached(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    first = ShoutTransformer()
    run(tmp_path, game, [first])
    assert sorted(first.calls) == ["a.txt", "b.txt"]

    second = ShoutTransformer()
    result = run(tmp_path, game, [second])
    assert second.calls == []
    assert set(statuses(result).values()) == {"cached"}
    assert result.ok
    cached = result.outcomes[0]
    assert (cached.bytes_in, cached.bytes_out, cached.notes, cached.details) == (
        5,
        6,
        ["shouted"],
        {"length": 5},
    )
    assert (tmp_path / "out" / "romfs" / "a.txt").read_bytes() == b"ALPHA!"


def test_only_what_changed_is_rebuilt(tmp_path: Path) -> None:
    root = tmp_path / "game"
    run(tmp_path, make_game(root, FILES), [ShoutTransformer()])

    target = root / "RomFS" / "a.txt"
    target.write_bytes(b"changed")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    transformer = ShoutTransformer()
    result = run(tmp_path, make_game(root, {}), [transformer])
    assert transformer.calls == ["a.txt"]
    assert statuses(result) == {"romfs/a.txt": "done", "romfs/sub/b.txt": "cached"}
    assert (tmp_path / "out" / "romfs" / "a.txt").read_bytes() == b"CHANGED!"


@pytest.mark.parametrize("change", ["params", "version", "force"])
def test_cache_is_invalidated_by_params_version_and_force(tmp_path: Path, change: str) -> None:
    game = make_game(tmp_path / "game", FILES)
    run(tmp_path, game, [ShoutTransformer()])

    transformer = ShoutTransformer("?" if change == "params" else "!")
    if change == "version":
        transformer.version = 2  # type: ignore[misc]
    result = run(tmp_path, game, [transformer], force=change == "force")
    assert sorted(transformer.calls) == ["a.txt", "b.txt"]
    assert set(statuses(result).values()) == {"done"}


def test_a_tampered_staged_file_is_rebuilt(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    run(tmp_path, game, [ShoutTransformer()])
    (tmp_path / "work" / "staging" / "romfs" / "a.txt").write_bytes(b"tampered and longer")

    transformer = ShoutTransformer()
    result = run(tmp_path, game, [transformer])
    assert transformer.calls == ["a.txt"]
    assert statuses(result)["romfs/a.txt"] == "done"
    assert (tmp_path / "work" / "staging" / "romfs" / "a.txt").read_bytes() == b"ALPHA!"


def test_unreadable_state_means_rebuild_not_crash(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    run(tmp_path, game, [ShoutTransformer()])
    (tmp_path / "work" / "state.json").write_text("{ not json", encoding="utf-8")
    transformer = ShoutTransformer()
    result = run(tmp_path, game, [transformer])
    assert sorted(transformer.calls) == ["a.txt", "b.txt"] and result.ok


def test_outputs_no_longer_planned_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "game"
    run(tmp_path, make_game(root, FILES), [ShoutTransformer()])
    staged = tmp_path / "work" / "staging" / "romfs" / "sub" / "b.txt"
    assert staged.exists()

    (root / "RomFS" / "sub" / "b.txt").unlink()
    packager = CopyPackager()
    result = run(tmp_path, make_game(root, {}), [ShoutTransformer()], packager)
    assert statuses(result) == {"romfs/a.txt": "cached"}
    assert not staged.exists() and not staged.parent.exists()  # stale file and empty dir gone
    assert packager.calls[0][0] == ["romfs/a.txt"]
    state = json.loads((tmp_path / "work" / "state.json").read_text(encoding="utf-8"))
    assert list(state["entries"]) == ["romfs/a.txt"]


def test_one_failure_does_not_stop_the_others(tmp_path: Path) -> None:
    files = {
        "a.txt": b"alpha",
        "b.txt": b"explode",
        "c.txt": b"crash",
        "d.txt": b"silent",
        "e.txt": b"echo",
    }
    game = make_game(tmp_path / "game", files)
    packager = CopyPackager()
    seen: list[tuple[str, int, int]] = []
    result = build(
        game,
        generic_profile(),
        transformers=[ShoutTransformer()],
        packager=packager,
        tools=Toolbox(environ={}),
        options=options(tmp_path),
        on_outcome=lambda outcome, done, total: seen.append((outcome.action.output, done, total)),
    )
    assert not result.ok
    assert statuses(result) == {
        "romfs/a.txt": "done",
        "romfs/b.txt": "failed",
        "romfs/c.txt": "failed",
        "romfs/d.txt": "failed",
        "romfs/e.txt": "done",
    }
    errors = {outcome.action.output: outcome.error for outcome in result.outcomes}
    assert errors["romfs/b.txt"] == "this file explodes"
    assert errors["romfs/c.txt"] == "internal error (RuntimeError): unexpected bug"
    assert errors["romfs/d.txt"] == "the shout transformer produced no output file"
    assert packager.calls[0][0] == ["romfs/a.txt", "romfs/e.txt"]
    staging = tmp_path / "work" / "staging" / "romfs"
    assert sorted(path.name for path in staging.iterdir()) == ["a.txt", "e.txt"]  # no leftovers
    assert [done for _, done, _ in seen] == [1, 2, 3, 4, 5] and {total for *_, total in seen} == {5}
    report = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is False and report["summary"]["status"]["failed"] == 3


def test_a_newly_failing_action_drops_its_old_output(tmp_path: Path) -> None:
    root = tmp_path / "game"
    run(tmp_path, make_game(root, {"a.txt": b"alpha"}), [ShoutTransformer()])
    staged = tmp_path / "work" / "staging" / "romfs" / "a.txt"
    assert staged.exists()

    source = root / "RomFS" / "a.txt"
    source.write_bytes(b"explode now")
    packager = CopyPackager()
    result = run(tmp_path, make_game(root, {}), [ShoutTransformer()], packager)
    assert statuses(result) == {"romfs/a.txt": "failed"}
    assert not staged.exists()  # the stale success must not be packaged
    assert packager.calls[0][0] == []


def test_parallel_execution_gives_the_same_result(tmp_path: Path) -> None:
    files = {
        f"dir{index % 3}/file{index:02d}.txt": f"content {index}".encode() for index in range(24)
    }
    game = make_game(tmp_path / "game", files)
    serial = run(tmp_path / "serial", game, [ShoutTransformer()], jobs=1)
    parallel = run(tmp_path / "parallel", game, [ShoutTransformer()], jobs=8)
    assert [o.action.output for o in serial.outcomes] == [
        o.action.output for o in parallel.outcomes
    ]
    assert statuses(serial) == statuses(parallel)
    for relative in files:
        left = tmp_path / "serial" / "out" / "romfs" / relative
        right = tmp_path / "parallel" / "out" / "romfs" / relative
        assert left.read_bytes() == right.read_bytes()
    again = run(tmp_path / "parallel", game, [ShoutTransformer()], jobs=8)
    assert set(statuses(again).values()) == {"cached"}


def test_title_id_is_required(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES, title_id=None)
    with pytest.raises(InputError, match="--title-id"):
        run(tmp_path, game, [ShoutTransformer()])
    assert not (tmp_path / "out").exists()


def test_duplicate_transformer_names_are_rejected(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    with pytest.raises(ValueError, match="duplicate transformer names"):
        run(tmp_path, game, [ShoutTransformer(), ShoutTransformer()])


def test_nothing_to_do_still_packages_and_reports(tmp_path: Path) -> None:
    root = tmp_path / "game"
    run(tmp_path, make_game(root, {"a.txt": b"alpha"}), [ShoutTransformer()])
    (root / "RomFS" / "a.txt").unlink()
    packager = CopyPackager()
    result = run(tmp_path, make_game(root, {"z.bin": b"z"}), [ShoutTransformer()], packager)
    assert result.ok and result.outcomes == []
    assert packager.calls[0][0] == []  # the packager is told there is nothing left
    assert (tmp_path / "out" / REPORT_NAME).exists()


def test_patchset_name_comes_from_the_profile(tmp_path: Path) -> None:
    profile = Profile(title_id=TITLE_ID, binary=BinarySettings(patchset_name="my-set"))
    assert default_patchset_name(profile, TITLE_ID) == "my-set"
    assert default_patchset_name(generic_profile(), TITLE_ID) == f"bea-{TITLE_ID}"
    game = make_game(tmp_path / "game", FILES)
    packager = CopyPackager()
    run(tmp_path, game, [ShoutTransformer()], packager, profile=profile)
    assert packager.calls[0][1].patchset_name == "my-set"


def test_limitations_mention_generic_profile_and_unsupported_files(tmp_path: Path) -> None:
    game = make_game(tmp_path / "game", FILES)
    result = run(tmp_path, game, [ShoutTransformer()])
    text = " ".join(result.limitations())
    assert "No profile exists" in text and "1 file(s) were recognized" in text
    named = run(tmp_path / "named", game, [ShoutTransformer()], profile=Profile(title_id=TITLE_ID))
    assert "No profile exists" not in " ".join(named.limitations())


def test_status_enum_values_are_stable() -> None:
    # Reports and the command line print these; changing them is a breaking change.
    assert [status.value for status in ActionStatus] == ["planned", "done", "cached", "failed"]
