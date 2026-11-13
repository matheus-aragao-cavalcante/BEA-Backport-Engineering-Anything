"""Path globbing for profile rules.

Profiles select files with gitignore-flavoured globs evaluated against RomFS-relative
POSIX paths:

* ``*`` matches any run of characters inside one path segment; ``?`` matches one;
  ``[abc]``, ``[a-z]`` and ``[!abc]`` match one character from a set (never ``/``);
* a ``**`` segment matches any number of directories, including none
  (``a/**/b`` matches ``a/b`` and ``a/x/y/b``; ``a/**`` matches everything below ``a``);
* a pattern without ``/`` is matched against the file name at any depth
  (``*.png`` matches ``ui/icons/ok.png``); a pattern with ``/`` is anchored at the root;
* a pattern ending in ``/`` selects everything below that directory (``ui/`` is ``ui/**``);
* matching ignores case, because profiles are written by hand against dumps that may
  have been extracted on case-insensitive filesystems.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

__all__ = ["PathMatcher", "glob_match", "glob_to_regex"]


def _class_body(body: str) -> str:
    """Translate the inside of ``[...]``: ``-`` stays a range, everything else is literal.

    Escaping every other character also covers the doubled ``&&``, ``||`` and ``~~``
    that the regular-expression engine reserves for future set operations.
    """
    return "".join(char if char == "-" else re.escape(char) for char in body if char != "/")


def _translate_segment(segment: str) -> str:
    """Translate one path segment (no ``/``) into a regex fragment."""
    out: list[str] = []
    index = 0
    length = len(segment)
    while index < length:
        char = segment[index]
        index += 1
        if char == "*":
            # A run of stars inside a segment behaves like a single star.
            while index < length and segment[index] == "*":
                index += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = index
            if end < length and segment[end] in "!^":
                end += 1
            if end < length and segment[end] == "]":
                end += 1
            while end < length and segment[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))  # no closing bracket: a literal "["
                continue
            body = segment[index:end]
            index = end + 1
            negate = body[:1] in ("!", "^")
            if negate:
                body = body[1:]
            body = _class_body(body)
            if not body:
                out.append("(?!)" if not negate else "[^/]")
                continue
            out.append(f"(?!/)[{'^' if negate else ''}{body}]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def glob_to_regex(pattern: str) -> str:
    """Translate a profile glob into the source of an anchored regular expression.

    Raises:
        ValueError: the pattern is empty.
    """
    text = pattern.strip().replace("\\", "/")
    # "./name" and "/name" are anchored at the root, like any pattern holding a "/".
    rooted = text.startswith(("./", "/"))
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/") and text.strip("/"):
        text += "**"  # "dir/" selects everything below that directory
    anchored = rooted or "/" in text.rstrip("/")
    text = text.strip("/")
    if not text:
        raise ValueError(f"empty glob pattern: {pattern!r}")
    segments = [segment for segment in text.split("/") if segment]
    if not anchored:
        segments = ["**", *segments]

    parts: list[str] = []
    last = len(segments) - 1
    for position, segment in enumerate(segments):
        if segment == "**":
            if position == last:
                # Trailing "**": everything below (at least one more segment).
                parts.append(".+")
            else:
                # Zero or more whole directories.
                parts.append("(?:[^/]+/)*")
            continue
        parts.append(_translate_segment(segment))
        if position != last:
            parts.append("/")
    # \A and \Z rather than ^ and $: the expression is anchored whatever function uses
    # it, and a name that ends in a line break is another name.
    return r"\A(?:" + "".join(parts) + r")\Z"


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(glob_to_regex(pattern), re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        raise ValueError(f"invalid glob pattern {pattern!r}: {exc}") from exc


def glob_match(pattern: str, path: str) -> bool:
    """Tell whether the RomFS-relative POSIX ``path`` matches ``pattern``."""
    return _compile(pattern).fullmatch(path) is not None


class PathMatcher:
    """A set of include globs minus a set of exclude globs.

    An empty include list matches nothing; excludes always win.
    """

    __slots__ = ("_exclude", "_include", "exclude", "include")

    def __init__(self, include: str | Iterable[str], exclude: str | Iterable[str] = ()) -> None:
        # A single string is one glob, never an iterable of one-character globs.
        self.include: tuple[str, ...] = (include,) if isinstance(include, str) else tuple(include)
        self.exclude: tuple[str, ...] = (exclude,) if isinstance(exclude, str) else tuple(exclude)
        self._include = tuple(_compile(pattern) for pattern in self.include)
        self._exclude = tuple(_compile(pattern) for pattern in self.exclude)

    def matches(self, path: str) -> bool:
        """Tell whether ``path`` is selected."""
        if not any(regex.fullmatch(path) for regex in self._include):
            return False
        return not any(regex.fullmatch(path) for regex in self._exclude)

    def __repr__(self) -> str:
        return f"PathMatcher(include={self.include!r}, exclude={self.exclude!r})"
