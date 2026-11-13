"""The build pipeline: plan, execute, stage, package.

``build`` turns a game folder plus a profile into a packaged mod, in four steps:

1. **Plan.** Every transformer inspects the game and the profile and yields the
   :class:`~bea.transformers.base.Action` items it will run and the
   :class:`~bea.transformers.base.Skip` items it will not. Nothing is written. A dry
   run stops here.
2. **Execute.** Each action runs once and its output lands in the *staging tree*
   (``<work>/staging/<logical path>``). An action is skipped when nothing it depends on
   changed since the previous run: the cache key covers the transformer (name and
   version), the action's parameters, and the size and modification time of its source
   file. Outputs are written atomically, so an interrupted build never leaves a broken
   file behind and simply resumes.
3. **Reconcile.** Staged files that the current plan no longer produces are deleted, so
   editing a profile can never leave stale outputs in a package.
4. **Package.** The packager lays the staged files out for the target (LayeredFS).

One failing action does not stop the others: everything that can be built is built, the
failures are reported, and the build is marked as not ok.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bea import __version__
from bea.errors import BeaError, InputError
from bea.fsutil import (
    atomic_output,
    atomic_write_text,
    fs_path,
    prune_empty_dirs,
    remove_file,
    resolve_relative,
    walk_files,
)
from bea.model import ROMFS_PREFIX, GameInput
from bea.packagers.base import PackageContext, Packager, PackageResult, StagedFile
from bea.profile.schema import Profile
from bea.toolbox import Toolbox
from bea.transformers.base import Action, PlanContext, Skip, SkipKind, Transformer

__all__ = [
    "NOTICE_NAME",
    "REPORT_NAME",
    "ActionOutcome",
    "ActionStatus",
    "BuildOptions",
    "BuildResult",
    "Plan",
    "build",
    "default_patchset_name",
    "make_plan",
]

#: File written next to the package describing what was built.
REPORT_NAME = "bea_build.json"
REPORT_SCHEMA = 1
#: Notice written next to every package: its files are derived from the user's game.
NOTICE_NAME = "DO_NOT_DISTRIBUTE.txt"
_NOTICE = """The files in this folder were produced by BEA from your own copy of a game.

They are derived from that game: keep them for your own use, on your own hardware.
Do not share, upload or sell them. To share your work, share the BEA profile
(profile.yaml / patches.yaml) instead: it contains no part of the game.
"""

_STATE_NAME = "state.json"
_STATE_SCHEMA = 1
_STAGING_DIRNAME = "staging"


class ActionStatus(StrEnum):
    """What happened to a planned action."""

    #: Dry run: the action would be executed.
    PLANNED = "planned"
    #: Executed in this run.
    DONE = "done"
    #: Up to date from a previous run; not executed again.
    CACHED = "cached"
    #: Could not be completed; ``error`` says why.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything the transformers decided, before anything is written."""

    #: Sorted by output path, so a plan is identical from run to run.
    actions: tuple[Action, ...]
    skips: tuple[Skip, ...]

    @property
    def has_errors(self) -> bool:
        """Tell whether the profile does not fit the game somewhere."""
        return any(skip.is_error for skip in self.skips)


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """Where and how to build."""

    #: Directory that receives the package of this game.
    out_dir: Path
    #: Directory that holds the staging tree and the cache state of this game.
    work_dir: Path
    #: Only plan; write nothing.
    dry_run: bool = False
    #: Ignore the cache and execute every action again.
    force: bool = False
    #: Number of actions executed at the same time.
    jobs: int = 1


@dataclass(slots=True)
class ActionOutcome:
    """The result of one action."""

    action: Action
    status: ActionStatus
    #: Size of the source file.
    bytes_in: int = 0
    #: Size of the produced file (0 when nothing was produced).
    bytes_out: int = 0
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form used in the build report."""
        return {
            "transformer": self.action.transformer,
            "source": self.action.source,
            "output": self.action.output,
            "status": self.status.value,
            "summary": self.action.summary,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "notes": list(self.notes),
            "details": _jsonable({**self.action.details, **self.details}),
            "error": self.error,
        }


@dataclass(slots=True)
class BuildResult:
    """Everything a build did (or, for a dry run, would do)."""

    game: GameInput
    profile: Profile
    options: BuildOptions
    packager: str
    plan: Plan
    outcomes: list[ActionOutcome]
    package: PackageResult | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Tell whether the build is complete: no failed action, no profile mismatch."""
        return not self.plan.has_errors and all(
            outcome.status is not ActionStatus.FAILED for outcome in self.outcomes
        )

    def status_counts(self) -> dict[str, int]:
        """Return how many actions ended in each status."""
        counts = Counter(outcome.status.value for outcome in self.outcomes)
        return {status.value: counts.get(status.value, 0) for status in ActionStatus}

    def skip_counts(self) -> dict[str, int]:
        """Return how many files were skipped, by reason kind."""
        counts = Counter(skip.kind.value for skip in self.plan.skips)
        return {kind.value: counts.get(kind.value, 0) for kind in SkipKind}

    def asset_bytes(self) -> tuple[int, int]:
        """Return ``(before, after)`` sizes of the RomFS files that were produced."""
        before = after = 0
        for outcome in self.outcomes:
            if outcome.status is ActionStatus.FAILED or outcome.status is ActionStatus.PLANNED:
                continue
            if outcome.action.output.startswith(ROMFS_PREFIX):
                before += outcome.bytes_in
                after += outcome.bytes_out
        return before, after

    def limitations(self) -> list[str]:
        """Return plain statements of what this build did not do or did not verify."""
        statements = [
            "BEA checks bytes and file formats, not behaviour: test the result on your own "
            "hardware before relying on it.",
            "Only loose RomFS files are processed; assets packed inside engine containers "
            "are left untouched in this version.",
        ]
        skipped = self.skip_counts()
        if skipped[SkipKind.UNSUPPORTED.value]:
            statements.append(
                f"{skipped[SkipKind.UNSUPPORTED.value]} file(s) were recognized but could not "
                "be processed by this version (see 'skipped')."
            )
        if self.profile.is_generic:
            statements.append(
                "No profile exists for this game: only generic texture downsizing was applied "
                "(no configuration edits, no executable patches)."
            )
        if self.options.dry_run:
            statements.append("Dry run: nothing was written.")
        return statements

    def to_report(self) -> dict[str, Any]:
        """Return the JSON-serializable build report."""
        before, after = self.asset_bytes()
        title = self.game.title
        package = self.package
        return {
            "schema": REPORT_SCHEMA,
            "tool": {"name": "bea", "version": __version__},
            "command": "build",
            "dry_run": self.options.dry_run,
            "ok": self.ok,
            "input": {
                "path": str(self.game.root),
                "title_id": title.title_id,
                "name": title.name,
                "display_version": title.display_version,
                "modules": [
                    {"name": module.name, "build_id": module.build_id, "size": module.size}
                    for module in self.game.modules
                ],
            },
            "profile": _jsonable(self.profile.describe()),
            "packager": self.packager,
            "summary": {
                "actions": len(self.outcomes),
                "status": self.status_counts(),
                "skipped": self.skip_counts(),
                "asset_bytes_before": before,
                "asset_bytes_after": after,
                "seconds": round(self.seconds, 3),
            },
            "actions": [outcome.as_dict() for outcome in self.outcomes],
            "skipped": [
                {
                    "transformer": skip.transformer,
                    "source": skip.source,
                    "kind": skip.kind.value,
                    "reason": skip.reason,
                }
                for skip in self.plan.skips
            ],
            "package": None
            if package is None
            else {
                "root": str(package.root),
                "files": list(package.files),
                "removed": list(package.removed),
                "linked": package.linked,
                "copied": package.copied,
                "unchanged": package.unchanged,
                "warnings": list(package.warnings),
            },
            "limitations": self.limitations(),
        }


# ------------------------------------------------------------------------- planning


def make_plan(context: PlanContext, transformers: Sequence[Transformer]) -> Plan:
    """Ask every transformer what it will do and merge the answers into one plan.

    Two actions may not write the same output (compared without regard to letter case,
    because the package ends up on case-insensitive filesystems): the first transformer
    wins and the second is recorded as a profile mismatch.
    """
    actions: list[Action] = []
    skips: list[Skip] = []
    owners: dict[str, Action] = {}
    for transformer in transformers:
        for item in transformer.plan(context):
            if isinstance(item, Skip):
                skips.append(item)
                continue
            previous = owners.get(item.output.casefold())
            if previous is not None:
                skips.append(
                    Skip(
                        transformer=item.transformer,
                        source=item.source,
                        kind=SkipKind.MISMATCH,
                        reason=(
                            f"{item.output} is already produced by the "
                            f"'{previous.transformer}' transformer from {previous.source}"
                        ),
                    )
                )
                continue
            owners[item.output.casefold()] = item
            actions.append(item)
    actions.sort(key=lambda action: action.output)
    skips.sort(key=lambda skip: (skip.source, skip.transformer))
    return Plan(actions=tuple(actions), skips=tuple(skips))


def default_patchset_name(profile: Profile, title_id: str) -> str:
    """Return the folder name used for executable patches / the flat mod folder."""
    return profile.binary.patchset_name or f"bea-{title_id}"


# ------------------------------------------------------------------------ execution


def build(
    game: GameInput,
    profile: Profile,
    *,
    transformers: Sequence[Transformer],
    packager: Packager,
    tools: Toolbox,
    options: BuildOptions,
    on_outcome: Callable[[ActionOutcome, int, int], None] | None = None,
) -> BuildResult:
    """Run the pipeline and return what happened.

    ``on_outcome(outcome, done, total)`` is called from the calling thread after each
    action finishes (in completion order), for progress display.

    Raises:
        InputError: the game's title ID is unknown (nothing can be packaged without it).
        OutputError: the output location cannot be used safely.
    """
    started = time.perf_counter()
    title_id = game.title.title_id
    if title_id is None:
        raise InputError(
            "the title ID of this game could not be determined; pass it with --title-id"
        )
    names = [transformer.name for transformer in transformers]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate transformer names: {names}")

    plan = make_plan(PlanContext(game=game, profile=profile, tools=tools), transformers)
    result = BuildResult(
        game=game,
        profile=profile,
        options=options,
        packager=packager.name,
        plan=plan,
        outcomes=[],
    )

    if options.dry_run:
        result.outcomes = [
            ActionOutcome(action, ActionStatus.PLANNED, bytes_in=_source_size(game, action))
            for action in plan.actions
        ]
        result.seconds = time.perf_counter() - started
        return result

    staging = options.work_dir / _STAGING_DIRNAME
    state = _State.load(options.work_dir / _STATE_NAME)
    if options.force:
        state.entries.clear()
    executor = _Executor(game, {t.name: t for t in transformers}, staging, state)
    try:
        result.outcomes = executor.run(plan.actions, max(1, options.jobs), on_outcome)
    finally:
        # Whatever happened (including Ctrl+C), remember the work that did complete.
        state.save()

    produced = {
        outcome.action.output
        for outcome in result.outcomes
        if outcome.status in (ActionStatus.DONE, ActionStatus.CACHED)
    }
    _remove_stale(staging, produced, state)
    state.save()

    staged = []
    for output in sorted(produced):
        path = resolve_relative(staging, output)
        staged.append(StagedFile(output, path, os.stat(fs_path(path)).st_size))
    context = PackageContext(
        title_id=title_id, patchset_name=default_patchset_name(profile, title_id)
    )
    result.package = packager.package(staged, options.out_dir, context)
    result.seconds = time.perf_counter() - started
    atomic_write_text(options.out_dir / NOTICE_NAME, _NOTICE)
    atomic_write_text(
        options.out_dir / REPORT_NAME,
        json.dumps(result.to_report(), indent=2, ensure_ascii=False) + "\n",
    )
    return result


class _State:
    """The cache state of one game: which staged file came from which inputs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}

    @classmethod
    def load(cls, path: Path) -> _State:
        state = cls(path)
        try:
            with open(fs_path(path), encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return state  # no state, or unreadable: everything is rebuilt
        if not isinstance(document, dict) or document.get("schema") != _STATE_SCHEMA:
            return state
        entries = document.get("entries")
        if isinstance(entries, dict):
            state.entries = {
                key: value
                for key, value in entries.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
        return state

    def save(self) -> None:
        document = {"schema": _STATE_SCHEMA, "bea_version": __version__, "entries": self.entries}
        atomic_write_text(self.path, json.dumps(document, indent=1, sort_keys=True) + "\n")


class _Executor:
    """Runs actions, consulting and updating the cache state."""

    def __init__(
        self,
        game: GameInput,
        transformers: Mapping[str, Transformer],
        staging: Path,
        state: _State,
    ) -> None:
        self._game = game
        self._transformers = transformers
        self._staging = staging
        self._state = state

    def run(
        self,
        actions: Sequence[Action],
        jobs: int,
        on_outcome: Callable[[ActionOutcome, int, int], None] | None,
    ) -> list[ActionOutcome]:
        total = len(actions)
        outcomes: list[ActionOutcome | None] = [None] * total
        done = 0

        def record(index: int, outcome: ActionOutcome, entry: dict[str, Any] | None) -> None:
            nonlocal done
            output = actions[index].output
            if outcome.status is ActionStatus.DONE and entry is not None:
                self._state.entries[output] = entry
            elif outcome.status is ActionStatus.FAILED:
                self._state.entries.pop(output, None)
            outcomes[index] = outcome
            done += 1
            if on_outcome is not None:
                on_outcome(outcome, done, total)

        if jobs <= 1 or total <= 1:
            for index in range(total):
                record(index, *self._run_one(actions[index]))
        else:
            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="bea") as pool:
                futures = {
                    pool.submit(self._run_one, action): i for i, action in enumerate(actions)
                }
                try:
                    for future in as_completed(futures):
                        record(futures[future], *future.result())
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        return [outcome for outcome in outcomes if outcome is not None]

    def _run_one(self, action: Action) -> tuple[ActionOutcome, dict[str, Any] | None]:
        started = time.perf_counter()
        staged: Path | None = None
        bytes_in = 0
        try:
            transformer = self._transformers[action.transformer]
            source = self._game.resolve(action.source)
            source_stat = os.stat(fs_path(source))
            bytes_in = source_stat.st_size
            staged = resolve_relative(self._staging, action.output)
            key = _cache_key(action, transformer, source_stat.st_size, source_stat.st_mtime_ns)

            entry = self._state.entries.get(action.output)
            if entry is not None and entry.get("key") == key and _staged_matches(staged, entry):
                return (
                    ActionOutcome(
                        action,
                        ActionStatus.CACHED,
                        bytes_in=bytes_in,
                        bytes_out=int(entry.get("size", 0)),
                        notes=[str(note) for note in entry.get("notes", [])],
                        details=dict(entry.get("details", {})),
                    ),
                    None,
                )

            with atomic_output(staged) as temporary:
                result = transformer.transform(source, temporary, action.params)
                if not os.path.isfile(temporary):
                    raise BeaError(f"the {transformer.name} transformer produced no output file")
            staged_stat = os.stat(fs_path(staged))
            details = _jsonable(result.details)
            outcome = ActionOutcome(
                action,
                ActionStatus.DONE,
                bytes_in=bytes_in,
                bytes_out=staged_stat.st_size,
                notes=list(result.notes),
                details=details,
                seconds=time.perf_counter() - started,
            )
            new_entry = {
                "key": key,
                "size": staged_stat.st_size,
                "mtime_ns": staged_stat.st_mtime_ns,
                "notes": list(result.notes),
                "details": details,
            }
            return outcome, new_entry
        except KeyboardInterrupt:
            raise
        except BeaError as exc:
            error = str(exc)
        except Exception as exc:
            # A transformer bug must not take the whole build down: record it and go on.
            error = f"internal error ({type(exc).__name__}): {exc}"
        # A failed action must not leave an output from an older run to be packaged.
        if staged is not None:
            remove_file(staged)
        return (
            ActionOutcome(
                action,
                ActionStatus.FAILED,
                bytes_in=bytes_in,
                error=error,
                seconds=time.perf_counter() - started,
            ),
            None,
        )


def _cache_key(action: Action, transformer: Transformer, size: int, mtime_ns: int) -> str:
    payload = {
        "transformer": transformer.name,
        "version": transformer.version,
        "source": action.source,
        "source_size": size,
        "source_mtime_ns": mtime_ns,
        "output": action.output,
        "params": _jsonable(action.params),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _staged_matches(staged: Path, entry: Mapping[str, Any]) -> bool:
    """Tell whether the staged file is still the one the state entry describes."""
    try:
        stat = os.stat(fs_path(staged))
    except OSError:
        return False
    return stat.st_size == entry.get("size") and stat.st_mtime_ns == entry.get("mtime_ns")


def _remove_stale(staging: Path, produced: set[str], state: _State) -> None:
    """Delete staged files (and state entries) that the current build did not produce."""
    if os.path.isdir(fs_path(staging)):
        for found in list(walk_files(staging)):
            if found.path not in produced:
                remove_file(resolve_relative(staging, found.path))
        prune_empty_dirs(staging)
    for output in [output for output in state.entries if output not in produced]:
        del state.entries[output]


def _source_size(game: GameInput, action: Action) -> int:
    try:
        return os.stat(fs_path(game.resolve(action.source))).st_size
    except (OSError, ValueError):
        return 0


def _jsonable(value: Any) -> Any:
    """Convert mappings, sequences, sets, paths and bytes into plain JSON types."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex().upper()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, StrEnum):
        return value.value
    return str(value)
