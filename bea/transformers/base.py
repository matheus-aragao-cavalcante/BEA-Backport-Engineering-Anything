"""The transformer contract.

A transformer turns one input file of the game into one output file of the mod. Work is
split in two phases so that BEA can show what it *would* do before touching anything,
and can skip work that is already done:

``plan``
    Cheap and read-only. Looks at file headers and at the profile, and yields an
    :class:`Action` for every file it will transform or a :class:`Skip` for every file it
    recognizes but will not transform (with the reason).

``transform``
    Executes one action: reads ``src``, writes ``dst``. It receives only the action's
    ``params`` (plain JSON data), never the profile, so the result depends on nothing
    but the source file and those parameters. That is what makes the pipeline cacheable:
    same source fingerprint + same transformer version + same params = same output.

Transformers must be game-agnostic: every game-specific decision arrives through the
profile at plan time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from bea.model import GameInput
from bea.profile.schema import Profile
from bea.toolbox import Toolbox

__all__ = [
    "Action",
    "PlanContext",
    "PlanItem",
    "Skip",
    "SkipKind",
    "TransformResult",
    "Transformer",
]


class SkipKind(StrEnum):
    """Why a recognized file is not transformed."""

    #: This version of BEA cannot do it (missing encoder, missing external tool ...).
    UNSUPPORTED = "unsupported"
    #: The profile says to leave it alone.
    EXCLUDED = "excluded"
    #: The file could not be read or is malformed.
    INVALID = "invalid"
    #: The profile expects something that is not there (a config file, a build, bytes).
    #: Unlike the other kinds this one is an error: the profile does not fit this game.
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class Action:
    """One planned transformation: ``source`` -> ``output``."""

    #: :attr:`Transformer.name` of the transformer that will run it.
    transformer: str
    #: Logical path of the input file (``romfs/...`` or ``exefs/...``).
    source: str
    #: Logical path of the output file (see :mod:`bea.model`).
    output: str
    #: JSON-serializable parameters handed to :meth:`Transformer.transform`.
    params: Mapping[str, Any]
    #: One human-readable line: ``"2048x2048 -> 1024x1024 (mip_drop 1)"``.
    summary: str
    #: JSON-serializable facts for reports (dimensions before/after, formats ...).
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Skip:
    """One file a transformer recognized but will not transform."""

    transformer: str
    #: Logical path of the file (or of the profile item, for :attr:`SkipKind.MISMATCH`).
    source: str
    kind: SkipKind
    reason: str

    @property
    def is_error(self) -> bool:
        """Tell whether this skip means the profile does not fit the game."""
        return self.kind is SkipKind.MISMATCH


type PlanItem = Action | Skip


@dataclass(slots=True)
class TransformResult:
    """What :meth:`Transformer.transform` reports about one executed action."""

    #: Things the user should know (quality caveats, partial application ...).
    notes: list[str] = field(default_factory=list)
    #: JSON-serializable facts for the build report.
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Everything a transformer may consult while planning."""

    game: GameInput
    profile: Profile
    tools: Toolbox


class Transformer(ABC):
    """Base class of every transformer."""

    #: Short unique identifier (``"texture"``, ``"config"``, ``"media"``, ``"binary"``).
    name: ClassVar[str]
    #: Bump whenever the output for the same inputs would change: it invalidates caches.
    version: ClassVar[int] = 1

    @abstractmethod
    def supports(self, path: str) -> bool:
        """Tell whether this transformer handles files like the logical ``path``.

        This is a cheap, name-based test (extension, tree); it does not open the file and
        does not consult the profile.
        """

    @abstractmethod
    def plan(self, context: PlanContext) -> Iterable[PlanItem]:
        """Yield an :class:`Action` or a :class:`Skip` for every file this transformer cares about.

        Files that need no change (already within limits, not selected by any rule) yield
        nothing. Planning must not write anything and must not raise for a bad file: a
        bad file is a :class:`Skip` of kind :attr:`SkipKind.INVALID`.
        """

    @abstractmethod
    def transform(self, src: Path, dst: Path, config: Mapping[str, Any]) -> TransformResult:
        """Execute one action: read ``src``, write the result to ``dst``.

        ``config`` is the action's ``params``. ``dst`` is a temporary path that the
        pipeline moves into place only when this method returns; its parent directory
        exists and its extension equals that of the final output.

        Raises:
            BeaError: the action cannot be completed (the pipeline records the failure
                and continues with the other actions).
        """
