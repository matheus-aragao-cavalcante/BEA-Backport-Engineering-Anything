"""Tests for bea.packagers.layeredfs: destinations, the manifest, safe clean-up."""

from __future__ import annotations

import errno
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from bea import fsutil
from bea.errors import OutputError
from bea.packagers.base import PackageContext, Packager, PackageResult, StagedFile
from bea.packagers.layeredfs import MANIFEST_NAME, LayeredFSPackager, Layout

# Built at run time so that this file holds no title-ID literal.
TITLE_ID = f"{(0x0100 << 48) | 0xBEA0BEA0B000:016X}"
OTHER_TITLE_ID = f"{(0x0100 << 48) | 0xBEA0BEA0C000:016X}"
SET = "bea-test"
CONTEXT = PackageContext(title_id=TITLE_ID, patchset_name=SET)
#: Where the atmosphere layout puts the romfs and exefs trees of the test title.
CONTENTS = f"atmosphere/contents/{TITLE_ID}"


# ----------------------------------------------------------------------------------
# Helpers (independent of the packager: they only read and write plain files)
# ----------------------------------------------------------------------------------


def stage(root: Path, files: Mapping[str, bytes]) -> list[StagedFile]:
    """Write a synthetic staging tree and describe it the way the pipeline does."""
    staged = []
    for logical, data in files.items():
        path = fsutil.resolve_relative(root, logical)
        fsutil.atomic_write_bytes(path, data)
        staged.append(StagedFile(logical=logical, path=path, size=len(data)))
    return staged


def tree(root: Path) -> dict[str, bytes]:
    """Return every file below ``root`` as ``{POSIX relative path: content}``."""
    top = fsutil.fs_path(root, force=True)
    found: dict[str, bytes] = {}
    for directory, _subdirs, names in os.walk(top):
        for name in names:
            full = os.path.join(directory, name)
            with open(full, "rb") as handle:
                found[full[len(top) + 1 :].replace(os.sep, "/")] = handle.read()
    return found


def directories(root: Path) -> set[str]:
    """Return every directory below ``root`` as POSIX relative paths."""
    top = fsutil.fs_path(root, force=True)
    return {
        os.path.join(directory, name)[len(top) + 1 :].replace(os.sep, "/")
        for directory, subdirs, _names in os.walk(top)
        for name in subdirs
    }


def package_tree(out: Path) -> dict[str, bytes]:
    """Return the files of the package folder, without the manifest."""
    found = tree(out)
    found.pop(MANIFEST_NAME, None)
    return found


def manifest_text(
    files: Sequence[str], *, layout: str = "atmosphere", title_id: str = TITLE_ID
) -> str:
    """Return the manifest BEA must write, spelled out by hand.

    Sorted keys, two-space indentation, one trailing newline. Only for ASCII names
    (nothing needs escaping).
    """
    listed = "[]"
    if files:
        listed = "[\n" + ",\n".join(f'    "{name}"' for name in files) + "\n  ]"
    return (
        "{\n"
        f'  "files": {listed},\n'
        f'  "layout": "{layout}",\n'
        '  "packager": "layeredfs",\n'
        '  "schema": 1,\n'
        f'  "title_id": "{title_id}"\n'
        "}\n"
    )


def read_manifest_text(out: Path) -> str:
    return (out / MANIFEST_NAME).read_bytes().decode("utf-8")


def write_manifest(out: Path, files: object, **overrides: object) -> None:
    """Plant the manifest of a pretended previous run."""
    document: dict[str, object] = {
        "schema": 1,
        "packager": "layeredfs",
        "layout": "atmosphere",
        "title_id": TITLE_ID,
        "files": files,
    }
    document.update(overrides)
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST_NAME).write_bytes(json.dumps(document).encode("utf-8"))


def put(path: Path, data: bytes) -> Path:
    """Create a file the way a user (not BEA) would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def failing_on_call(number: int, error: BaseException) -> Callable[..., str]:
    """Return a stand-in for ``fsutil.link_or_copy`` that raises on its ``number``-th call."""
    real = fsutil.link_or_copy
    calls = 0

    def link_or_copy(source: object, destination: object) -> str:
        nonlocal calls
        calls += 1
        if calls == number:
            raise error
        return real(source, destination)  # type: ignore[arg-type]

    return link_or_copy


def make_directory_link(link: Path, target: Path) -> bool:
    """Make ``link`` a symbolic link (or a junction) to the directory ``target``, if allowed."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        return True
    if sys.platform == "win32":
        # Junctions need no privilege, unlike symbolic links on a default Windows account.
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, AttributeError, OSError):
            return False
        return True
    return False


# ----------------------------------------------------------------------------------
# Public surface, layouts and destinations
# ----------------------------------------------------------------------------------


def test_public_surface() -> None:
    assert MANIFEST_NAME == ".bea-package.json"
    assert [(member.name, member.value) for member in Layout] == [
        ("ATMOSPHERE", "atmosphere"),
        ("FLAT", "flat"),
    ]
    packager = LayeredFSPackager()
    assert isinstance(packager, Packager)
    assert LayeredFSPackager.name == "layeredfs"
    assert packager.layout is Layout.ATMOSPHERE
    assert LayeredFSPackager("flat").layout is Layout.FLAT
    assert LayeredFSPackager(Layout.FLAT).layout is Layout.FLAT
    assert LayeredFSPackager("atmosphere").layout is Layout.ATMOSPHERE


@pytest.mark.parametrize("layout", ["", "Atmosphere", "FLAT", "atmosphere ", "sdcard", "flat/"])
def test_unknown_layout_is_rejected_listing_the_valid_ones(layout: str) -> None:
    with pytest.raises(ValueError) as caught:
        LayeredFSPackager(layout)
    message = str(caught.value)
    assert repr(layout) in message
    assert "'atmosphere'" in message
    assert "'flat'" in message


@pytest.mark.parametrize(
    ("layout", "logical", "expected"),
    [
        ("atmosphere", "romfs/a.bin", f"atmosphere/contents/{TITLE_ID}/romfs/a.bin"),
        (
            "atmosphere",
            "romfs/Data/UI/Title Screen.v2.png",
            f"atmosphere/contents/{TITLE_ID}/romfs/Data/UI/Title Screen.v2.png",
        ),
        ("atmosphere", "exefs/main", f"atmosphere/contents/{TITLE_ID}/exefs/main"),
        ("atmosphere", "exefs/main.npdm", f"atmosphere/contents/{TITLE_ID}/exefs/main.npdm"),
        ("atmosphere", "exefs_patches/AB12CD.ips", f"atmosphere/exefs_patches/{SET}/AB12CD.ips"),
        ("flat", "romfs/a.bin", f"{SET}/romfs/a.bin"),
        ("flat", "romfs/Data/UI/Title Screen.v2.png", f"{SET}/romfs/Data/UI/Title Screen.v2.png"),
        ("flat", "exefs/main", f"{SET}/exefs/main"),
        ("flat", "exefs/main.npdm", f"{SET}/exefs/main.npdm"),
        ("flat", "exefs_patches/AB12CD.ips", f"{SET}/exefs/AB12CD.ips"),
        # Names that merely look special are ordinary names and are kept verbatim.
        (
            "atmosphere",
            "romfs/.hidden/..data",
            f"atmosphere/contents/{TITLE_ID}/romfs/.hidden/..data",
        ),
        ("flat", "romfs/romfs/exefs/x", f"{SET}/romfs/romfs/exefs/x"),
        (
            "flat",
            "romfs/\u30c6\u30af\u30b9\u30c1\u30e3/\u00df.bin",
            f"{SET}/romfs/\u30c6\u30af\u30b9\u30c1\u30e3/\u00df.bin",
        ),
    ],
)
def test_destination_of_every_tree_in_both_layouts(
    layout: str, logical: str, expected: str
) -> None:
    assert LayeredFSPackager(layout).destination(logical, CONTEXT) == expected


def test_destination_uses_the_names_of_the_context() -> None:
    longest = "A.b_c-9" + "z" * 57  # 64 characters: the longest patch-set name
    context = PackageContext(title_id=OTHER_TITLE_ID, patchset_name=longest)
    atmosphere = LayeredFSPackager("atmosphere")
    flat = LayeredFSPackager("flat")
    assert (
        atmosphere.destination("romfs/x", context)
        == f"atmosphere/contents/{OTHER_TITLE_ID}/romfs/x"
    )
    assert (
        atmosphere.destination("exefs_patches/x.ips", context)
        == f"atmosphere/exefs_patches/{longest}/x.ips"
    )
    assert flat.destination("exefs/main", context) == f"{longest}/exefs/main"
    for name in ("a", "0", "7zip", "Mod.v1_2-final"):
        context = PackageContext(title_id=TITLE_ID, patchset_name=name)
        assert flat.destination("romfs/x", context) == f"{name}/romfs/x"


@pytest.mark.parametrize("layout", ["atmosphere", "flat"])
@pytest.mark.parametrize(
    "logical",
    [
        "",
        "a.bin",
        "data/a.bin",
        "ROMFS/a.bin",
        "Romfs/a.bin",
        "romfs2/a.bin",
        "romfs_patches/a.bin",
        "exefs_patch/a.ips",
        "sdmc/a.bin",
        "/romfs/a.bin",
        "romfs\\a.bin",
        " romfs/a.bin",
        # A tree name alone names no file.
        "romfs",
        "romfs/",
        "exefs/",
        "exefs_patches/",
    ],
)
def test_destination_rejects_what_is_not_a_logical_path(layout: str, logical: str) -> None:
    with pytest.raises(ValueError):
        LayeredFSPackager(layout).destination(logical, CONTEXT)


@pytest.mark.parametrize("layout", ["atmosphere", "flat"])
@pytest.mark.parametrize("tree_name", ["romfs", "exefs", "exefs_patches"])
@pytest.mark.parametrize(
    "relative",
    [
        "/abs.bin",
        "..",
        ".",
        "../escape.bin",
        "a/../../escape.bin",
        "a/..",
        "a/../b.bin",
        "a/./b.bin",
        "./a.bin",
        "a//b.bin",
        "a/",
        "a\\b.bin",
        "..\\escape.bin",
        "C:/windows/a.bin",
        "c:a.bin",
    ],
)
def test_destination_rejects_unsafe_relative_parts(
    layout: str, tree_name: str, relative: str
) -> None:
    with pytest.raises(ValueError):
        LayeredFSPackager(layout).destination(f"{tree_name}/{relative}", CONTEXT)


@pytest.mark.parametrize("layout", ["atmosphere", "flat"])
@pytest.mark.parametrize(
    "title_id",
    [
        "",
        TITLE_ID.lower(),
        TITLE_ID[:-1],
        TITLE_ID + "0",
        "G" * 16,
        "0x" + TITLE_ID[2:],
        TITLE_ID + "\n",
        " " + TITLE_ID[1:],
        TITLE_ID[:7] + "/" + TITLE_ID[8:],
        "../" + TITLE_ID[3:],
        None,
        int(TITLE_ID, 16),
    ],
)
def test_destination_rejects_a_title_id_that_is_not_16_uppercase_hex_digits(
    layout: str, title_id: object
) -> None:
    context = PackageContext(title_id=title_id, patchset_name=SET)  # type: ignore[arg-type]
    for logical in ("romfs/a.bin", "exefs/main", "exefs_patches/a.ips"):
        with pytest.raises(ValueError, match="title ID"):
            LayeredFSPackager(layout).destination(logical, context)


@pytest.mark.parametrize("layout", ["atmosphere", "flat"])
@pytest.mark.parametrize(
    "name",
    [
        "",
        ".hidden",
        "-dash",
        "_underscore",
        "..",
        "a" * 65,
        "has space",
        "a/b",
        "a\\b",
        "a:b",
        "a*b",
        "caf\u00e9",
        "name\n",
        None,
    ],
)
def test_destination_rejects_an_unsafe_patchset_name(layout: str, name: object) -> None:
    context = PackageContext(title_id=TITLE_ID, patchset_name=name)  # type: ignore[arg-type]
    for logical in ("romfs/a.bin", "exefs/main", "exefs_patches/a.ips"):
        with pytest.raises(ValueError, match="patch-set name"):
            LayeredFSPackager(layout).destination(logical, context)


# ----------------------------------------------------------------------------------
# package(): a first run
# ----------------------------------------------------------------------------------

#: A small build touching the three trees; an empty file and bytes that are not text.
BUILD: Mapping[str, bytes] = {
    "romfs/Data/UI/title.png": b"\x89PNG-ish\r\n\x00\xff",
    "romfs/Data/empty.bin": b"",
    "romfs/readme.txt": b"hello\n",
    "exefs/main": b"NSO-ish" * 9,
    "exefs_patches/AB12CD.ips": b"PATCH\x00\x01\x00\x00\x01\xaaEOF",
}


def test_first_run_atmosphere_layout(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staged = stage(staging, BUILD)
    out = tmp_path / "not" / "there" / "yet"

    result = LayeredFSPackager().package(staged, out, CONTEXT)

    # Code-point order: "D" sorts before "r", "contents" before "exefs_patches".
    expected_files = [
        f"atmosphere/contents/{TITLE_ID}/exefs/main",
        f"atmosphere/contents/{TITLE_ID}/romfs/Data/UI/title.png",
        f"atmosphere/contents/{TITLE_ID}/romfs/Data/empty.bin",
        f"atmosphere/contents/{TITLE_ID}/romfs/readme.txt",
        f"atmosphere/exefs_patches/{SET}/AB12CD.ips",
    ]
    assert isinstance(result, PackageResult)
    assert result.root == out
    assert result.files == expected_files
    assert result.removed == []
    assert result.warnings == []
    assert result.unchanged == 0
    assert result.linked + result.copied == 5
    assert package_tree(out) == {
        expected_files[0]: BUILD["exefs/main"],
        expected_files[1]: BUILD["romfs/Data/UI/title.png"],
        expected_files[2]: b"",
        expected_files[3]: b"hello\n",
        expected_files[4]: BUILD["exefs_patches/AB12CD.ips"],
    }
    assert directories(out) == {
        "atmosphere",
        "atmosphere/contents",
        f"atmosphere/contents/{TITLE_ID}",
        f"atmosphere/contents/{TITLE_ID}/exefs",
        f"atmosphere/contents/{TITLE_ID}/romfs",
        f"atmosphere/contents/{TITLE_ID}/romfs/Data",
        f"atmosphere/contents/{TITLE_ID}/romfs/Data/UI",
        "atmosphere/exefs_patches",
        f"atmosphere/exefs_patches/{SET}",
    }
    assert read_manifest_text(out) == manifest_text(expected_files)
    # The manifest is not a file of the package, and packaging never alters its input.
    assert MANIFEST_NAME not in result.files
    assert tree(staging) == dict(BUILD)


def test_first_run_flat_layout(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", BUILD)
    out = tmp_path / "mods"

    result = LayeredFSPackager(Layout.FLAT).package(staged, out, CONTEXT)

    # Executables and their patches share one folder in a mod folder.
    expected_files = [
        f"{SET}/exefs/AB12CD.ips",
        f"{SET}/exefs/main",
        f"{SET}/romfs/Data/UI/title.png",
        f"{SET}/romfs/Data/empty.bin",
        f"{SET}/romfs/readme.txt",
    ]
    assert result.files == expected_files
    assert (result.linked + result.copied, result.unchanged) == (5, 0)
    assert result.removed == [] and result.warnings == []
    assert package_tree(out) == {
        expected_files[0]: BUILD["exefs_patches/AB12CD.ips"],
        expected_files[1]: BUILD["exefs/main"],
        expected_files[2]: BUILD["romfs/Data/UI/title.png"],
        expected_files[3]: b"",
        expected_files[4]: b"hello\n",
    }
    assert read_manifest_text(out) == manifest_text(expected_files, layout="flat")


def test_files_are_placed_in_sorted_destination_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage(
        tmp_path / "staging",
        {"romfs/b.bin": b"b", "romfs/a/z.bin": b"z", "exefs/main": b"m", "romfs/Z.bin": b"Z"},
    )
    out = tmp_path / "out"
    real = fsutil.link_or_copy
    placed: list[str] = []

    def recording(source: Path, destination: Path) -> str:
        relative = os.path.relpath(fsutil.fs_path(destination), fsutil.fs_path(out))
        placed.append(relative.replace(os.sep, "/"))
        return real(source, destination)

    monkeypatch.setattr(fsutil, "link_or_copy", recording)
    result = LayeredFSPackager().package(staged, out, CONTEXT)
    # Plain code-point order of the destinations, whatever the order of the staged list:
    # "e" < "r", and "Z" < "a" < "b".
    assert placed == [
        f"{CONTENTS}/exefs/main",
        f"{CONTENTS}/romfs/Z.bin",
        f"{CONTENTS}/romfs/a/z.bin",
        f"{CONTENTS}/romfs/b.bin",
    ]
    assert result.files == placed


def test_non_ascii_names_survive_and_the_manifest_stays_ascii(tmp_path: Path) -> None:
    # Katakana and a sharp s: characters without a decomposed form, so that a filesystem
    # that normalizes names hands them back unchanged.
    relative = "テクスチャ/straße 01.bin"
    staged = stage(tmp_path / "staging", {f"romfs/{relative}": b"unicode"})
    out = tmp_path / "out"
    packager = LayeredFSPackager()

    result = packager.package(staged, out, CONTEXT)

    destination = f"{CONTENTS}/romfs/{relative}"
    assert result.files == [destination]
    assert package_tree(out) == {destination: b"unicode"}
    raw = (out / MANIFEST_NAME).read_bytes()
    assert raw.isascii()
    assert json.loads(raw) == {
        "schema": 1,
        "packager": "layeredfs",
        "layout": "atmosphere",
        "title_id": TITLE_ID,
        "files": [destination],
    }
    again = packager.package(staged, out, CONTEXT)
    assert (again.unchanged, again.linked + again.copied, again.removed) == (1, 0, [])
    assert packager.package([], out, CONTEXT).removed == [destination]
    assert tree(out) == {MANIFEST_NAME: manifest_text([]).encode()}


def test_empty_staged_list_into_a_new_folder(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = LayeredFSPackager().package([], out, CONTEXT)
    assert result == PackageResult(root=out)
    assert tree(out) == {MANIFEST_NAME: manifest_text([]).encode()}
    assert directories(out) == set()


def test_empty_staged_list_removes_the_previous_package(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", BUILD)
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    first = packager.package(staged, out, CONTEXT)
    put(out / "atmosphere" / "contents" / OTHER_TITLE_ID / "romfs" / "mod.bin", b"someone else's")

    result = packager.package([], out, CONTEXT)

    assert result.files == []
    assert result.removed == first.files
    assert (result.linked, result.copied, result.unchanged) == (0, 0, 0)
    assert result.warnings == []
    # Everything BEA made is gone, directories included; the other mod is intact.
    assert tree(out) == {
        MANIFEST_NAME: manifest_text([]).encode(),
        f"atmosphere/contents/{OTHER_TITLE_ID}/romfs/mod.bin": b"someone else's",
    }
    assert directories(out) == {
        "atmosphere",
        "atmosphere/contents",
        f"atmosphere/contents/{OTHER_TITLE_ID}",
        f"atmosphere/contents/{OTHER_TITLE_ID}/romfs",
    }
    assert tree(tmp_path / "staging") == dict(BUILD)


# ----------------------------------------------------------------------------------
# package(): later runs
# ----------------------------------------------------------------------------------


def test_second_run_with_the_same_input_changes_nothing(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", BUILD)
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    first = packager.package(staged, out, CONTEXT)
    before = tree(out)
    # An old, recognizable time stamp: if the manifest were written again it would change.
    long_ago = 1_000_000_000 * 10**9
    os.utime(out / MANIFEST_NAME, ns=(long_ago, long_ago))

    second = packager.package(staged, out, CONTEXT)

    assert second.files == first.files
    assert (second.linked, second.copied, second.unchanged) == (0, 0, 5)
    assert second.removed == []
    assert second.warnings == []
    assert tree(out) == before
    assert before[MANIFEST_NAME] == manifest_text(first.files).encode()
    assert os.stat(out / MANIFEST_NAME).st_mtime_ns == long_ago
    # ... and the same holds for a different packager object and a third run.
    third = LayeredFSPackager("atmosphere").package(list(reversed(staged)), out, CONTEXT)
    assert (third.linked, third.copied, third.unchanged) == (0, 0, 5)
    assert tree(out) == before
    assert os.stat(out / MANIFEST_NAME).st_mtime_ns == long_ago


def test_second_run_removes_updates_and_adds_without_touching_foreign_files(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    out = tmp_path / "out"
    romfs = f"{CONTENTS}/romfs"
    packager = LayeredFSPackager()
    staged = stage(
        staging,
        {
            "romfs/keep/same.bin": b"same",
            "romfs/keep/changed.bin": b"before",
            "romfs/gone/deep/er/stale.bin": b"stale 1",
            "romfs/shared/stale.bin": b"stale 2",
            "exefs_patches/OLD.ips": b"old patch",
        },
    )
    first = packager.package(staged, out, CONTEXT)
    assert len(first.files) == 5

    # What a user may have next to (and inside) the package.
    foreign = {
        "notes.txt": b"my notes",
        f"{romfs}/shared/user.bin": b"next to a stale file",
        f"atmosphere/contents/{OTHER_TITLE_ID}/romfs/mod.bin": b"another mod",
        f"atmosphere/exefs_patches/other-set/{'AB' * 4}.ips": b"another patch",
    }
    for relative, data in foreign.items():
        put(fsutil.resolve_relative(out, relative), data)
    empty_dirs = ["empty-root-dir", "atmosphere/config", f"{romfs}/keep/empty-user-dir"]
    for relative in empty_dirs:
        fsutil.resolve_relative(out, relative).mkdir(parents=True)

    # The pipeline leaves an up-to-date staged file alone and rewrites the others.
    same = next(item for item in staged if item.logical == "romfs/keep/same.bin")
    staged = [
        same,
        *stage(
            staging,
            {"romfs/keep/changed.bin": b"after, and longer", "romfs/new/added.bin": b"added"},
        ),
    ]

    second = packager.package(staged, out, CONTEXT)

    assert second.files == [
        f"{romfs}/keep/changed.bin",
        f"{romfs}/keep/same.bin",
        f"{romfs}/new/added.bin",
    ]
    assert second.removed == [
        f"{romfs}/gone/deep/er/stale.bin",
        f"{romfs}/shared/stale.bin",
        f"atmosphere/exefs_patches/{SET}/OLD.ips",
    ]
    assert (second.unchanged, second.linked + second.copied) == (1, 2)
    assert second.warnings == []
    assert package_tree(out) == {
        f"{romfs}/keep/changed.bin": b"after, and longer",
        f"{romfs}/keep/same.bin": b"same",
        f"{romfs}/new/added.bin": b"added",
        **foreign,
    }
    # Directories emptied by the clean-up are gone; the user's are there, even the empty ones.
    assert directories(out) == {
        "empty-root-dir",
        "atmosphere",
        "atmosphere/config",
        "atmosphere/contents",
        f"atmosphere/contents/{TITLE_ID}",
        romfs,
        f"{romfs}/keep",
        f"{romfs}/keep/empty-user-dir",
        f"{romfs}/new",
        f"{romfs}/shared",
        f"atmosphere/contents/{OTHER_TITLE_ID}",
        f"atmosphere/contents/{OTHER_TITLE_ID}/romfs",
        "atmosphere/exefs_patches",
        "atmosphere/exefs_patches/other-set",
    }
    assert read_manifest_text(out) == manifest_text(second.files)


def test_switching_the_layout_moves_the_package(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", BUILD)
    out = tmp_path / "out"
    put(out / "atmosphere" / "config" / "user.ini", b"[user]")
    atmosphere = LayeredFSPackager("atmosphere").package(staged, out, CONTEXT)

    flat = LayeredFSPackager("flat").package(staged, out, CONTEXT)

    assert flat.files == [
        f"{SET}/exefs/AB12CD.ips",
        f"{SET}/exefs/main",
        f"{SET}/romfs/Data/UI/title.png",
        f"{SET}/romfs/Data/empty.bin",
        f"{SET}/romfs/readme.txt",
    ]
    assert flat.removed == atmosphere.files
    assert (flat.unchanged, flat.linked + flat.copied) == (0, 5)
    assert flat.warnings == []
    assert sorted(package_tree(out)) == sorted([*flat.files, "atmosphere/config/user.ini"])
    # Of the atmosphere tree only what the user put there is left.
    assert {name for name in directories(out) if name.startswith("atmosphere")} == {
        "atmosphere",
        "atmosphere/config",
    }
    assert read_manifest_text(out) == manifest_text(flat.files, layout="flat")


def test_packaging_another_title_replaces_the_package_and_says_so(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    first = packager.package(stage(staging / "one", {"romfs/a.bin": b"one"}), out, CONTEXT)
    other = PackageContext(title_id=OTHER_TITLE_ID, patchset_name=SET)

    second = packager.package(stage(staging / "two", {"romfs/a.bin": b"two"}), out, other)

    destination = f"atmosphere/contents/{OTHER_TITLE_ID}/romfs/a.bin"
    assert second.files == [destination]
    assert second.removed == first.files == [f"{CONTENTS}/romfs/a.bin"]
    assert package_tree(out) == {destination: b"two"}
    assert read_manifest_text(out) == manifest_text([destination], title_id=OTHER_TITLE_ID)
    assert len(second.warnings) == 1
    assert TITLE_ID in second.warnings[0] and OTHER_TITLE_ID in second.warnings[0]
    # Packaging the same title again is the normal case and warns about nothing.
    third = packager.package(stage(staging / "two", {"romfs/b.bin": b"three"}), out, other)
    assert third.removed == [destination]
    assert third.warnings == []


def test_a_path_that_only_changes_letter_case_is_renamed(tmp_path: Path) -> None:
    # Two staging trees: on a case-insensitive disk the two spellings are one path.
    old = stage(tmp_path / "staging-old", {"romfs/Data/Tex.bin": b"old"})
    new = stage(tmp_path / "staging-new", {"romfs/data/tex.bin": b"new content"})
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    packager.package(old, out, CONTEXT)

    result = packager.package(new, out, CONTEXT)

    assert result.files == [f"{CONTENTS}/romfs/data/tex.bin"]
    assert result.removed == [f"{CONTENTS}/romfs/Data/Tex.bin"]
    assert (result.unchanged, result.linked + result.copied) == (0, 1)
    # Whatever the filesystem thinks of letter case, exactly the new spelling is on disk:
    # had the new file been written before the old one was deleted, a case-insensitive
    # disk would have lost it.
    romfs = out / "atmosphere" / "contents" / TITLE_ID / "romfs"
    assert os.listdir(romfs) == ["data"]
    assert os.listdir(romfs / "data") == ["tex.bin"]
    assert (romfs / "data" / "tex.bin").read_bytes() == b"new content"
    assert read_manifest_text(out) == manifest_text(result.files)


def test_a_file_can_become_a_directory_and_a_file_again(tmp_path: Path) -> None:
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    node = f"{CONTENTS}/romfs/node"
    as_file = stage(tmp_path / "staging-1", {"romfs/node": b"a file"})
    as_directory = stage(tmp_path / "staging-2", {"romfs/node/inner/leaf.bin": b"a leaf"})

    packager.package(as_file, out, CONTEXT)
    assert package_tree(out) == {node: b"a file"}

    second = packager.package(as_directory, out, CONTEXT)
    assert second.removed == [node]
    assert package_tree(out) == {f"{node}/inner/leaf.bin": b"a leaf"}

    third = packager.package(as_file, out, CONTEXT)
    assert third.removed == [f"{node}/inner/leaf.bin"]
    assert package_tree(out) == {node: b"a file"}
    assert f"{node}/inner" not in directories(out)
    assert read_manifest_text(out) == manifest_text([node])


def test_destinations_beyond_260_characters(tmp_path: Path) -> None:
    # Seven long folders, as deep engine paths are: well past the classic Windows limit.
    relative = "/".join(f"level{index}_" + "x" * 40 for index in range(7)) + "/texture.bin"
    staging = tmp_path / "staging"
    staged = stage(staging, {f"romfs/{relative}": b"deep payload", "romfs/short.bin": b"short"})
    out = tmp_path / "out"
    deep = f"{CONTENTS}/romfs/{relative}"
    short = f"{CONTENTS}/romfs/short.bin"
    assert len(str(fsutil.resolve_relative(out, deep))) > 300
    assert len(str(staged[0].path)) > 300
    packager = LayeredFSPackager()

    first = packager.package(staged, out, CONTEXT)
    assert first.files == [deep, short]
    assert (first.linked + first.copied, first.unchanged) == (2, 0)
    assert package_tree(out) == {deep: b"deep payload", short: b"short"}
    assert read_manifest_text(out) == manifest_text([deep, short])

    second = packager.package(staged, out, CONTEXT)
    assert (second.linked, second.copied, second.unchanged) == (0, 0, 2)

    # A changed deep file is replaced, then removed together with its seven folders.
    fsutil.atomic_write_bytes(staged[0].path, b"deep payload, second edition")
    third = packager.package(staged, out, CONTEXT)
    assert (third.linked + third.copied, third.unchanged) == (1, 1)
    assert package_tree(out)[deep] == b"deep payload, second edition"

    fourth = packager.package(staged[1:], out, CONTEXT)
    assert fourth.removed == [deep]
    assert package_tree(out) == {short: b"short"}
    assert directories(out) == {
        "atmosphere",
        "atmosphere/contents",
        f"atmosphere/contents/{TITLE_ID}",
        f"atmosphere/contents/{TITLE_ID}/romfs",
    }


# ----------------------------------------------------------------------------------
# package(): recognizing files that are already in place
# ----------------------------------------------------------------------------------


def test_copies_are_recognized_by_size_and_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_hard_links(source: object, destination: object) -> None:
        raise OSError(errno.EPERM, "hard links are not supported here")

    monkeypatch.setattr(os, "link", no_hard_links)
    staged = stage(tmp_path / "staging", BUILD)
    out = tmp_path / "out"
    packager = LayeredFSPackager()

    first = packager.package(staged, out, CONTEXT)
    assert (first.linked, first.copied, first.unchanged) == (0, 5, 0)
    target = out / "atmosphere" / "contents" / TITLE_ID / "exefs" / "main"
    assert target.read_bytes() == BUILD["exefs/main"]

    second = packager.package(staged, out, CONTEXT)
    assert (second.linked, second.copied, second.unchanged) == (0, 0, 5)


def test_a_file_with_the_same_size_and_time_is_left_alone(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"staged!"})
    out = tmp_path / "out"
    # Not a link to the staged file and not even the same bytes: only the fingerprint
    # (size and modification time) says "already in place", and that is the rule.
    target = put(out / "atmosphere" / "contents" / TITLE_ID / "romfs" / "a.bin", b"inplace")
    assert len(b"inplace") == len(b"staged!")
    source = os.stat(staged[0].path)
    os.utime(target, ns=(source.st_atime_ns, source.st_mtime_ns))

    result = LayeredFSPackager().package(staged, out, CONTEXT)

    assert (result.unchanged, result.linked, result.copied) == (1, 0, 0)
    assert target.read_bytes() == b"inplace"
    assert result.files == [f"{CONTENTS}/romfs/a.bin"]


@pytest.mark.parametrize(
    ("content", "shift_ns"),
    [
        (b"inplace", 2_000_000_000),  # same size, another time
        (b"inplace", -2_000_000_000),
        (b"in place, but longer", 0),  # same time, another size
        (b"", 0),
    ],
)
def test_a_file_with_another_size_or_time_is_replaced(
    tmp_path: Path, content: bytes, shift_ns: int
) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"staged!"})
    out = tmp_path / "out"
    target = put(out / "atmosphere" / "contents" / TITLE_ID / "romfs" / "a.bin", content)
    source = os.stat(staged[0].path)
    os.utime(target, ns=(source.st_atime_ns, source.st_mtime_ns + shift_ns))

    result = LayeredFSPackager().package(staged, out, CONTEXT)

    assert (result.unchanged, result.linked + result.copied) == (0, 1)
    assert target.read_bytes() == b"staged!"
    assert result.removed == []


class _NoIdentity:
    """A ``stat`` result as some network and virtual filesystems report it: no file ID."""

    st_ino = 0
    st_dev = 0

    def __init__(self, real: os.stat_result) -> None:
        self._real = real

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_filesystems_without_file_ids_do_not_make_every_file_look_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"first"})
    out = tmp_path / "out"
    packager = LayeredFSPackager()
    packager.package(staged, out, CONTEXT)
    fsutil.atomic_write_bytes(staged[0].path, b"second, longer")
    real_stat = os.stat

    def stat_without_identity(*args: object, **kwargs: object) -> object:
        return _NoIdentity(real_stat(*args, **kwargs))  # type: ignore[arg-type]

    # With an inode number of zero for every file, "same inode" means nothing.
    monkeypatch.setattr(os, "stat", stat_without_identity)
    changed = packager.package(staged, out, CONTEXT)
    again = packager.package(staged, out, CONTEXT)
    monkeypatch.undo()

    assert (changed.unchanged, changed.linked + changed.copied) == (0, 1)
    assert package_tree(out) == {f"{CONTENTS}/romfs/a.bin": b"second, longer"}
    assert (again.unchanged, again.linked + again.copied) == (1, 0)


# ----------------------------------------------------------------------------------
# package(): input that is refused before anything is touched
# ----------------------------------------------------------------------------------


def test_two_files_with_the_same_destination_are_refused(tmp_path: Path) -> None:
    # In a mod folder an executable and a patch of the same name would be one file.
    staged = stage(tmp_path / "staging", {"exefs/AB12.ips": b"1", "exefs_patches/AB12.ips": b"2"})
    out = tmp_path / "out"

    with pytest.raises(OutputError) as caught:
        LayeredFSPackager("flat").package(staged, out, CONTEXT)

    message = str(caught.value)
    assert "'exefs/AB12.ips'" in message
    assert "'exefs_patches/AB12.ips'" in message
    assert f"{SET}/exefs/AB12.ips" in message
    assert not out.exists()
    # The atmosphere layout keeps patches in their own folder: no conflict there.
    result = LayeredFSPackager("atmosphere").package(staged, out, CONTEXT)
    assert result.files == [
        f"{CONTENTS}/exefs/AB12.ips",
        f"atmosphere/exefs_patches/{SET}/AB12.ips",
    ]


def test_the_same_logical_path_twice_is_refused(tmp_path: Path) -> None:
    one = stage(tmp_path / "staging-1", {"romfs/a.bin": b"1"})
    two = stage(tmp_path / "staging-2", {"romfs/a.bin": b"2"})
    out = tmp_path / "out"
    for layout in ("atmosphere", "flat"):
        with pytest.raises(OutputError, match=re.escape("romfs/a.bin")):
            LayeredFSPackager(layout).package(one + two, out, CONTEXT)
        with pytest.raises(OutputError):
            LayeredFSPackager(layout).package(one + one, out, CONTEXT)
    assert not out.exists()


@pytest.mark.parametrize("layout", ["atmosphere", "flat"])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("romfs/data/Tex.BIN", "romfs/data/tex.bin"),
        ("romfs/Data/tex.bin", "romfs/data/tex.bin"),
        ("romfs/DATA/UI/a.bin", "romfs/data/ui/A.bin"),
        ("exefs/Main", "exefs/main"),
        ("exefs_patches/ab12.ips", "exefs_patches/AB12.IPS"),
        ("romfs/Écran.bin", "romfs/écran.bin"),
        # A name with a sharp s (whose Unicode uppercase is two letters) still collides
        # when the rest differs only by case.
        ("romfs/straße/A.bin", "romfs/straße/a.bin"),
    ],
)
def test_destinations_that_differ_only_by_letter_case_are_refused(
    tmp_path: Path, layout: str, first: str, second: str
) -> None:
    # Separate staging trees: on a case-insensitive disk the two would be one file.
    staged = stage(tmp_path / "staging-1", {first: b"1"})
    staged += stage(tmp_path / "staging-2", {second: b"2"})
    out = tmp_path / "out"

    with pytest.raises(OutputError) as caught:
        LayeredFSPackager(layout).package(staged, out, CONTEXT)

    message = str(caught.value)
    assert repr(first) in message
    assert repr(second) in message
    assert "case" in message
    assert not out.exists()
    # Each of them alone is fine.
    for item in staged:
        assert len(LayeredFSPackager(layout).package([item], tmp_path / "ok", CONTEXT).files) == 1


def test_names_that_a_fat_volume_keeps_apart_are_not_a_collision(tmp_path: Path) -> None:
    # Unicode uppercases a sharp s to "SS", but FAT/exFAT compare names one character at
    # a time: these are two different files on an SD card.
    staged = stage(tmp_path / "staging-1", {"romfs/straße.bin": b"1"})
    staged += stage(tmp_path / "staging-2", {"romfs/STRASSE.bin": b"2"})
    result = LayeredFSPackager().package(staged, tmp_path / "out", CONTEXT)
    assert result.files == [
        f"{CONTENTS}/romfs/STRASSE.bin",
        f"{CONTENTS}/romfs/straße.bin",
    ]


@pytest.mark.parametrize(
    ("as_file", "below_it"),
    [
        ("romfs/data", "romfs/data/inner.bin"),
        ("romfs/Data", "romfs/data/deep/er/inner.bin"),
        ("exefs/main", "exefs/main/extra"),
    ],
)
def test_a_file_cannot_also_be_a_directory(tmp_path: Path, as_file: str, below_it: str) -> None:
    staged = stage(tmp_path / "staging-1", {as_file: b"file"})
    staged += stage(tmp_path / "staging-2", {below_it: b"below"})
    out = tmp_path / "out"
    for order in (staged, staged[::-1]):
        with pytest.raises(OutputError) as caught:
            LayeredFSPackager().package(order, out, CONTEXT)
        message = str(caught.value)
        assert repr(as_file) in message
        assert repr(below_it) in message
        assert "directory" in message
    assert not out.exists()


def test_invalid_logical_path_or_context_is_refused_before_anything_is_touched(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    good = stage(tmp_path / "staging", {"romfs/a.bin": b"a"})
    bad = [StagedFile("romfs/../../escape.bin", good[0].path, 1)]
    unknown = [StagedFile("sdmc/a.bin", good[0].path, 1)]
    packager = LayeredFSPackager()
    with pytest.raises(ValueError):
        packager.package(good + bad, out, CONTEXT)
    with pytest.raises(ValueError):
        packager.package(good + unknown, out, CONTEXT)
    # The names of the context are checked even when there is no file to place.
    with pytest.raises(ValueError, match="title ID"):
        packager.package([], out, PackageContext(TITLE_ID.lower(), SET))
    with pytest.raises(ValueError, match="patch-set name"):
        packager.package([], out, PackageContext(TITLE_ID, "../escape"))
    assert not out.exists()
    assert not (tmp_path / "escape.bin").exists()


def test_missing_staged_file_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"a", "romfs/b.bin": b"b"})
    os.remove(staged[1].path)
    out = tmp_path / "out"

    with pytest.raises(OutputError) as caught:
        LayeredFSPackager().package(staged, out, CONTEXT)

    assert "romfs/b.bin" in str(caught.value)
    assert str(staged[1].path) in str(caught.value)
    assert not out.exists()


def test_staged_path_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "staging" / "romfs" / "folder"
    folder.mkdir(parents=True)
    out = tmp_path / "out"
    with pytest.raises(OutputError, match="not a regular file"):
        LayeredFSPackager().package([StagedFile("romfs/folder", folder, 0)], out, CONTEXT)
    assert not out.exists()


def test_out_dir_that_is_a_file_is_refused(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"a"})
    out = put(tmp_path / "out", b"a file, not a folder")
    with pytest.raises(OutputError) as caught:
        LayeredFSPackager().package(staged, out, CONTEXT)
    assert str(out) in str(caught.value)
    assert "not a directory" in str(caught.value)
    assert out.read_bytes() == b"a file, not a folder"


def test_out_dir_below_a_file_is_refused(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"a"})
    blocker = put(tmp_path / "blocker", b"x")
    with pytest.raises(OutputError) as caught:
        LayeredFSPackager().package(staged, blocker / "out", CONTEXT)
    assert str(blocker / "out") in str(caught.value)
    assert blocker.read_bytes() == b"x"


def test_manifest_name_taken_by_a_directory_is_refused(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"a"})
    out = tmp_path / "out"
    put(out / MANIFEST_NAME / "inside.txt", b"user data")
    with pytest.raises(OutputError, match=MANIFEST_NAME.replace(".", r"\.")):
        LayeredFSPackager().package(staged, out, CONTEXT)
    # Without a manifest BEA could not remember what it created, so it creates nothing.
    assert tree(out) == {f"{MANIFEST_NAME}/inside.txt": b"user data"}


def test_a_directory_in_the_way_of_a_file_is_never_removed(tmp_path: Path) -> None:
    staged = stage(tmp_path / "staging", {"romfs/a.bin": b"a"})
    out = tmp_path / "out"
    destination = f"{CONTENTS}/romfs/a.bin"
    put(fsutil.resolve_relative(out, f"{destination}/user.txt"), b"mine")
    packager = LayeredFSPackager()

    with pytest.raises(OutputError, match="directory"):
        packager.package(staged, out, CONTEXT)
    assert package_tree(out) == {f"{destination}/user.txt": b"mine"}

    # The interrupted run listed the path as its own; a later run must still see that
    # it is the user's directory and keep its hands off.
    assert destination in json.loads(read_manifest_text(out))["files"]
    result = packager.package([], out, CONTEXT)
    assert result.removed == []
    assert len(result.warnings) == 1 and "directory" in result.warnings[0]
    assert package_tree(out) == {f"{destination}/user.txt": b"mine"}
    assert read_manifest_text(out) == manifest_text([])
