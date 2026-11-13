"""Tests for bea.transformers.config: declarative edits to INI, JSON and plain-text files.

Every document is written out by hand inside its test and every expectation is the exact
byte string the edit must produce. Edits normally go through ``edited``, which also
checks that applying the same edits to the result changes nothing (idempotence).
"""

from __future__ import annotations

import codecs
import copy
import dataclasses
import json
import sys
from collections.abc import Buffer, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from bea.errors import BeaError, InputError, MismatchError, OutputError
from bea.model import FileEntry, GameInput, TitleInfo
from bea.profile.schema import (
    ConfigFormat,
    ConfigRule,
    IfMissing,
    IniEdit,
    JsonEdit,
    Profile,
    TextEdit,
)
from bea.toolbox import Toolbox
from bea.transformers.base import Action, PlanContext, PlanItem, Skip, SkipKind, TransformResult
from bea.transformers.config import ConfigTransformer, apply_config_edits, edits_to_params

# ----------------------------------------------------------------------------- helpers


def ini(section: str | None, key: str, value: str, if_missing: str = "error") -> dict[str, Any]:
    """One INI edit in the form transformers receive it."""
    return {"section": section, "key": key, "value": value, "if_missing": if_missing}


def at(path: Sequence[str | int], value: Any, if_missing: str = "error") -> dict[str, Any]:
    """One JSON edit in the form transformers receive it."""
    return {"path": list(path), "value": value, "if_missing": if_missing}


def sub(find: str, replace: str, count: int = 1) -> dict[str, Any]:
    """One text edit in the form transformers receive it."""
    return {"find": find, "replace": replace, "count": count}


def edited(
    data: bytes, fmt: str, edits: Sequence[Mapping[str, Any]], encoding: str | None = None
) -> bytes:
    """Apply ``edits`` and check that applying them again to the result changes nothing."""
    result = apply_config_edits(data, fmt, edits, encoding=encoding)
    assert apply_config_edits(result, fmt, edits, encoding=encoding) == result
    return result


# ------------------------------------------------- text handling common to all formats


def test_utf8_bom_is_kept_and_not_seen_by_the_edits() -> None:
    data = b"\xef\xbb\xbf[Video]\r\nScale=100\r\n"
    # Without stripping the mark the first line would not read as the header "[Video]".
    assert edited(data, "ini", [ini("Video", "Scale", "70")]) == (
        b"\xef\xbb\xbf[Video]\r\nScale=70\r\n"
    )


def test_utf16_le_with_bom() -> None:
    data = b"\xff\xfeK\x00=\x001\x00\r\x00\n\x00"
    # U+00E9 is E9 00 in UTF-16 LE.
    assert edited(data, "ini", [ini(None, "K", "é2")]) == (
        b"\xff\xfeK\x00=\x00\xe9\x002\x00\r\x00\n\x00"
    )


def test_utf16_be_with_bom() -> None:
    data = b"\xfe\xff\x00K\x00=\x001\x00\n"
    assert edited(data, "ini", [ini(None, "K", "2")]) == b"\xfe\xff\x00K\x00=\x002\x00\n"


def test_utf32_le_bom_is_not_mistaken_for_utf16_le() -> None:
    # FF FE 00 00 starts with the UTF-16 LE mark FF FE. Read as UTF-16 the key would be
    # "\0K\0" and the edit would not find it.
    data = b"\xff\xfe\x00\x00K\x00\x00\x00=\x00\x00\x001\x00\x00\x00"
    assert edited(data, "ini", [ini(None, "K", "2")]) == (
        b"\xff\xfe\x00\x00K\x00\x00\x00=\x00\x00\x002\x00\x00\x00"
    )


def test_utf32_be_with_bom() -> None:
    data = b"\x00\x00\xfe\xff\x00\x00\x00K\x00\x00\x00=\x00\x00\x001"
    assert edited(data, "ini", [ini(None, "K", "2")]) == (
        b"\x00\x00\xfe\xff\x00\x00\x00K\x00\x00\x00=\x00\x00\x002"
    )


def test_utf8_is_assumed_without_bom_and_without_encoding() -> None:
    # "Título" with U+00ED as the two UTF-8 bytes C3 AD; the edit names the key in capitals.
    data = b"T\xc3\xadtulo=1\nOther=\xe6\x97\xa5\n"
    assert edited(data, "ini", [ini(None, "TÍTULO", "ü")]) == (
        b"T\xc3\xadtulo=\xc3\xbc\nOther=\xe6\x97\xa5\n"
    )


def test_explicit_encoding_is_used_when_there_is_no_bom() -> None:
    # cp1252: E9 is U+00E9, FC is U+00FC, E7 is U+00E7. None of this is valid UTF-8.
    data = b"Name=caf\xe9\nOther=\xfc\n"
    assert edited(data, "ini", [ini(None, "Name", "garçon")], encoding="cp1252") == (
        b"Name=gar\xe7on\nOther=\xfc\n"
    )
    # Shift-JIS: U+65E5 is 93 FA, U+672C is 96 7B.
    japanese = b"Lang=\x93\xfa\n"
    assert edited(japanese, "ini", [ini(None, "Lang", "日本")], encoding="shift_jis") == (
        b"Lang=\x93\xfa\x96\x7b\n"
    )


def test_bom_wins_over_the_explicit_encoding() -> None:
    # Read as cp1252 the first key would be "ï»¿Name" and the edit would fail.
    data = b"\xef\xbb\xbfName=\xc3\xa9\n"
    assert edited(data, "ini", [ini(None, "Name", "ü")], encoding="cp1252") == (
        b"\xef\xbb\xbfName=\xc3\xbc\n"
    )


@pytest.mark.parametrize(
    ("encoding", "data", "expected"),
    [
        ("utf-16", b"K\x00=\x001\x00", b"K\x00=\x002\x00"),
        ("UTF16", b"K\x00=\x001\x00", b"K\x00=\x002\x00"),
        (
            "utf-32",
            b"K\x00\x00\x00=\x00\x00\x001\x00\x00\x00",
            b"K\x00\x00\x00=\x00\x00\x002\x00\x00\x00",
        ),
        ("utf-8-sig", b"K=1", b"K=2"),
        ("utf_8_sig", b"K=1", b"K=2"),
    ],
)
def test_codecs_that_write_a_bom_never_add_one_to_a_file_without(
    encoding: str, data: bytes, expected: bytes
) -> None:
    assert edited(data, "ini", [ini(None, "K", "2")], encoding=encoding) == expected


def test_undecodable_bytes_suggest_setting_the_encoding() -> None:
    data = b"Name=caf\xe9\n"
    with pytest.raises(MismatchError, match=r"not valid UTF-8.*byte 0xE9 at offset 8.*`encoding`"):
        apply_config_edits(data, "ini", [ini(None, "Name", "x")])
    with pytest.raises(MismatchError, match=r"not valid shift_jis.*`encoding`"):
        apply_config_edits(b"Name=\x81\n", "text", [sub("x", "y")], encoding="shift_jis")


def test_damaged_file_with_bom_is_reported_with_its_file_offset() -> None:
    # A UTF-16 LE mark followed by an odd number of bytes: the last one starts at offset 6.
    data = b"\xff\xfeK\x00=\x001"
    with pytest.raises(MismatchError, match=r"UTF-16-LE.*byte 0x31 at offset 6"):
        apply_config_edits(data, "ini", [ini(None, "K", "2")])


@pytest.mark.parametrize("encoding", ["klingon-8", "", "hex", "rot13", "utf-8\x00"])
def test_unknown_or_non_text_encoding_is_reported(encoding: str) -> None:
    with pytest.raises(MismatchError, match="is not a text encoding BEA knows"):
        apply_config_edits(b"K=1\n", "ini", [ini(None, "K", "2")], encoding=encoding)


def test_codec_that_cannot_reproduce_the_file_is_refused() -> None:
    # unicode_escape decodes these bytes but writes a line break back as the two
    # characters "\n": every line of the file would be altered, not only the edited one.
    with pytest.raises(MismatchError, match="does not give the same bytes"):
        apply_config_edits(b"K=1\nL=2\n", "ini", [ini(None, "K", "2")], encoding="unicode_escape")


def test_edit_that_the_encoding_cannot_hold_is_refused() -> None:
    with pytest.raises(MismatchError, match=r"cannot be stored as ascii.*U\+00E9"):
        apply_config_edits(b"K=1\n", "ini", [ini(None, "K", "é")], encoding="ascii")
    with pytest.raises(MismatchError, match=r"cannot be stored as cp1252.*U\+65E5"):
        apply_config_edits(b"K=\xe9\n", "text", [sub("(?<=K=).*", "日")], encoding="cp1252")


def _picky_search(name: str) -> codecs.CodecInfo | None:
    """Codec search function for ``bea_test_picky``: Latin-1 that cannot encode "!"."""
    if name != "bea_test_picky":
        return None

    def encode(text: str, errors: str = "strict") -> tuple[bytes, int]:
        if "!" in text:
            # A bare UnicodeError: no position, no offending character.
            raise UnicodeError("this codec has no exclamation mark")
        return text.encode("latin-1", errors), len(text)

    def decode(data: Buffer, errors: str = "strict") -> tuple[str, int]:
        raw = bytes(data)
        return raw.decode("latin-1", errors), len(raw)

    return codecs.CodecInfo(encode, decode, name="bea_test_picky")


@pytest.fixture
def picky_codec() -> Iterator[str]:
    """Make the ``bea_test_picky`` codec known for the duration of one test."""
    codecs.register(_picky_search)
    try:
        yield "bea_test_picky"
    finally:
        codecs.unregister(_picky_search)


def test_codec_failures_of_any_kind_are_reported_as_mismatches(picky_codec: str) -> None:
    edits = [ini(None, "K", "2")]
    assert edited(b"K=1\nL=\xe9\n", "ini", edits, encoding=picky_codec) == b"K=2\nL=\xe9\n"
    # The edited text cannot be encoded, and the codec does not say where.
    with pytest.raises(
        MismatchError, match="cannot be stored as bea_test_picky: this codec has no exclamation"
    ):
        apply_config_edits(b"K=1\n", "ini", [ini(None, "K", "wow!")], encoding=picky_codec)
    # The file itself decodes but cannot be encoded again: it cannot be reproduced.
    with pytest.raises(MismatchError, match="does not give the same bytes"):
        apply_config_edits(b"K=!\n", "ini", edits, encoding=picky_codec)
    # A codec that fails to decode with a bare UnicodeError.
    with pytest.raises(MismatchError, match=r"cannot be read as undefined text.*`encoding`"):
        apply_config_edits(b"K=1\n", "ini", edits, encoding="undefined")


def test_line_endings_are_kept_line_by_line_and_additions_use_the_first_one() -> None:
    data = b"A=1\r\nB=2\nC=3\rD=4"
    edits = [ini(None, "B", "20"), ini(None, "D", "40")]
    assert edited(data, "ini", edits) == b"A=1\r\nB=20\nC=3\rD=40"
    # The added line is a complete line, ended like the first line of the file; the line
    # before it had no ending and gets the same one.
    edits.append(ini(None, "E", "5", "add"))
    assert edited(data, "ini", edits) == b"A=1\r\nB=20\nC=3\rD=40\r\nE=5\r\n"


@pytest.mark.parametrize(
    ("fmt", "data", "edits"),
    [
        ("ini", b"[S]\r\nA = 1\r\n", [ini("S", "A", "1"), ini("S", "Z", "9", "ignore")]),
        ("json", b'{ "a" : 1,\n\n   "b":[1,2 ] }\r\n', [at(["a"], 1), at(["b", 1], 2)]),
        ("text", b"a=1\r\n", [sub(r"^a=\d", "a=1")]),
    ],
)
def test_file_that_already_holds_the_values_comes_back_untouched(
    fmt: str, data: bytes, edits: list[dict[str, Any]]
) -> None:
    assert apply_config_edits(data, fmt, edits) is data


def test_no_edits_means_no_change() -> None:
    data = b"\xef\xbb\xbfanything = at all\r\n"
    assert apply_config_edits(data, "ini", []) is data
    assert apply_config_edits(data, "text", []) is data
    assert apply_config_edits(b'{"a":1}', ConfigFormat.JSON, []) == b'{"a":1}'


def test_unknown_format_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown config format 'yaml'"):
        apply_config_edits(b"a: 1\n", "yaml", [])


def test_the_edits_passed_in_are_never_modified() -> None:
    cases: list[tuple[bytes, str, list[dict[str, Any]]]] = [
        (b"[S]\nA=1\n", "ini", [ini("S", "A", "2"), ini("T", "B", "3", "add")]),
        (b'{"a": 1}', "json", [at(["b"], {"c": [1, 2]}, "add"), at(["b", "c", 0], 9)]),
        (b"a=1\n", "text", [sub("a=1", "a=2")]),
    ]
    for data, fmt, edits in cases:
        original = copy.deepcopy(edits)
        untouched = bytes(data)
        apply_config_edits(data, fmt, edits)
        assert edits == original
        assert data == untouched


# --------------------------------------------------------------------------------- ini


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"Key=old\n", b"Key=new\n"),
        (b"Key = old\n", b"Key = new\n"),
        (b"Key\t=\told\n", b"Key\t=\tnew\n"),
        # Indentation and the padding around "=" stay; what followed the old value goes.
        (b"  Key  =   old   \n", b"  Key  =   new\n"),
        (b"Key=\n", b"Key=new\n"),
        (b"Key=   \n", b"Key=   new\n"),
        # The value is everything after the FIRST "=", up to the end of the line.
        (b"Key=a=b=c\n", b"Key=new\n"),
        (b"Key=old ; trailing note\n", b"Key=new\n"),
        (b"Key=old", b"Key=new"),
        (b"Key=old\r\n", b"Key=new\r\n"),
        (b"Key=old\r", b"Key=new\r"),
    ],
)
def test_ini_set_replaces_the_value_and_nothing_else(before: bytes, after: bytes) -> None:
    assert edited(before, "ini", [ini(None, "Key", "new")]) == after


def test_ini_value_is_written_as_given() -> None:
    assert edited(b"[S]\nKey=1\n", "ini", [ini("S", "Key", "a=b; c  ")]) == b"[S]\nKey=a=b; c  \n"
    assert edited(b"[S]\nKey = 1\n", "ini", [ini("S", "Key", "")]) == b"[S]\nKey = \n"


def test_ini_names_compare_case_insensitively_and_keep_their_spelling() -> None:
    data = b"[RENDERING]\nresolutionscale=100\n"
    assert edited(data, "ini", [ini("Rendering", "ResolutionScale", "70")]) == (
        b"[RENDERING]\nresolutionscale=70\n"
    )
    # casefold, not lower: the sharp s equals "ss".
    german = "[Straße]\nGRÖSSE=1\n".encode()
    assert edited(german, "ini", [ini("STRASSE", "größe", "2")]) == "[Straße]\nGRÖSSE=2\n".encode()
    # White space around the names of an edit means nothing, as in the file.
    assert edited(b"[S]\nKey=1\n", "ini", [ini("  S ", " Key ", "2")]) == b"[S]\nKey=2\n"


def test_ini_keys_are_looked_up_in_their_own_section_only() -> None:
    data = b"Scale=1\n[A]\nScale=2\n[B]\nScale=3\n"
    assert (
        edited(data, "ini", [ini(None, "Scale", "10")]) == b"Scale=10\n[A]\nScale=2\n[B]\nScale=3\n"
    )
    assert (
        edited(data, "ini", [ini("B", "Scale", "30")]) == b"Scale=1\n[A]\nScale=2\n[B]\nScale=30\n"
    )
    with pytest.raises(MismatchError, match=r"edits\[0\] \(key 'Scale' in \[C\]\)"):
        apply_config_edits(data, "ini", [ini("C", "Scale", "1")])
    # A key that only exists in other sections is missing from this one.
    with pytest.raises(MismatchError, match="not defined in that section"):
        apply_config_edits(b"[A]\nK=1\n[B]\nL=2\n", "ini", [ini("B", "K", "1")])


def test_ini_comments_and_other_lines_are_never_keys() -> None:
    data = b"[S]\n;Key=1\n# Key=2\n  ; Key=3\nKey=4\njust some words\n"
    assert edited(data, "ini", [ini("S", "Key", "9")]) == (
        b"[S]\n;Key=1\n# Key=2\n  ; Key=3\nKey=9\njust some words\n"
    )
    with pytest.raises(MismatchError, match="not defined"):
        apply_config_edits(b"[S]\n;Key=1\n", "ini", [ini("S", "Key", "9")])


def test_ini_prefix_characters_make_a_different_key() -> None:
    data = b"[S]\n+Key=a\n-Key=b\nKey=c\n"
    assert edited(data, "ini", [ini("S", "Key", "z")]) == b"[S]\n+Key=a\n-Key=b\nKey=z\n"
    assert edited(data, "ini", [ini("S", "+Key", "z")]) == b"[S]\n+Key=z\n-Key=b\nKey=c\n"


def test_ini_section_headers() -> None:
    # Indented, padded inside the brackets and followed by text that holds another "]".
    data = b"  [ Video ]  ; see [Audio]\nA=1\n"
    assert edited(data, "ini", [ini("video", "A", "2")]) == b"  [ Video ]  ; see [Audio]\nA=2\n"
    # No closing bracket: not a header, so "B" still belongs to [S].
    data = b"[S]\nA=1\n[broken\nB=2\n"
    assert edited(data, "ini", [ini("S", "B", "3")]) == b"[S]\nA=1\n[broken\nB=3\n"
    # A header is a header even when it holds "=".
    data = b"[a=b]\nK=1\n"
    assert edited(data, "ini", [ini("a=b", "K", "2")]) == b"[a=b]\nK=2\n"
    with pytest.raises(MismatchError, match="not defined before the first section header"):
        apply_config_edits(data, "ini", [ini(None, "[a", "x")])


def test_ini_blocks_of_the_same_section_form_one_section() -> None:
    data = b"[S]\nA=1\n[T]\nX=0\n[S]\nB=2\n"
    assert edited(data, "ini", [ini("S", "A", "10")]) == b"[S]\nA=10\n[T]\nX=0\n[S]\nB=2\n"
    assert edited(data, "ini", [ini("s", "B", "20")]) == b"[S]\nA=1\n[T]\nX=0\n[S]\nB=20\n"
    # A new key goes to the LAST block of the section.
    assert edited(data, "ini", [ini("S", "C", "3", "add")]) == (
        b"[S]\nA=1\n[T]\nX=0\n[S]\nB=2\nC=3\n"
    )
    assert edited(
        b"[S]\nA=1\n\n[T]\nX=0\n\n[S]\nB=2\n\n[U]\n", "ini", [ini("S", "C", "3", "add")]
    ) == (b"[S]\nA=1\n\n[T]\nX=0\n\n[S]\nB=2\nC=3\n\n[U]\n")


@pytest.mark.parametrize("if_missing", ["error", "add", "ignore"])
def test_ini_key_defined_twice_in_its_section_is_ambiguous(if_missing: str) -> None:
    with pytest.raises(MismatchError, match=r"edits\[0\].*defined 2 times.*lines 2, 4"):
        apply_config_edits(
            b"[S]\nKey=1\nOther=0\nKEY=2\n", "ini", [ini("S", "Key", "3", if_missing)]
        )
    # Across two blocks of the same section too.
    split = b"[S]\nA=1\n[T]\nA=5\n[S]\nA=2\n"
    with pytest.raises(MismatchError, match=r"defined 2 times.*lines 2, 6"):
        apply_config_edits(split, "ini", [ini("S", "A", "3", if_missing)])
    # The same name in another section is no ambiguity.
    assert (
        edited(split, "ini", [ini("T", "A", "6", if_missing)]) == b"[S]\nA=1\n[T]\nA=6\n[S]\nA=2\n"
    )


def test_ini_missing_key_is_an_error_by_default_and_names_the_edit() -> None:
    edits = [ini("Video", "Scale", "2"), ini("Video", "Shadows", "Low")]
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(b"[Audio]\nVolume=3\n[Video]\nScale=1\nGamma=2\n", "ini", edits)
    message = str(caught.value)
    assert message.startswith("edits[1] (key 'Shadows' in [Video]): the key is not defined")
    assert "Scale, Gamma" in message  # what the section holds instead
    assert "if_missing" in message

    with pytest.raises(MismatchError) as caught:
        apply_config_edits(b"[Audio]\nVolume=3\n[Video]\n", "ini", [ini("Render", "Scale", "2")])
    message = str(caught.value)
    assert message.startswith(
        "edits[0] (key 'Scale' in [Render]): the file has no [Render] section"
    )
    assert "[Audio], [Video]" in message

    with pytest.raises(MismatchError, match=r"edits\[0\] \(key 'G' before the first section\)"):
        apply_config_edits(b"[Audio]\nG=3\n", "ini", [ini(None, "G", "1")])
    with pytest.raises(MismatchError, match="it has no section"):
        apply_config_edits(b"A=1\n", "ini", [ini("S", "A", "1")])
    with pytest.raises(MismatchError, match="no key is defined there"):
        apply_config_edits(b"[S]\n; nothing yet\n", "ini", [ini("S", "A", "1")])


@pytest.mark.parametrize(
    "data", [b"", b"[S]\nA=1\n", b"A=1\n\n[T]\nKey=5\n", b"[S]\r\n; Key=1\r\n"]
)
def test_ini_missing_key_or_section_can_be_ignored(data: bytes) -> None:
    edits = [ini("S", "Key", "9", "ignore"), ini(None, "Key", "9", "ignore")]
    assert apply_config_edits(data, "ini", edits) is data


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # After the last non-blank line: the blank line before the next section stays below.
        (b"[S]\nA=1\n\n[T]\nB=2\n", b"[S]\nA=1\nC=3\n\n[T]\nB=2\n"),
        (b"[S]\n\n[T]\n", b"[S]\nC=3\n\n[T]\n"),
        (b"[S]\nA=1\n; tail\n\n[T]\n", b"[S]\nA=1\n; tail\nC=3\n\n[T]\n"),
        (b"[S]\nA=1\n   \n\t\n[T]\n", b"[S]\nA=1\nC=3\n   \n\t\n[T]\n"),
        (b"[S]\nA=1\n\n\n", b"[S]\nA=1\nC=3\n\n\n"),
        (b"[T]\nB=2\n[S]\n", b"[T]\nB=2\n[S]\nC=3\n"),
        # A last line without ending gets one, and so does the new line.
        (b"[S]\nA=1", b"[S]\nA=1\nC=3\n"),
        (b"[S]", b"[S]\nC=3\n"),
        (b"[S]\r\nA=1\r\n\r\n[T]\r\n", b"[S]\r\nA=1\r\nC=3\r\n\r\n[T]\r\n"),
        (b"[S]\rA=1\r", b"[S]\rA=1\rC=3\r"),
    ],
)
def test_ini_add_puts_the_key_after_the_last_non_blank_line_of_its_section(
    before: bytes, after: bytes
) -> None:
    assert edited(before, "ini", [ini("S", "C", "3", "add")]) == after


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Nothing before the first header yet: the key goes to the very top of the file.
        (b"[S]\nA=1\n", b"G=1\n[S]\nA=1\n"),
        (b"", b"G=1\n"),
        (b"\n\n[S]\n", b"G=1\n\n\n[S]\n"),
        (b"\xef\xbb\xbf[S]\r\nA=1\r\n", b"\xef\xbb\xbfG=1\r\n[S]\r\nA=1\r\n"),
        # Otherwise after the last non-blank line of that region.
        (b"A=1\nB=2\n\n[S]\nC=3\n", b"A=1\nB=2\nG=1\n\n[S]\nC=3\n"),
        (b"; banner\n\n[S]\nA=1\n", b"; banner\nG=1\n\n[S]\nA=1\n"),
        (b"A=1\n", b"A=1\nG=1\n"),
        (b"A=1", b"A=1\nG=1\n"),
    ],
)
def test_ini_add_before_the_first_section(before: bytes, after: bytes) -> None:
    assert edited(before, "ini", [ini(None, "G", "1", "add")]) == after


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"[A]\nx=1\n", b"[A]\nx=1\n\n[B]\nk=v\n"),
        # The file is first made to end with a line ending.
        (b"[A]\nx=1", b"[A]\nx=1\n\n[B]\nk=v\n"),
        # An empty file gets no blank line.
        (b"", b"[B]\nk=v\n"),
        (b"\xef\xbb\xbf", b"\xef\xbb\xbf[B]\nk=v\n"),
        (b"[A]\r\nx=1\r\n", b"[A]\r\nx=1\r\n\r\n[B]\r\nk=v\r\n"),
        (b"[A]\rx=1", b"[A]\rx=1\r\r[B]\rk=v\r"),
        (b"x=1\n", b"x=1\n\n[B]\nk=v\n"),
        # A blank line is added even after one that is already there.
        (b"[A]\nx=1\n\n", b"[A]\nx=1\n\n\n[B]\nk=v\n"),
    ],
)
def test_ini_add_creates_a_missing_section_at_the_end_of_the_file(
    before: bytes, after: bytes
) -> None:
    assert edited(before, "ini", [ini("B", "k", "v", "add")]) == after


def test_ini_added_names_are_spelled_as_in_the_edit() -> None:
    assert edited(b"", "ini", [ini(" Graphics Options ", " Max FPS ", "30", "add")]) == (
        b"[Graphics Options]\nMax FPS=30\n"
    )


def test_ini_edits_apply_in_order_each_on_the_result_of_the_previous_one() -> None:
    edits = [
        ini("New", "K", "1", "add"),  # creates the section
        ini("new", "k", "2"),  # which the next edit then finds
        ini("New", "L", "3", "add"),
        ini("New", "M", "4", "add"),  # after L, not before it
        ini("S", "A", "x"),
        ini("S", "A", "y"),  # the last one wins
    ]
    assert edited(b"[S]\nA=1\n", "ini", edits) == b"[S]\nA=y\n\n[New]\nK=2\nL=3\nM=4\n"
    # One edit that cannot be applied fails the whole file, whatever came before.
    with pytest.raises(MismatchError, match=r"edits\[1\]"):
        apply_config_edits(b"[S]\nA=1\n", "ini", [ini("S", "A", "2"), ini("S", "B", "3")])


@pytest.mark.parametrize(
    ("section", "key", "value", "problem"),
    [
        ("S", "A", "1\nB=2", "line break"),
        ("S", "A", "1\r", "line break"),
        ("S", "A\nB", "1", "line break"),
        ("S\n[T]", "A", "1", "line break"),
        ("S", "A=B", "1", "would not be read back as the key 'A=B'"),
        ("S", ";A", "1", "would not be read back as the key ';A'"),
        ("S", "#A", "1", "would not be read back as the key '#A'"),
        ("S", "[A]", "1", r"would not be read back as the key '\[A\]'"),
        ("S", "[A", "x]", r"would not be read back as the key '\[A'"),
        ("S", "A", " 1", "starts with white space"),
        ("S", "A", "\t1", "starts with white space"),
        ("S]x", "A", "1", "cannot contain '\\]'"),
    ],
)
@pytest.mark.parametrize("if_missing", ["error", "add", "ignore"])
def test_ini_edit_that_could_not_be_read_back_is_refused(
    section: str, key: str, value: str, problem: str, if_missing: str
) -> None:
    # Whether or not the key exists: such a line would break the file or the next run.
    for data in (b"[S]\nA=0\n", b"[T]\nB=0\n"):
        with pytest.raises(MismatchError, match=problem):
            apply_config_edits(data, "ini", [ini(section, key, value, if_missing)])


def test_ini_unicode_file_with_crlf_and_no_final_newline() -> None:
    before = "; 設定\r\n[画面]\r\n解像度 = 1080\r\n\r\n[音]\r\n音量=5".encode()
    after = "; 設定\r\n[画面]\r\n解像度 = 720\r\n影=低\r\n\r\n[音]\r\n音量=5".encode()
    edits = [ini("画面", "解像度", "720"), ini("画面", "影", "低", "add")]
    assert edited(before, "ini", edits) == after


def test_ini_edit_fields_must_be_strings() -> None:
    with pytest.raises(TypeError, match="'value' must be a string"):
        apply_config_edits(b"A=1\n", "ini", [{"section": None, "key": "A", "value": 2}])
    with pytest.raises(ValueError, match="create"):
        apply_config_edits(b"A=1\n", "ini", [ini(None, "B", "2", "create")])


def test_ini_edit_defaults_mirror_the_profile_schema() -> None:
    # No "section" means before the first header; no "if_missing" means error.
    assert edited(b"A=1\n[S]\nA=5\n", "ini", [{"key": "A", "value": "2"}]) == b"A=2\n[S]\nA=5\n"
    with pytest.raises(MismatchError, match="not defined"):
        apply_config_edits(b"A=1\n", "ini", [{"key": "B", "value": "2"}])


# -------------------------------------------------------------------------------- json


def test_json_value_is_replaced_and_the_layout_kept() -> None:
    before = b'{\n  "video": {\n    "maxFps": 60,\n    "vsync": true\n  },\n  "name": "x"\n}\n'
    after = b'{\n  "video": {\n    "maxFps": 30,\n    "vsync": true\n  },\n  "name": "x"\n}\n'
    assert edited(before, "json", [at(["video", "maxFps"], 30)]) == after


@pytest.mark.parametrize("unit", ["  ", "    ", "\t", " ", "\t\t", ""])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final_newlines", [0, 1, 2])
def test_json_indentation_unit_line_endings_and_final_newline_are_kept(
    unit: str, newline: str, final_newlines: int
) -> None:
    lines = [
        "{",
        f'{unit}"a": @,',
        f'{unit}"b": [',
        f"{unit}{unit}1,",
        f"{unit}{unit}{{",
        f'{unit}{unit}{unit}"c": null',
        f"{unit}{unit}}}",
        f"{unit}]",
        "}",
    ]
    document = newline.join(lines) + newline * final_newlines
    before = document.replace("@", "1").encode()
    after = document.replace("@", "5").encode()
    assert edited(before, "json", [at(["a"], 5)]) == after


@pytest.mark.parametrize(
    ("before", "edit", "after"),
    [
        (b'{"a": 1, "b": [1, 2]}', at(["a"], 2), b'{"a": 2, "b": [1, 2]}'),
        (b'{"a":1,"b":[1,2]}', at(["a"], 2), b'{"a":2,"b":[1,2]}'),
        (b'{"a":1}\n', at(["a"], 2), b'{"a":2}\n'),
        (b'\n\n {"a": 1} \n\n', at(["a"], 2), b'\n\n {"a": 2} \n\n'),
        # Each separator keeps its own spacing.
        (b'{"a":1, "b":2}', at(["a"], 3), b'{"a":3, "b":2}'),
        (b'{"a": 1,"b": 2}', at(["a"], 3), b'{"a": 3,"b": 2}'),
        # A document that shows one separator only uses the same spacing for the other.
        (b'{"a":1}', at(["b"], 2, "add"), b'{"a":1,"b":2}'),
        (b'{"a": 1}', at(["b"], 2, "add"), b'{"a": 1, "b": 2}'),
        (b"[1,2]", at([0], {"k": 0}), b'[{"k":0},2]'),
        (b"[1, 2]", at([0], {"k": 0}), b'[{"k": 0}, 2]'),
        # One that shows none gets the usual spacing.
        (b"{}", at(["a", "b"], [1, 2], "add"), b'{"a": {"b": [1, 2]}}'),
        (b"[0]", at([0], {"a": [1, 2]}), b'[{"a": [1, 2]}]'),
        # Separators inside strings do not count, even behind an escaped quote.
        (b'{"k, x: y":1,"b":2}', at(["b"], 3), b'{"k, x: y":1,"b":3}'),
        (b'{"q\\": , ":1,"b":2}', at(["b"], 3), b'{"q\\": , ":1,"b":3}'),
        (b'{"k,x:y": 1, "b": 2}', at(["b"], 3), b'{"k,x:y": 1, "b": 3}'),
    ],
)
def test_json_one_line_document_stays_on_one_line_with_its_separators(
    before: bytes, edit: dict[str, Any], after: bytes
) -> None:
    assert edited(before, "json", [edit]) == after


def test_json_rewrite_uses_the_first_indented_line_and_the_first_line_ending() -> None:
    # Oddly laid out, but multi-line: the unit is the three spaces of the first indented
    # line, lines end like the first one does, and the white space around the document
    # (here a final CRLF) is kept as it was.
    before = b'{ "a" : 1,\n\n   "b":[1,2 ] }\r\n'
    after = b'{\n   "a": 2,\n   "b": [\n      1,\n      2\n   ]\n}\r\n'
    assert edited(before, "json", [at(["a"], 2)]) == after


def test_json_key_order_is_kept_and_new_keys_go_last() -> None:
    before = b'{"zeta": 1, "alpha": {"y": 1, "x": 2}, "mid": 3}'
    edits = [at(["alpha", "x"], 20), at(["alpha", "new"], 0, "add"), at(["zeta"], 10)]
    after = b'{"zeta": 10, "alpha": {"y": 1, "x": 20, "new": 0}, "mid": 3}'
    assert edited(before, "json", edits) == after


def test_json_non_ascii_stays_raw_when_the_file_has_some() -> None:
    # "José" and "São" with their accented letters as UTF-8 bytes.
    before = b'{"name": "Jos\xc3\xa9", "city": "S\xc3\xa3o"}'
    after = b'{"name": "Zo\xc3\xab", "city": "S\xc3\xa3o"}'
    assert edited(before, "json", [at(["name"], "Zoë")]) == after


def test_json_pure_ascii_file_stays_pure_ascii() -> None:
    before = b'{"name": "Jos\\u00e9", "n": 1}'
    assert edited(before, "json", [at(["n"], 2)]) == b'{"name": "Jos\\u00e9", "n": 2}'
    assert edited(before, "json", [at(["name"], "Zoë \U0001f600")]) == (
        b'{"name": "Zo\\u00eb \\ud83d\\ude00", "n": 1}'
    )


def test_json_writes_characters_the_encoding_lacks_as_escapes() -> None:
    # cp1252 holds U+00E9 (the byte E9) but neither U+65E5 nor U+1F600. JSON can always
    # spell those as escapes, so the edit is not refused, and what the file can hold
    # stays raw as before.
    before = b'{"a": "\xe9", "b": 1}'
    after = b'{"a": "\\u65e5 \xe9 \\ud83d\\ude00", "b": 1}'
    assert edited(before, "json", [at(["a"], "日 é \U0001f600")], encoding="cp1252") == after
    # A lone surrogate the file spells as an escape goes back as that escape, although
    # the file writes its other non-ASCII characters raw.
    before = b'{"a": "\\ud800", "b": 1, "c": "\xc3\xa9"}'
    after = b'{"a": "\\ud800", "b": 2, "c": "\xc3\xa9"}'
    assert edited(before, "json", [at(["b"], 2)]) == after


def test_json_with_bom_and_in_utf16() -> None:
    assert edited(b'\xef\xbb\xbf{"a": 1}\n', "json", [at(["a"], 2)]) == b'\xef\xbb\xbf{"a": 2}\n'
    before = b"\xff\xfe" + '{\r\n\t"a": "é",\r\n\t"b": 1\r\n}'.encode("utf-16-le")
    after = b"\xff\xfe" + '{\r\n\t"a": "é",\r\n\t"b": 2\r\n}'.encode("utf-16-le")
    assert edited(before, "json", [at(["b"], 2)]) == after


def test_json_arrays_are_indexed_by_integers() -> None:
    before = b'{"modes": [{"w": 1920}, {"w": 1280}]}'
    assert edited(before, "json", [at(["modes", 1, "w"], 960)]) == (
        b'{"modes": [{"w": 1920}, {"w": 960}]}'
    )
    assert edited(b"[1, 2, 3]", "json", [at([0], 9), at([2], [])]) == b"[9, 2, []]"
    assert edited(b"[[1, 2], [3, 4]]", "json", [at([1, 0], None)]) == b"[[1, 2], [null, 4]]"


def test_json_index_out_of_range() -> None:
    data = b'{"list": [1, 2, 3]}'
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(data, "json", [at(["list", 3], 0)])
    assert str(caught.value).startswith(
        'edits[0] (path ["list", 3]): the value at ["list"] is an array of 3, so it has no index 3'
    )
    assert apply_config_edits(data, "json", [at(["list", 3], 0, "ignore")]) is data
    assert apply_config_edits(data, "json", [at(["list", 7, "x"], 0, "ignore")]) is data
    # "add" creates keys, never array elements.
    with pytest.raises(MismatchError, match="never extends an array"):
        apply_config_edits(data, "json", [at(["list", 3], 0, "add")])
    with pytest.raises(MismatchError, match="never extends an array"):
        apply_config_edits(b"[]", "json", [at([0], 0, "add")])


@pytest.mark.parametrize("if_missing", ["error", "add", "ignore"])
@pytest.mark.parametrize(
    ("data", "path", "problem"),
    [
        (
            b'{"a": [1]}',
            ["a", "x"],
            r'the value at \["a"\] is an array, but the key "x" needs an object',
        ),
        (
            b'{"a": {"x": 1}}',
            ["a", 0],
            r'the value at \["a"\] is an object, but the index 0 needs an array',
        ),
        (b'{"a": 5}', ["a", "b"], r'the value at \["a"\] is a number, but the key "b"'),
        (b'{"a": "5"}', ["a", 0], r'the value at \["a"\] is a string, but the index 0'),
        (b'{"a": null}', ["a", "b"], r'the value at \["a"\] is null, but the key "b"'),
        (b'{"a": true}', ["a", "b", "c"], r'the value at \["a"\] is a boolean, but the key "b"'),
        (b"42", ["a"], r'the document is a number, but the key "a"'),
        (b"[1]", ["a"], r'the document is an array, but the key "a"'),
        (b'{"0": 1}', [0], r"the document is an object, but the index 0"),
        (b'{"a": [[1]]}', ["a", 0, "k"], r'the value at \["a", 0\] is an array, but the key "k"'),
    ],
)
def test_json_wrong_kind_of_value_on_the_path_is_always_an_error(
    data: bytes, path: list[str | int], problem: str, if_missing: str
) -> None:
    # Not a matter of something missing: the document is not shaped as the profile assumes.
    with pytest.raises(MismatchError, match=problem):
        apply_config_edits(data, "json", [at(path, 1, if_missing)])


def test_json_missing_key() -> None:
    data = b'{"video": {"maxFps": 60, "hdr": false}}'
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(
            data, "json", [at(["video", "maxFps"], 30), at(["video", "vsync"], True)]
        )
    message = str(caught.value)
    assert message.startswith(
        'edits[1] (path ["video", "vsync"]): the value at ["video"] has no key "vsync"'
    )
    assert "maxFps, hdr" in message  # what the object holds instead
    assert "if_missing" in message
    with pytest.raises(MismatchError, match=r'the document has no key "audio"'):
        apply_config_edits(data, "json", [at(["audio", "volume"], 1)])

    assert apply_config_edits(data, "json", [at(["video", "vsync"], True, "ignore")]) is data
    assert apply_config_edits(data, "json", [at(["audio", "volume"], 1, "ignore")]) is data


def test_json_add_creates_the_key_and_the_missing_parent_objects() -> None:
    data = b'{"video": {"maxFps": 60}}'
    assert edited(data, "json", [at(["video", "vsync"], False, "add")]) == (
        b'{"video": {"maxFps": 60, "vsync": false}}'
    )
    assert edited(data, "json", [at(["audio", "out", "volume"], 0.5, "add")]) == (
        b'{"video": {"maxFps": 60}, "audio": {"out": {"volume": 0.5}}}'
    )
    # An existing key is simply set.
    assert (
        edited(data, "json", [at(["video", "maxFps"], 30, "add")]) == b'{"video": {"maxFps": 30}}'
    )
    multi_line = b'{\n  "a": 1\n}\n'
    assert edited(multi_line, "json", [at(["b", "c"], [1, {}], "add")]) == (
        b'{\n  "a": 1,\n  "b": {\n    "c": [\n      1,\n      {}\n    ]\n  }\n}\n'
    )


def test_json_add_never_creates_an_array() -> None:
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(b'{"a": {}}', "json", [at(["a", "list", 0, "x"], 1, "add")])
    message = str(caught.value)
    assert 'the value at ["a"] has no key "list"' in message
    assert "never arrays" in message
    # Nothing was created on the way either: the same edit, ignoring, leaves the file alone.
    data = b'{"a": {}}'
    assert apply_config_edits(data, "json", [at(["a", "list", 0, "x"], 1, "ignore")]) is data


@pytest.mark.parametrize(
    ("before", "value", "after"),
    [
        (b'{"v": 1}', True, b'{"v": true}'),
        (b'{"v": true}', 1, b'{"v": 1}'),
        (b'{"v": 0}', False, b'{"v": false}'),
        (b'{"v": 1}', 1.0, b'{"v": 1.0}'),
        (b'{"v": 1.0}', 1, b'{"v": 1}'),
        (b'{"v": null}', 0, b'{"v": 0}'),
        (b'{"v": 0}', None, b'{"v": null}'),
        (b'{"v": "1"}', 1, b'{"v": 1}'),
        (b'{"v": 1}', "1", b'{"v": "1"}'),
        (b'{"v": []}', {}, b'{"v": {}}'),
        (b'{"v": [1, 2]}', [2, 1], b'{"v": [2, 1]}'),
        (b'{"v": [1, 2]}', [1, 2, 3], b'{"v": [1, 2, 3]}'),
        (b'{"v": {"a": 1}}', {"a": True}, b'{"v": {"a": true}}'),
        (b'{"v": {"a": 1}}', {"a": 1, "b": 2}, b'{"v": {"a": 1, "b": 2}}'),
        (
            b'{"v": 1}',
            'tab\there "quoted" back\\slash',
            b'{"v": "tab\\there \\"quoted\\" back\\\\slash"}',
        ),
        (b'{"v": 1}', -12345678901234567890, b'{"v": -12345678901234567890}'),
        (b'{"v": 1}', 2.5e-07, b'{"v": 2.5e-07}'),
        # Tuples are arrays too (a profile may hold its lists frozen).
        (b'{"v": 1}', (1, ("a", None)), b'{"v": [1, ["a", null]]}'),
    ],
)
def test_json_value_of_another_type_or_content_replaces_the_old_one(
    before: bytes, value: Any, after: bytes
) -> None:
    assert edited(before, "json", [at(["v"], value)]) == after


@pytest.mark.parametrize(
    ("data", "value"),
    [
        (b'{"v":1 }', 1),
        (b'{"v":1.50 }', 1.5),
        (b'{"v":1e2 }', 100.0),
        (b'{"v":true }', True),
        (b'{"v":null }', None),
        (b'{"v":"\\u00e9\\/" }', "é/"),
        (b'{"v":[1,[2,{"a":null}]] }', [1, [2, {"a": None}]]),
        (b'{"v":[1,2] }', (1, 2)),
        # Object keys have no order: the file keeps its own.
        (b'{"v":{"b":2,"a":1} }', {"a": 1, "b": 2}),
        (b'{"v":NaN }', float("nan")),
    ],
)
def test_json_value_that_is_already_there_leaves_the_file_alone(data: bytes, value: Any) -> None:
    # The stray space would vanish if the document were written back.
    assert apply_config_edits(data, "json", [at(["v"], value)]) is data


def test_json_numbers_no_edit_replaces_keep_their_spelling() -> None:
    # Trailing zeros, exponents, a negative zero with and without fraction, more digits
    # than a double holds and a magnitude no double reaches.
    numbers = (
        b'"a": 1.50, "b": 1e3, "c": -0.0, "d": -0, "pi": 3.14159265358979323846264, "big": 1E400'
    )
    before = b"{" + numbers + b', "n": 1}'
    after = b"{" + numbers + b', "n": 2}'
    assert edited(before, "json", [at(["n"], 2)]) == after
    # A replaced number is written the shortest way that reads back as the same double.
    assert edited(b'{"a": 1.50, "n": 1}', "json", [at(["a"], 2.50)]) == b'{"a": 2.5, "n": 1}'
    # NaN and the infinities are not JSON, but files that hold them keep them.
    special = b'{"a": NaN, "b": -Infinity, "n": 1}'
    assert edited(special, "json", [at(["n"], 2)]) == b'{"a": NaN, "b": -Infinity, "n": 2}'


def test_json_empty_containers_stay_compact_in_a_multi_line_document() -> None:
    before = b'{\n\t"a": [],\n\t"b": {},\n\t"n": 1\n}'
    after = b'{\n\t"a": [],\n\t"b": {},\n\t"n": [\n\t\t{}\n\t]\n}'
    assert edited(before, "json", [at(["n"], [{}])]) == after


STYLES: list[dict[str, Any]] = [
    {"indent": 2},
    {"indent": 4},
    {"indent": "\t"},
    {"indent": None},
    {"indent": None, "separators": (",", ":")},
]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_json_rewrite_matches_what_the_standard_library_writes(
    style: dict[str, Any], ensure_ascii: bool
) -> None:
    # An independent oracle for the layout: a document written by json.dumps, once
    # edited, must be exactly what json.dumps writes for the edited data.
    document: dict[str, Any] = {
        "name": 'Señor "Quote" \\ / 日本 \U0001f600',
        "video": {
            "maxFps": 60,
            "scale": 0.5,
            "tiny": 1e-07,
            "modes": [[1920, 1080], [1280, 720]],
            "hdr": None,
        },
        "flags": [True, False, None],
        "empty": {"list": [], "object": {}, "text": ""},
        "control": "line\nbreak\ttab \u0007 bell",
        "big": 12345678901234567890123,
        "last": -1,
    }
    before = (json.dumps(document, ensure_ascii=ensure_ascii, **style) + "\n").encode()
    document["video"]["maxFps"] = 30
    document["video"]["modes"][1] = {"w": 960, "h": 540}
    document["audio"] = {"volume": 0.75}
    after = (json.dumps(document, ensure_ascii=ensure_ascii, **style) + "\n").encode()
    edits = [
        at(["video", "maxFps"], 30),
        at(["video", "modes", 1], {"w": 960, "h": 540}),
        at(["audio", "volume"], 0.75, "add"),
    ]
    assert edited(before, "json", edits) == after


def test_json_edits_apply_in_order_without_touching_the_values_passed_in() -> None:
    edits = [
        at(["a"], {"b": [1, 2]}, "add"),
        at(["a", "b", 0], 9),  # edits the value the previous edit inserted
        at(["a", "c"], "x", "add"),
    ]
    assert edited(b"{}", "json", edits) == b'{"a": {"b": [9, 2], "c": "x"}}'
    assert edits[0]["value"] == {"b": [1, 2]}
    with pytest.raises(MismatchError, match=r"edits\[1\]"):
        apply_config_edits(b'{"a": 1}', "json", [at(["a"], 2), at(["b"], 3)])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   \n",
        b"{",
        b'{"a": 1,}',
        b"{'a': 1}",
        b'// comment\n{"a": 1}',
        b'{"a": 1} trailing',
        b'{"a": 01}',
        b"\xef\xbb\xbf\xef\xbb\xbf{}",
    ],
)
def test_json_invalid_document(data: bytes) -> None:
    with pytest.raises(MismatchError, match=r"not valid JSON \(.+ at line \d+, column \d+\)"):
        apply_config_edits(data, "json", [at(["a"], 1, "ignore")])


def test_json_number_the_interpreter_refuses_to_convert_is_reported() -> None:
    # Valid JSON, but Python does not convert integers beyond a number of digits.
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with pytest.raises(MismatchError, match="not JSON that BEA can read"):
            apply_config_edits(b'{"a": ' + b"9" * 5000 + b"}", "json", [at(["a"], 1)])
    finally:
        sys.set_int_max_str_digits(previous)


def test_json_duplicated_key_is_refused() -> None:
    # Which "a" counts depends on the parser, and writing the file back would drop one.
    data = b'{"b": {"a": 1, "x": 0, "a": 2}}'
    with pytest.raises(MismatchError, match=r'defines the key "a" twice.*format: text'):
        apply_config_edits(data, "json", [at(["b", "x"], 1)])
    # Even when no edit would change anything.
    with pytest.raises(MismatchError, match="twice"):
        apply_config_edits(data, "json", [at(["b", "x"], 0)])


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ([], "the path is empty"),
        ([True], "True is not a path item"),
        (["a", -1], "-1 is not a path item"),
        (["a", 1.5], "1.5 is not a path item"),
        ([None], "None is not a path item"),
    ],
)
def test_json_malformed_path_is_refused(path: list[Any], problem: str) -> None:
    with pytest.raises(MismatchError, match=problem):
        apply_config_edits(b'{"a": [1, 2]}', "json", [at(path, 0, "ignore")])


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({1, 2}, "holds set data"),
        (b"bytes", "holds bytes data"),
        ({"ok": [object()]}, "holds object data"),
        ({1: "a"}, "object key 1"),
    ],
)
def test_json_value_that_is_not_json_is_refused(value: Any, problem: str) -> None:
    with pytest.raises(MismatchError, match=problem):
        apply_config_edits(b'{"a": 1}', "json", [at(["a"], value)])


def test_json_document_nested_too_deeply_is_reported_not_crashed_on() -> None:
    depth = 2 * sys.getrecursionlimit()
    # Too deep to write back (and possibly to parse): either way a clean refusal.
    data = b"[" + b"[" * depth + b"]" * depth + b", 1]"
    with pytest.raises(MismatchError, match="nested too deeply"):
        apply_config_edits(data, "json", [at([1], 2)])
    with pytest.raises(MismatchError, match="nested too deeply"):
        apply_config_edits(b"[" * 200_000 + b"]" * 200_000, "json", [at([0], 2)])


# -------------------------------------------------------------------------------- text


def test_text_regular_expression_with_a_group_reference() -> None:
    data = b"vsync_interval = 1\nother = 5\n"
    edits = [sub(r"^(vsync_interval\s*=\s*)\d+", r"\g<1>2")]
    assert edited(data, "text", edits) == b"vsync_interval = 2\nother = 5\n"


def test_text_patterns_are_compiled_in_multi_line_mode() -> None:
    data = b"a=1\nb=1\na=1\nxa=1\n"
    assert edited(data, "text", [sub(r"^a=\d$", "a=2", count=2)]) == b"a=2\nb=1\na=2\nxa=1\n"
    # Not in "dot matches newline" mode.
    assert apply_config_edits(data, "text", [sub(r"b.*", "B")]) == b"a=1\nB\na=1\nxa=1\n"


@pytest.mark.parametrize(
    ("count", "message"),
    [
        (1, "expected 1 match, found 2"),
        (3, "expected 3 matches, found 2"),
        (0, "expected 0 matches, found 2"),
    ],
)
def test_text_number_of_matches_must_be_the_expected_one(count: int, message: str) -> None:
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(b"fps=60\nfps=60\n", "text", [sub(r"fps=\d+", "fps=30", count)])
    assert str(caught.value) == f"edits[0] (find 'fps=\\\\d+'): {message}"


def test_text_no_match_is_an_error() -> None:
    with pytest.raises(MismatchError, match=r"edits\[0\].*expected 1 match, found 0"):
        apply_config_edits(b"fps=60\n", "text", [sub("vsync", "x")])
    # The count defaults to one.
    with pytest.raises(MismatchError, match="expected 1 match, found 2"):
        apply_config_edits(b"a a", "text", [{"find": "a", "replace": "b"}])
    assert apply_config_edits(b"a", "text", [{"find": "a", "replace": "b"}]) == b"b"


def test_text_edits_apply_in_order_each_on_the_result_of_the_previous_one() -> None:
    edits = [sub("alpha", "beta"), sub("beta", "gamma", count=2)]
    assert apply_config_edits(b"alpha beta\n", "text", edits) == b"gamma gamma\n"
    with pytest.raises(
        MismatchError, match=r"edits\[1\] \(find 'alpha'\): expected 1 match, found 0"
    ):
        apply_config_edits(b"alpha\n", "text", [sub("alpha", "beta"), sub("alpha", "x")])


def test_text_keeps_line_endings_and_uses_the_replacement_as_written() -> None:
    data = b"x=1\r\ny=2\r\n"
    assert edited(data, "text", [sub(r"^y=\d+", "y=3")]) == b"x=1\r\ny=3\r\n"
    # A line break in the replacement is the profile's own: it is not adapted to the file.
    assert apply_config_edits(data, "text", [sub("^x=1", "x=5\nw=0")]) == b"x=5\nw=0\r\ny=2\r\n"
    assert apply_config_edits(data, "text", [sub("^x=1", r"x=5\nw=0")]) == b"x=5\nw=0\r\ny=2\r\n"


def test_text_with_unicode_bom_and_explicit_encoding() -> None:
    bom = b"\xef\xbb\xbf"
    before = bom + "名前=古い\n".encode()
    after = bom + "名前=新しい\n".encode()
    # "^" matches right at the start: the expression never sees the byte-order mark.
    assert edited(before, "text", [sub(r"^(名前=).*$", r"\1新しい")]) == after
    assert edited(b"caf\xe9=1\n", "text", [sub(r"(?<=caf\xe9=)\d", "2")], encoding="latin-1") == (
        b"caf\xe9=2\n"
    )


@pytest.mark.parametrize(
    ("find", "replace", "problem"),
    [
        ("(unclosed", "x", r"edits\[0\].*regular expression is not valid"),
        ("[z-a]", "x", "regular expression is not valid"),
        ("(a)", r"\2", r"edits\[0\].*replacement '\\\\2' is not valid"),
        ("(a)", r"\g<name>", "replacement .* is not valid"),
        ("a", "trailing\\", "replacement .* is not valid"),
    ],
)
def test_text_invalid_expression_or_replacement_is_reported(
    find: str, replace: str, problem: str
) -> None:
    # Reported even when nothing would match: the edit itself is wrong.
    for data in (b"a\n", b"nothing here\n"):
        with pytest.raises(MismatchError, match=problem):
            apply_config_edits(data, "text", [sub(find, replace)])


def test_text_long_expression_is_shortened_in_the_message() -> None:
    find = "x" * 200
    with pytest.raises(MismatchError) as caught:
        apply_config_edits(b"y\n", "text", [sub(find, "z")])
    message = str(caught.value)
    assert message == f"edits[0] (find '{'x' * 57}...'): expected 1 match, found 0"


# --------------------------------------------------------------------- edits_to_params


def test_edits_to_params_for_an_ini_rule() -> None:
    rule = ConfigRule(
        match=("config/graphics.ini",),
        format=ConfigFormat.INI,
        edits=(
            IniEdit("Rendering", "ResolutionScale", "70"),
            IniEdit(None, "ShadowQuality", "Low", IfMissing.ADD),
            IniEdit("Rendering", "Fog", "0", IfMissing.IGNORE),
        ),
    )
    assert edits_to_params(rule) == {
        "format": "ini",
        "encoding": None,
        "edits": [
            {
                "section": "Rendering",
                "key": "ResolutionScale",
                "value": "70",
                "if_missing": "error",
            },
            {"section": None, "key": "ShadowQuality", "value": "Low", "if_missing": "add"},
            {"section": "Rendering", "key": "Fog", "value": "0", "if_missing": "ignore"},
        ],
    }


def test_edits_to_params_for_a_json_rule() -> None:
    value = {"modes": ((1280, 720), (960, 540)), "hdr": None}
    rule = ConfigRule(
        match=("*.json",),
        format=ConfigFormat.JSON,
        edits=(JsonEdit(("video", "list", 0), value, IfMissing.ADD), JsonEdit(("fps",), 30)),
        encoding="utf-16",
    )
    params = edits_to_params(rule)
    assert params == {
        "format": "json",
        "encoding": "utf-16",
        "edits": [
            {
                "path": ["video", "list", 0],
                "value": {"modes": [[1280, 720], [960, 540]], "hdr": None},
                "if_missing": "add",
            },
            {"path": ["fps"], "value": 30, "if_missing": "error"},
        ],
    }
    # Plain data all the way down: lists instead of tuples, strings instead of enums.
    assert json.loads(json.dumps(params)) == params
    assert type(params["format"]) is str
    assert type(params["edits"][0]["if_missing"]) is str
    assert type(params["edits"][0]["path"]) is list
    assert type(params["edits"][0]["value"]["modes"][0]) is list


def test_edits_to_params_shares_nothing_mutable_with_the_profile() -> None:
    value = {"list": [1, 2], "nested": {"k": [3]}}
    rule = ConfigRule(match=("a.json",), format=ConfigFormat.JSON, edits=(JsonEdit(("a",), value),))
    params = edits_to_params(rule)
    params["edits"][0]["value"]["list"].append(99)
    params["edits"][0]["value"]["nested"]["k"].clear()
    params["edits"][0]["path"].append("x")
    assert value == {"list": [1, 2], "nested": {"k": [3]}}
    assert rule.edits[0] == JsonEdit(("a",), {"list": [1, 2], "nested": {"k": [3]}})


def test_edits_to_params_for_a_text_rule() -> None:
    rule = ConfigRule(
        match=("boot/engine.cfg",),
        format=ConfigFormat.TEXT,
        edits=(TextEdit(r"^(vsync\s*=\s*)\d+", r"\g<1>2"), TextEdit("a", "b", count=3)),
        encoding="shift_jis",
        required=False,
    )
    assert edits_to_params(rule) == {
        "format": "text",
        "encoding": "shift_jis",
        "edits": [
            {"find": r"^(vsync\s*=\s*)\d+", "replace": r"\g<1>2", "count": 1},
            {"find": "a", "replace": "b", "count": 3},
        ],
    }


def test_edits_to_params_refuses_an_edit_of_the_wrong_kind() -> None:
    rule = ConfigRule(
        match=("a.ini",),
        format=ConfigFormat.INI,
        edits=(IniEdit(None, "A", "1"), TextEdit("a", "b")),
    )
    with pytest.raises(
        TypeError, match=r"edits\[1\] is a TextEdit, but format 'ini' takes IniEdit"
    ):
        edits_to_params(rule)


@pytest.mark.parametrize(
    ("rule", "before", "after"),
    [
        (
            ConfigRule(
                match=("a.ini",),
                format=ConfigFormat.INI,
                edits=(IniEdit("S", "A", "2"), IniEdit("S", "B", "3", IfMissing.ADD)),
            ),
            b"[S]\nA=1\n",
            b"[S]\nA=2\nB=3\n",
        ),
        (
            ConfigRule(
                match=("a.json",),
                format=ConfigFormat.JSON,
                edits=(JsonEdit(("a", "b"), (1, 2), IfMissing.ADD), JsonEdit(("n",), None)),
            ),
            b'{"n": 1}',
            b'{"n": null, "a": {"b": [1, 2]}}',
        ),
        (
            ConfigRule(
                match=("a.cfg",),
                format=ConfigFormat.TEXT,
                edits=(TextEdit(r"(?<=fps=)\d+", "30", count=2),),
                encoding="utf-16-le",
            ),
            "fps=60;fps=120".encode("utf-16-le"),
            "fps=30;fps=30".encode("utf-16-le"),
        ),
    ],
)
def test_params_drive_apply_config_edits_also_after_a_trip_through_json(
    rule: ConfigRule, before: bytes, after: bytes
) -> None:
    # Action.params are stored as JSON by the pipeline's cache: nothing may be lost there.
    for params in (edits_to_params(rule), json.loads(json.dumps(edits_to_params(rule)))):
        result = apply_config_edits(
            before, params["format"], params["edits"], encoding=params["encoding"]
        )
        assert result == after


# ------------------------------------------------------------------- ConfigTransformer


def make_context(
    tmp_path: Path, files: Mapping[str, bytes | None], rules: Sequence[ConfigRule]
) -> PlanContext:
    """Build a game folder under ``tmp_path`` and a profile holding ``rules``.

    ``files`` maps RomFS-relative paths to their content; ``None`` lists the file in the
    game index without creating it on disk.
    """
    romfs = tmp_path / "game" / "romfs"
    romfs.mkdir(parents=True)
    entries = []
    for relative in sorted(files):
        content = files[relative]
        if content is None:
            entries.append(FileEntry(relative, 0, 0))
            continue
        target = romfs.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        stat = target.stat()
        entries.append(FileEntry(relative, stat.st_size, stat.st_mtime_ns))
    game = GameInput(
        root=tmp_path / "game",
        exefs_dir=None,
        romfs_dir=romfs,
        nacp_path=None,
        title=TitleInfo(),
        modules=(),
        exefs_files=(),
        romfs_files=tuple(entries),
    )
    profile = Profile(title_id=None, configs=tuple(rules))
    return PlanContext(game=game, profile=profile, tools=Toolbox(tmp_path / "tools", environ={}))


def with_entries(context: PlanContext, *entries: FileEntry) -> PlanContext:
    """Return ``context`` with more entries in the RomFS index of its game."""
    game = dataclasses.replace(context.game, romfs_files=(*context.game.romfs_files, *entries))
    return dataclasses.replace(context, game=game)


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Content and modification time of every file below ``root``."""
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def outline(items: Sequence[PlanItem]) -> list[tuple[str, str, str]]:
    """Reduce plan items to ``(kind, source, detail)`` for comparison."""
    return [
        ("action", item.source, item.summary)
        if isinstance(item, Action)
        else ("skip", item.source, item.kind.value)
        for item in items
    ]


def ini_rule(match: str, *edits: IniEdit, **options: Any) -> ConfigRule:
    return ConfigRule(match=(match,), format=ConfigFormat.INI, edits=edits, **options)


SCALE_70 = IniEdit("Video", "Scale", "70")


def test_transformer_identity() -> None:
    transformer = ConfigTransformer()
    assert transformer.name == "config"
    assert transformer.version == 1
    assert transformer.supports("romfs/config/graphics.ini")
    assert transformer.supports("romfs/anything.at.all")
    assert not transformer.supports("romfs/")
    assert not transformer.supports("exefs/main")
    assert not transformer.supports("exefs_patches/0A.ips")
    assert not transformer.supports("config/graphics.ini")


def test_plan_yields_an_action_for_a_file_the_edits_change(tmp_path: Path) -> None:
    rule = ini_rule(
        "config/graphics.ini", SCALE_70, IniEdit("Video", "Shadows", "Low", IfMissing.ADD)
    )
    context = make_context(tmp_path, {"config/graphics.ini": b"[Video]\nScale=100\n"}, [rule])
    (action,) = ConfigTransformer().plan(context)
    assert action == Action(
        transformer="config",
        source="romfs/config/graphics.ini",
        output="romfs/config/graphics.ini",
        params={
            "format": "ini",
            "encoding": None,
            "edits": [
                {"section": "Video", "key": "Scale", "value": "70", "if_missing": "error"},
                {"section": "Video", "key": "Shadows", "value": "Low", "if_missing": "add"},
            ],
        },
        summary="2 edits (ini)",
        details={"format": "ini", "edits": 2},
    )
    assert isinstance(action, Action)
    assert action.params == edits_to_params(rule)
    # The pipeline hashes the parameters as JSON to key its cache.
    assert json.loads(json.dumps(action.params)) == action.params


def test_plan_summary_counts_a_single_edit_in_the_singular(tmp_path: Path) -> None:
    rule = ConfigRule(
        match=("settings.json",), format=ConfigFormat.JSON, edits=(JsonEdit(("fps",), 30),)
    )
    context = make_context(tmp_path, {"settings.json": b'{"fps": 60}'}, [rule])
    (action,) = ConfigTransformer().plan(context)
    assert isinstance(action, Action)
    assert action.summary == "1 edit (json)"
    assert action.details == {"format": "json", "edits": 1}


def test_plan_reports_a_required_rule_that_selects_no_file(tmp_path: Path) -> None:
    rules = [
        ini_rule("config/other.ini", SCALE_70),
        ConfigRule(
            match=("config/graphics.ini", "cfg/*.ini"),
            exclude=("cfg/keep.ini",),
            format=ConfigFormat.INI,
            edits=(SCALE_70,),
        ),
    ]
    files = {"config/other.ini": b"[Video]\nScale=70\n", "cfg/keep.ini": b"[Video]\nScale=1\n"}
    (skip,) = ConfigTransformer().plan(make_context(tmp_path, files, rules))
    assert isinstance(skip, Skip)
    assert (skip.transformer, skip.source, skip.kind) == ("config", "configs[1]", SkipKind.MISMATCH)
    assert skip.is_error
    for fragment in ("'config/graphics.ini'", "'cfg/*.ini'", "'cfg/keep.ini'", "required: false"):
        assert fragment in skip.reason


def test_plan_stays_silent_for_an_optional_rule_that_selects_no_file(tmp_path: Path) -> None:
    rule = ini_rule("config/graphics.ini", SCALE_70, required=False)
    context = make_context(tmp_path, {"config/other.ini": b"[Video]\nScale=100\n"}, [rule])
    assert list(ConfigTransformer().plan(context)) == []


def test_plan_without_configs_or_without_romfs(tmp_path: Path) -> None:
    context = make_context(tmp_path, {"a.ini": b"[Video]\nScale=100\n"}, [])
    assert list(ConfigTransformer().plan(context)) == []
    # A game folder that has no RomFS at all: every required entry is unmet.
    game = dataclasses.replace(context.game, romfs_dir=None, romfs_files=())
    profile = Profile(title_id=None, configs=(ini_rule("a.ini", SCALE_70),))
    context = PlanContext(game=game, profile=profile, tools=context.tools)
    assert outline(list(ConfigTransformer().plan(context))) == [("skip", "configs[0]", "mismatch")]


def test_plan_refuses_a_file_selected_by_more_than_one_rule(tmp_path: Path) -> None:
    rules = [
        ini_rule("shared/*.ini", SCALE_70),
        ini_rule("other/*.ini", SCALE_70, required=False),
        ini_rule("shared/dup.ini", IniEdit("Video", "Scale", "50")),
        ini_rule("*.ini", IniEdit("Video", "Gamma", "2", IfMissing.ADD), required=False),
    ]
    files = {
        "shared/dup.ini": b"[Video]\nScale=100\n",
        "shared/solo.ini": b"[Video]\nScale=100\n",
    }
    items = list(ConfigTransformer().plan(make_context(tmp_path, files, rules)))
    # One skip per contested file, however many rules claim it, and no action for it.
    assert outline(items) == [
        ("skip", "romfs/shared/dup.ini", "mismatch"),
        ("skip", "romfs/shared/solo.ini", "mismatch"),
    ]
    reasons = [item.reason for item in items if isinstance(item, Skip)]
    assert "configs[0], configs[2] and configs[3]" in reasons[0]
    assert "configs[0] and configs[3]" in reasons[1]
    assert all("one configs entry only" in reason for reason in reasons)


def test_plan_still_handles_the_other_files_of_rules_that_share_one(tmp_path: Path) -> None:
    rules = [ini_rule("a/*.ini", SCALE_70), ini_rule("*/x.ini", SCALE_70)]
    files = {
        "a/x.ini": b"[Video]\nScale=100\n",
        "a/y.ini": b"[Video]\nScale=100\n",
        "b/x.ini": b"[Video]\nScale=100\n",
    }
    items = list(ConfigTransformer().plan(make_context(tmp_path, files, rules)))
    assert outline(items) == [
        ("skip", "romfs/a/x.ini", "mismatch"),
        ("action", "romfs/a/y.ini", "1 edit (ini)"),
        ("action", "romfs/b/x.ini", "1 edit (ini)"),
    ]


def test_plan_yields_nothing_for_a_file_that_already_complies(tmp_path: Path) -> None:
    rules = [
        ini_rule("a.ini", SCALE_70, IniEdit("Video", "Missing", "1", IfMissing.IGNORE)),
        ConfigRule(match=("b.json",), format=ConfigFormat.JSON, edits=(JsonEdit(("fps",), 30),)),
        ConfigRule(
            match=("c.cfg",),
            format=ConfigFormat.TEXT,
            edits=(TextEdit(r"^(fps=)\d+", r"\g<1>30"),),
        ),
    ]
    files = {
        "a.ini": b"[Video]\r\nScale = 70\r\n",
        "b.json": b'{ "fps" :30 }',
        "c.cfg": b"fps=30\n",
    }
    assert list(ConfigTransformer().plan(make_context(tmp_path, files, rules))) == []


def test_plan_reports_a_file_the_edits_do_not_fit(tmp_path: Path) -> None:
    rules = [
        ini_rule("a.ini", SCALE_70),
        ConfigRule(match=("b.json",), format=ConfigFormat.JSON, edits=(JsonEdit(("fps",), 30),)),
        ConfigRule(
            match=("c.cfg",), format=ConfigFormat.TEXT, edits=(TextEdit("fps=60", "fps=30"),)
        ),
        ini_rule("d.ini", SCALE_70),
    ]
    files = {
        "a.ini": b"[Video]\nGamma=1\n",
        "b.json": b"{not json}",
        "c.cfg": b"fps=60 fps=60\n",
        "d.ini": b"[Video]\nScale=\xff\xfe\n",
    }
    context = make_context(tmp_path, files, rules)
    items = list(ConfigTransformer().plan(context))
    assert outline(items) == [
        ("skip", "romfs/a.ini", "mismatch"),
        ("skip", "romfs/b.json", "mismatch"),
        ("skip", "romfs/c.cfg", "mismatch"),
        ("skip", "romfs/d.ini", "mismatch"),
    ]
    assert all(isinstance(item, Skip) and item.is_error for item in items)
    # The reason is the message of the error apply_config_edits raises for that file.
    for item, relative, rule in zip(items, sorted(files), rules, strict=True):
        params = edits_to_params(rule)
        with pytest.raises(MismatchError) as caught:
            apply_config_edits(files[relative], params["format"], params["edits"])
        assert isinstance(item, Skip)
        assert item.reason == str(caught.value)
    reasons = [item.reason for item in items if isinstance(item, Skip)]
    assert reasons[0].startswith("edits[0] (key 'Scale' in [Video]): the key is not defined")
    assert "not valid JSON" in reasons[1]
    assert reasons[2] == "edits[0] (find 'fps=60'): expected 1 match, found 2"
    assert "`encoding`" in reasons[3]


def test_plan_reports_an_unreadable_file_as_invalid(tmp_path: Path) -> None:
    rule = ini_rule("cfg/*.ini", SCALE_70)
    files = {"cfg/ghost.ini": None, "cfg/real.ini": b"[Video]\nScale=100\n"}
    context = make_context(tmp_path, files, [rule])
    # A directory where a file is expected cannot be read either.
    (context.game.resolve("romfs/cfg") / "folder.ini").mkdir()
    context = with_entries(context, FileEntry("cfg/folder.ini", 0, 0))
    items = list(ConfigTransformer().plan(context))
    assert outline(items) == [
        ("skip", "romfs/cfg/ghost.ini", "invalid"),
        ("action", "romfs/cfg/real.ini", "1 edit (ini)"),
        ("skip", "romfs/cfg/folder.ini", "invalid"),
    ]
    skips = [item for item in items if isinstance(item, Skip)]
    assert all(skip.reason.startswith("cannot read the file: ") for skip in skips)
    assert not any(skip.is_error for skip in skips)


def test_plan_never_reads_outside_of_the_romfs(tmp_path: Path) -> None:
    rule = ini_rule("*.ini", SCALE_70)
    context = make_context(tmp_path, {"cfg/real.ini": b"[Video]\nScale=70\n"}, [rule])
    # A file the rule would change, one level above the RomFS, and an index entry that
    # points at it: the entry is refused instead of being followed.
    (tmp_path / "game" / "outside.ini").write_bytes(b"[Video]\nScale=100\n")
    context = with_entries(context, FileEntry("../outside.ini", 18, 0))
    items = list(ConfigTransformer().plan(context))
    assert outline(items) == [("skip", "romfs/../outside.ini", "invalid")]
    assert isinstance(items[0], Skip)
    assert items[0].reason.startswith("cannot read the file: ")
    # The same goes for an index that names files although there is no RomFS folder.
    orphan = dataclasses.replace(context.game, romfs_dir=None)
    items = list(ConfigTransformer().plan(dataclasses.replace(context, game=orphan)))
    assert outline(items) == [
        ("skip", "romfs/cfg/real.ini", "invalid"),
        ("skip", "romfs/../outside.ini", "invalid"),
    ]


def test_plan_goes_rule_by_rule_and_file_by_file(tmp_path: Path) -> None:
    json_30 = (JsonEdit(("fps",), 30),)
    text_edit = (TextEdit(r"^(fps=)\d+", r"\g<1>30"),)
    rules = [
        ini_rule("absent/optional.ini", SCALE_70, required=False),
        ini_rule("absent/required.ini", SCALE_70),
        ini_rule("cfg/a.ini", SCALE_70),
        ConfigRule(match=("cfg/*.json",), format=ConfigFormat.JSON, edits=json_30),
        ConfigRule(match=("cfg/shared.cfg",), format=ConfigFormat.TEXT, edits=text_edit),
        ConfigRule(match=("cfg/*.cfg",), format=ConfigFormat.TEXT, edits=text_edit),
        ini_rule("cfg/ghost.ini", SCALE_70),
    ]
    files = {
        "cfg/a.ini": b"[Video]\nScale=100\n",
        "cfg/bad.json": b'{"fps": 60',
        "cfg/ghost.ini": None,
        "cfg/ok.json": b'{"fps": 30}',
        "cfg/old.json": b'{"fps": 60}',
        "cfg/shared.cfg": b"fps=60\n",
        "cfg/solo.cfg": b"fps=60\n",
        "unrelated.bin": b"\x00\x01\x02",
    }
    items = list(ConfigTransformer().plan(make_context(tmp_path, files, rules)))
    assert outline(items) == [
        ("skip", "configs[1]", "mismatch"),
        ("action", "romfs/cfg/a.ini", "1 edit (ini)"),
        ("skip", "romfs/cfg/bad.json", "mismatch"),
        ("action", "romfs/cfg/old.json", "1 edit (json)"),
        ("skip", "romfs/cfg/shared.cfg", "mismatch"),
        ("action", "romfs/cfg/solo.cfg", "1 edit (text)"),
        ("skip", "romfs/cfg/ghost.ini", "invalid"),
    ]
    assert all(item.transformer == "config" for item in items)
    assert all(item.output == item.source for item in items if isinstance(item, Action))


def test_plan_matches_without_regard_to_case_and_honours_exclude_and_encoding(
    tmp_path: Path,
) -> None:
    rule = ConfigRule(
        match=("config/*.ini",),
        exclude=("config/skip*.ini",),
        format=ConfigFormat.INI,
        edits=(IniEdit(None, "Name", "garçon"),),
        encoding="cp1252",
    )
    files = {
        "Config/Graphics.INI": b"Name=caf\xe9\n",  # not UTF-8: only readable as cp1252
        "Config/SkipMe.ini": b"Name=caf\xe9\n",
    }
    (action,) = ConfigTransformer().plan(make_context(tmp_path, files, [rule]))
    assert isinstance(action, Action)
    assert action.source == action.output == "romfs/Config/Graphics.INI"
    assert action.params["encoding"] == "cp1252"


def test_plan_never_writes(tmp_path: Path) -> None:
    rules = [
        ini_rule("a.ini", SCALE_70, IniEdit("New", "K", "1", IfMissing.ADD)),
        ini_rule("b.ini", SCALE_70),
        ini_rule("missing.ini", SCALE_70),
    ]
    files = {"a.ini": b"[Video]\nScale=100\n", "b.ini": b"no section here\n"}
    context = make_context(tmp_path, files, rules)
    before = snapshot(tmp_path)
    assert len(list(ConfigTransformer().plan(context))) == 3
    assert snapshot(tmp_path) == before


def test_planned_actions_run_through_transform(tmp_path: Path) -> None:
    rules = [
        ini_rule("cfg/graphics.ini", SCALE_70, IniEdit("Video", "Shadows", "Low", IfMissing.ADD)),
        ConfigRule(
            match=("cfg/settings.json",),
            format=ConfigFormat.JSON,
            edits=(JsonEdit(("video", "maxFps"), 30),),
        ),
        ConfigRule(
            match=("boot/engine.cfg",),
            format=ConfigFormat.TEXT,
            edits=(TextEdit(r"^(vsync_interval\s*=\s*)\d+", r"\g<1>2"),),
        ),
    ]
    files = {
        "cfg/graphics.ini": b"\xef\xbb\xbf[Video]\r\nScale = 100\r\n\r\n[Audio]\r\nVolume=5\r\n",
        "cfg/settings.json": b'{\n\t"video": {\n\t\t"maxFps": 60\n\t}\n}\n',
        "boot/engine.cfg": b"vsync_interval = 1\nthreads = 4\n",
    }
    expected = {
        "romfs/cfg/graphics.ini": (
            b"\xef\xbb\xbf[Video]\r\nScale = 70\r\nShadows=Low\r\n\r\n[Audio]\r\nVolume=5\r\n"
        ),
        "romfs/cfg/settings.json": b'{\n\t"video": {\n\t\t"maxFps": 30\n\t}\n}\n',
        "romfs/boot/engine.cfg": b"vsync_interval = 2\nthreads = 4\n",
    }
    context = make_context(tmp_path, files, rules)
    transformer = ConfigTransformer()
    actions = [item for item in transformer.plan(context) if isinstance(item, Action)]
    assert sorted(action.output for action in actions) == sorted(expected)
    out = tmp_path / "staging"
    out.mkdir()
    game_before = snapshot(tmp_path / "game")
    for number, action in enumerate(actions):
        destination = out / f"{number}.tmp"
        result = transformer.transform(
            context.game.resolve(action.source), destination, action.params
        )
        assert destination.read_bytes() == expected[action.output]
        assert result.details["bytes_out"] == len(expected[action.output])
    assert snapshot(tmp_path / "game") == game_before

    # Planning again over the results (as the game folder) finds nothing left to do.
    replay = make_context(
        tmp_path / "replay",
        {logical.removeprefix("romfs/"): content for logical, content in expected.items()},
        rules,
    )
    assert list(transformer.plan(replay)) == []


def test_transform_writes_exactly_what_apply_config_edits_returns(tmp_path: Path) -> None:
    data = b"\xef\xbb\xbf[Video]\r\nScale = 100\r\n"
    src = tmp_path / "in" / "graphics.ini"
    src.parent.mkdir()
    src.write_bytes(data)
    before = snapshot(tmp_path / "in")
    dst = tmp_path / "out" / "graphics.bea-tmp-0123abcd.ini"
    dst.parent.mkdir()
    rule = ini_rule("graphics.ini", SCALE_70, IniEdit("Video", "Shadows", "Low", IfMissing.ADD))
    params = edits_to_params(rule)

    result = ConfigTransformer().transform(src, dst, params)

    expected = b"\xef\xbb\xbf[Video]\r\nScale = 70\r\nShadows=Low\r\n"
    assert dst.read_bytes() == expected
    assert apply_config_edits(data, params["format"], params["edits"]) == expected
    assert snapshot(tmp_path / "in") == before  # src is only ever read
    assert [path.name for path in dst.parent.iterdir()] == [dst.name]
    assert isinstance(result, TransformResult)
    assert result.notes == []
    assert result.details == {"format": "ini", "edits": 2, "bytes_in": 25, "bytes_out": 37}


def test_transform_honours_the_encoding_parameter(tmp_path: Path) -> None:
    src = tmp_path / "in.cfg"
    src.write_bytes(b"name=caf\xe9\n")
    dst = tmp_path / "out.cfg"
    config = {"format": "text", "encoding": "cp1252", "edits": [sub("café", "thé")]}
    result = ConfigTransformer().transform(src, dst, config)
    assert dst.read_bytes() == b"name=th\xe9\n"
    assert result.details == {"format": "text", "edits": 1, "bytes_in": 10, "bytes_out": 9}
    # The key may be absent altogether: UTF-8 is then assumed, and this file is not UTF-8.
    with pytest.raises(MismatchError, match="not valid UTF-8"):
        ConfigTransformer().transform(src, dst, {"format": "text", "edits": [sub("a", "b")]})


def test_transform_writes_nothing_when_an_edit_does_not_fit(tmp_path: Path) -> None:
    src = tmp_path / "graphics.ini"
    src.write_bytes(b"[Video]\nGamma=1\n")
    dst = tmp_path / "out" / "graphics.ini"
    dst.parent.mkdir()
    params = edits_to_params(ini_rule("graphics.ini", SCALE_70))
    with pytest.raises(MismatchError, match=r"edits\[0\] \(key 'Scale' in \[Video\]\)"):
        ConfigTransformer().transform(src, dst, params)
    assert list(dst.parent.iterdir()) == []
    assert src.read_bytes() == b"[Video]\nGamma=1\n"


def test_transform_reports_unusable_paths_as_user_errors(tmp_path: Path) -> None:
    params = edits_to_params(ini_rule("graphics.ini", SCALE_70))
    src = tmp_path / "graphics.ini"
    with pytest.raises(InputError, match="cannot read") as missing:
        ConfigTransformer().transform(src, tmp_path / "out.ini", params)
    assert isinstance(missing.value, BeaError)
    assert not (tmp_path / "out.ini").exists()

    src.write_bytes(b"[Video]\nScale=100\n")
    with pytest.raises(OutputError, match="cannot write"):
        ConfigTransformer().transform(src, tmp_path / "no-such-folder" / "out.ini", params)
    assert src.read_bytes() == b"[Video]\nScale=100\n"
