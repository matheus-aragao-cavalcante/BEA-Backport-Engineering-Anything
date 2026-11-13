"""Error types that BEA reports to the user.

Every exception that is *expected* to reach the command line derives from
:class:`BeaError`; its message is written for the person running the tool and never
needs a traceback. Anything else that escapes is a bug.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "BeaError",
    "FormatError",
    "InputError",
    "MismatchError",
    "OutputError",
    "PatchError",
    "ProfileError",
    "ToolError",
    "UnsupportedInputError",
]


class BeaError(Exception):
    """Base class for user-facing errors."""

    #: Process exit status used by the CLI when this error aborts a command.
    exit_code = 2


class InputError(BeaError):
    """The input folder/file is missing, malformed, or not something BEA can read."""


class UnsupportedInputError(InputError):
    """The input was recognized but this version of BEA cannot process it."""


class FormatError(BeaError):
    """A binary file does not follow its documented format."""


class ProfileError(BeaError):
    """A profile (``profile.yaml`` / ``patches.yaml``) is invalid.

    ``problems`` lists every individual finding (``"<key path>: <what is wrong>"``) so
    that a validator can show them all at once; the message is their concatenation.
    """

    def __init__(self, message: str, *, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems: tuple[str, ...] = tuple(problems)


class MismatchError(BeaError):
    """The profile does not fit the game it is applied to.

    Raised when something the profile takes for granted is not there: a configuration
    key, a number of text matches, the bytes at a patch site. BEA never guesses in that
    situation; it refuses and says what it expected and what it found.
    """


class PatchError(MismatchError):
    """A binary patch cannot be applied safely (mismatching bytes, bad range, overlap)."""


class ToolError(BeaError):
    """An external tool (for example ffmpeg) is missing or failed."""


class OutputError(BeaError):
    """BEA refuses to write or clean an output location."""
