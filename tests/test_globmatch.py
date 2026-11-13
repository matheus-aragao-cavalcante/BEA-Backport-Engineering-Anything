"""Tests for bea.globmatch: gitignore-flavoured globs over RomFS-relative paths."""

from __future__ import annotations

import re
import warnings

import pytest

from bea.globmatch import PathMatcher, glob_match, glob_to_regex


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # A pattern without "/" matches the file name at any depth.
        ("*.png", "a.png", True),
        ("*.png", "ui/icons/ok.png", True),
        ("*.png", "ui/icons/ok.png.bak", False),
        ("*.png", "ui/png", False),
        ("logo.dds", "x/y/logo.dds", True),
        ("logo.dds", "x/y/biglogo.dds", False),
        # A pattern with "/" is anchored at the root.
        ("ui/*.png", "ui/a.png", True),
        ("ui/*.png", "ui/sub/a.png", False),
        ("ui/*.png", "x/ui/a.png", False),
        ("/top.png", "top.png", True),
        ("/top.png", "sub/top.png", False),
        ("./ui/a.png", "ui/a.png", True),
        # "*" and "?" never cross a directory separator.
        ("ui/*", "ui/a/b", False),
        ("ui/?.png", "ui/a.png", True),
        ("ui/?.png", "ui/ab.png", False),
        ("ui/?.png", "ui//.png", False),
        ("a*b", "a/b", False),
        ("ui/tex_*_d.dds", "ui/tex_rock_d.dds", True),
        # "**" matches zero or more directories.
        ("**/*.png", "a.png", True),
        ("**/*.png", "x/y/z/a.png", True),
        ("ui/**/*.png", "ui/a.png", True),
        ("ui/**/*.png", "ui/x/y/a.png", True),
        ("ui/**/*.png", "gui/a.png", False),
        ("ui/**", "ui/a.png", True),
        ("ui/**", "ui/x/y/a.png", True),
        ("ui/**", "ui", False),
        ("ui/**", "uix/a.png", False),
        ("ui/", "ui/x/a.png", True),
        ("ui/", "other/ui/a.png", False),
        ("**", "anything/at/all.bin", True),
        ("**/movies/**", "data/movies/intro.mp4", True),
        ("**/movies/**", "movies/intro.mp4", True),
        ("**/movies/**", "data/movies", False),
        # Character classes.
        ("tex_[0-9].png", "tex_7.png", True),
        ("tex_[0-9].png", "tex_a.png", False),
        ("tex_[!0-9].png", "tex_a.png", True),
        ("tex_[!0-9].png", "tex_7.png", False),
        ("tex_[^ab].png", "tex_c.png", True),
        ("tex_[^ab].png", "tex_a.png", False),
        ("a[!x]b", "a/b", False),
        ("x[]]y", "x]y", True),
        ("x[y", "x[y", True),
        # Case is ignored.
        ("*.PNG", "ui/a.png", True),
        ("UI/**/*.png", "ui/Sub/A.PNG", True),
        # Regex metacharacters in names are literal.
        ("a+b(1).png", "a+b(1).png", True),
        ("a+b(1).png", "aab(1).png", False),
        ("file.name.png", "fileXname.png", False),
        ("$weird^{name}.txt", "$weird^{name}.txt", True),
        # Windows separators in a pattern are accepted.
        ("ui\\*.png", "ui/a.png", True),
        ("ui\\sub\\*.png", "ui/sub/a.png", True),
        # "./" names the root explicitly: such a pattern has a "/" and stays anchored.
        ("./top.png", "top.png", True),
        ("./top.png", "sub/top.png", False),
        ("./*.png", "a.png", True),
        ("./*.png", "x/y/a.png", False),
        ("././top.png", "sub/top.png", False),
        (".\\top.png", "top.png", True),
        (".\\top.png", "sub/top.png", False),
        ("./ui/", "ui/x/a.png", True),
        ("./ui/", "other/ui/a.png", False),
        # A trailing "/" selects everything below that directory, which is anchored.
        ("ui/icons/", "ui/icons/a/b.png", True),
        ("ui/icons/", "ui/icons", False),
        ("ui/icons/", "ui/a.png", False),
        ("ui/**/", "ui/a.png", True),
        ("*/", "top/a.png", True),
        ("*/", "a.png", False),
        # "**" stands for whole directories only.
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/xb", False),
        ("a/**/b", "ab", False),
        ("a/**/b", "a/b/c", False),
        ("a/**/b", "x/a/b", False),
        ("**/b", "b", True),
        ("**/b", "x/y/b", True),
        ("**/b", "xb", False),
        ("a/**/**/b", "a/b", True),
        ("a/**/**/b", "a/x/b", True),
        ("**/**", "a", True),
        ("**/*", "a/b/c", True),
        # Inside a segment a run of stars is one star.
        ("a**b", "axxb", True),
        ("a**b", "ab", True),
        ("a**b", "a/b", False),
        ("**.png", "x/a.png", True),
        ("ui/**.png", "ui/a.png", True),
        ("ui/**.png", "ui/x/a.png", False),
        # "*" may match nothing, "?" always takes one character.
        ("a*.png", "a.png", True),
        ("a?.png", "a.png", False),
        ("*", "x/y/z", True),
        ("?", "a", True),
        ("?", "x/a", True),
        ("?", "ab", False),
        ("???", "abc", True),
        ("ui/*/*.png", "ui/a/b.png", True),
        ("ui/*/*.png", "ui/b.png", False),
        ("ui/*/*.png", "ui/a/b/c.png", False),
        # More character classes.
        ("[abc].png", "b.png", True),
        ("[abc].png", "d.png", False),
        ("[abc].png", "ab.png", False),
        ("[A-C]x", "bx", True),
        ("[!A-C]x", "bx", False),
        ("[!a-c]x", "dx", True),
        ("[!]]x", "ax", True),
        ("[!]]x", "]x", False),
        ("[]a]x", "]x", True),
        ("[]a]x", "ax", True),
        ("[*?]", "*", True),
        ("[*?]", "?", True),
        ("[*?]", "a", False),
        ("[.]", ".", True),
        ("[.]", "a", False),
        ("a[-]b", "a-b", True),
        ("a[a-]b", "a-b", True),
        ("a[-a]b", "a-b", True),
        ("a[a-]b", "aXb", False),
        ("a[[]b", "a[b", True),
        ("a[^^]b", "a^b", False),
        ("a[^^]b", "acb", True),
        ("a[!^]b", "a^b", False),
        ("a[x^]b", "a^b", True),
        # Brackets that do not form a class are literal.
        ("x[]y", "x[]y", True),
        ("x[!]y", "x[!]y", True),
        ("x[^]y", "x[^]y", True),
        ("x]y", "x]y", True),
        ("a[b/c]d", "a[b/c]d", True),  # a class never spans a "/"
        ("a[b/c]d", "abd", False),
        # More literal metacharacters.
        ("a.b", "aXb", False),
        ("a(b)|c", "a(b)|c", True),
        ("a(b)|c", "c", False),
        ("a{1,2}", "a{1,2}", True),
        ("a{1,2}", "aa", False),
        ("#!~@%&=,;'.txt", "#!~@%&=,;'.txt", True),
        # Surrounding white space is not part of the pattern; inner spaces are.
        ("  ui/*.png  ", "ui/a.png", True),
        ("my file*.bin", "x/My File 01.BIN", True),
        ("my file*.bin", "x/myfile01.bin", False),
        # Repeated separators in a pattern count once.
        ("ui//a.png", "ui/a.png", True),
        # Case is ignored beyond ASCII too.
        ("README", "docs/readme", True),
        ("ÉTÉ/*.png", "été/a.png", True),
        ("あ*.bin", "x/あい.bin", True),
        # A line break in a name is just another character.
        ("a/**", "a/b\nc", True),
        ("*.png", "a\nb.png", True),
        ("a?b", "a\nb", True),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


@pytest.mark.parametrize("pattern", ["", "   ", "/", "//", "./", ".//", "\\", "\t\n"])
def test_empty_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        glob_to_regex(pattern)
    with pytest.raises(ValueError):
        glob_match(pattern, "a.png")


@pytest.mark.parametrize("pattern", ["tex_[z-a].png", "[9-0]", "ui/[b-a]*/x"])
def test_invalid_character_range_is_reported_as_value_error(pattern: str) -> None:
    with pytest.raises(ValueError, match="invalid glob pattern"):
        glob_match(pattern, "tex_b.png")
    # The pattern is named, so that a profile author can find it.
    with pytest.raises(ValueError, match=re.escape(repr(pattern))):
        PathMatcher([pattern])


@pytest.mark.parametrize(
    ("pattern", "path"),
    [("x[a&&b]y", "x&y"), ("x[a||b]y", "x|y"), ("x[a~~b]y", "x~y"), ("x[&|~][~|&]y", "x|&y")],
)
def test_doubled_punctuation_in_a_class_is_literal(pattern: str, path: str) -> None:
    # The regular-expression engine reserves "&&", "||" and "~~" inside a set for future
    # set operations and warns about them: a glob must neither warn nor change meaning.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert glob_match(pattern, path)
        assert not glob_match(pattern, "xcy")


@pytest.mark.parametrize(
    ("pattern", "inside", "around"),
    [
        ("*.png", "a.png", ["a.png.bak", "a.pngx", "a.png/x"]),
        ("b.png", "b.png", ["ab.png", "b.pngx", "xb.png"]),
        ("ui/*.png", "ui/a.png", ["x/ui/a.png", "ui/a.png/x", "gui/a.png"]),
        ("ui/**", "ui/a", ["xui/a", "x/ui/a"]),
    ],
)
def test_glob_to_regex_is_anchored_at_both_ends(
    pattern: str, inside: str, around: list[str]
) -> None:
    # "The source of an anchored regular expression": it must be safe with any of the
    # matching functions, not only with fullmatch().
    source = glob_to_regex(pattern)
    for function in (re.fullmatch, re.match, re.search):
        assert function(source, inside) is not None
        for path in around:
            assert function(source, path) is None, (function.__name__, path)


def test_a_trailing_line_break_is_part_of_the_name() -> None:
    # "$" would let a final "\n" through; a name that ends with one is another name.
    source = glob_to_regex("*.png")
    assert re.search(source, "a.png\n") is None
    assert re.fullmatch(source, "a.png\n") is None
    assert not glob_match("*.png", "a.png\n")
    assert glob_match("*.png\n", "a.png")  # white space around a *pattern* is trimmed


def test_glob_to_regex_leaves_case_to_the_caller() -> None:
    source = glob_to_regex("UI/*.PNG")
    assert re.fullmatch(source, "UI/a.PNG") is not None
    assert re.fullmatch(source, "ui/a.png") is None
    assert re.fullmatch(source, "ui/a.png", re.IGNORECASE) is not None


def test_path_matcher_combines_include_and_exclude() -> None:
    matcher = PathMatcher(["**/*.png", "textures/**"], ["ui/fonts/**", "*_lut.png"])
    assert matcher.matches("textures/rock.dds")
    assert matcher.matches("ui/icons/ok.png")
    assert not matcher.matches("ui/fonts/glyphs.png")
    assert not matcher.matches("fx/grade_lut.png")
    assert not matcher.matches("audio/a.wav")
    assert matcher.include == ("**/*.png", "textures/**")
    assert matcher.exclude == ("ui/fonts/**", "*_lut.png")
    assert "PathMatcher" in repr(matcher)


def test_path_matcher_without_includes_matches_nothing() -> None:
    assert not PathMatcher([]).matches("a.png")
    assert not PathMatcher([], ["*.png"]).matches("a.png")


def test_path_matcher_reports_bad_patterns_at_construction() -> None:
    with pytest.raises(ValueError):
        PathMatcher(["ok/*.png", ""])
    with pytest.raises(ValueError):
        PathMatcher(["ok/*.png"], ["/"])
    with pytest.raises(ValueError, match="invalid glob pattern"):
        PathMatcher(["ok/*.png"], ["tex_[z-a].png"])


def test_path_matcher_excludes_always_win() -> None:
    # Even when the very same pattern includes the file.
    assert not PathMatcher(["**"], ["**"]).matches("a/b.png")
    assert not PathMatcher(["ui/a.png"], ["ui/a.png"]).matches("ui/a.png")
    matcher = PathMatcher(["ui/**"], ["ui/skip/**", "*.tmp"])
    assert matcher.matches("ui/keep/a.png")
    assert not matcher.matches("ui/skip/a.png")
    assert not matcher.matches("ui/keep/a.tmp")
    assert not matcher.matches("UI/SKIP/A.PNG")  # excludes ignore case as well


def test_path_matcher_takes_any_iterable_of_patterns() -> None:
    patterns = ["ui/*.png", "fx/*.png"]
    for include in (patterns, tuple(patterns), iter(patterns), (p for p in patterns)):
        matcher = PathMatcher(include, exclude=iter(["fx/b.png"]))
        assert matcher.include == ("ui/*.png", "fx/*.png")
        assert matcher.exclude == ("fx/b.png",)
        assert matcher.matches("fx/a.png") and not matcher.matches("fx/b.png")
        # A one-shot iterator was consumed once: the matcher keeps working.
        assert matcher.matches("ui/a.png") and matcher.matches("ui/a.png")
    # A matcher built from a list does not follow later changes to that list.
    patterns.append("**")
    assert not PathMatcher(patterns[:2]).matches("other/a.png")


def test_path_matcher_takes_a_single_glob_as_one_pattern() -> None:
    # A string is one glob, not an iterable of one-character globs: "*.png" taken
    # letter by letter would start with "*", which selects every file.
    matcher = PathMatcher("ui/*.png", "ui/x*.png")
    assert matcher.include == ("ui/*.png",)
    assert matcher.exclude == ("ui/x*.png",)
    assert matcher.matches("ui/a.png")
    assert not matcher.matches("ui/xa.png")
    assert not PathMatcher("*.png").matches("audio/a.wav")
    assert PathMatcher("*.png").matches("audio/a.png")
    with pytest.raises(ValueError):
        PathMatcher("")


def test_path_matcher_repr_shows_both_lists() -> None:
    matcher = PathMatcher(["a/**", "*.png"], ["b"])
    assert repr(matcher) == "PathMatcher(include=('a/**', '*.png'), exclude=('b',))"
    assert repr(PathMatcher([])) == "PathMatcher(include=(), exclude=())"


def test_many_patterns_do_not_confuse_each_other() -> None:
    # More distinct patterns than any cache of compiled globs is likely to hold.
    for number in range(1500):
        assert glob_match(f"dir{number}/*.bin", f"dir{number}/a.bin")
        assert not glob_match(f"dir{number}/*.bin", f"dir{number + 1}/a.bin")
    assert glob_match("dir0/*.bin", "dir0/a.bin")
