"""Tests for bea.fsutil: long paths, atomic writes, deterministic walks."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bea import fsutil

IS_WINDOWS = sys.platform == "win32"


def link_directory(link: Path, target: Path) -> None:
    """Make ``link`` a directory link to ``target``, or skip the test when that is impossible."""
    if IS_WINDOWS:
        # A junction needs no privilege, unlike a symbolic link.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            pytest.skip("cannot create a directory junction here")
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symbolic links here")


def unlink_directory(link: Path) -> None:
    """Remove a link made by :func:`link_directory` (the link, never what it points at)."""
    with contextlib.suppress(OSError):
        if IS_WINDOWS:
            os.rmdir(link)
        else:
            os.unlink(link)


def names_in(directory: Path) -> list[str]:
    """Return what a directory holds, temporary leftovers included."""
    return sorted(os.listdir(fsutil.fs_path(directory)))


def test_fs_path_is_absolute_and_stable(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.bin"
    converted = fsutil.fs_path(target)
    assert os.path.isabs(converted)
    assert fsutil.fs_path(converted) == converted
    relative = fsutil.fs_path("some/relative/file.bin")
    assert os.path.isabs(relative)


@pytest.mark.skipif(
    sys.platform != "win32", reason="extended-length prefixes are a Windows concept"
)
def test_fs_path_prefixes_long_windows_paths(tmp_path: Path) -> None:
    long_path = tmp_path / ("d" * 120) / ("e" * 120) / "f.bin"
    converted = fsutil.fs_path(long_path)
    assert converted.startswith("\\\\?\\")
    assert fsutil.fs_path(converted) == converted
    assert not fsutil.fs_path(tmp_path).startswith("\\\\?\\") or len(str(tmp_path)) >= 200
    assert fsutil.fs_path("\\\\server\\share\\" + "x" * 250).startswith(
        "\\\\?\\UNC\\server\\share\\"
    )


def test_fs_path_normalizes_like_abspath(tmp_path: Path) -> None:
    plain = fsutil.fs_path(tmp_path / "b" / "c.bin")
    assert fsutil.fs_path(tmp_path / "a" / ".." / "b" / "." / "c.bin") == plain
    assert fsutil.fs_path(str(tmp_path / "b" / "c.bin")) == plain
    assert fsutil.fs_path(str(tmp_path) + os.sep) == fsutil.fs_path(tmp_path)


@pytest.mark.skipif(IS_WINDOWS, reason="only Windows has a path length limit to work around")
def test_fs_path_is_plain_abspath_outside_windows(tmp_path: Path) -> None:
    long_path = tmp_path / ("d" * 120) / ("e" * 120) / "f.bin"
    assert fsutil.fs_path(long_path) == str(long_path)
    assert fsutil.fs_path(long_path, force=True) == str(long_path)
    assert fsutil.fs_path(tmp_path, force=True) == str(tmp_path)


@pytest.mark.skipif(not IS_WINDOWS, reason="extended-length prefixes are a Windows concept")
def test_fs_path_force_prefixes_short_windows_paths(tmp_path: Path) -> None:
    # A walk starts from a short root and builds longer paths below it.
    forced = fsutil.fs_path(tmp_path, force=True)
    assert forced == "\\\\?\\" + str(tmp_path)
    assert fsutil.fs_path(forced, force=True) == forced
    assert fsutil.fs_path(forced) == forced
    assert os.path.samefile(forced, tmp_path)
    assert fsutil.fs_path("\\\\server\\share\\dir", force=True) == "\\\\?\\UNC\\server\\share\\dir"
    assert fsutil.fs_path("//server/share/dir", force=True) == "\\\\?\\UNC\\server\\share\\dir"


@pytest.mark.skipif(not IS_WINDOWS, reason="device paths are a Windows concept")
def test_fs_path_leaves_windows_device_paths_alone() -> None:
    # \\.\ already names a device namespace: it is not a server called ".".
    device = "\\\\.\\C:\\" + "x" * 250
    assert fsutil.fs_path(device) == device
    assert fsutil.fs_path(device, force=True) == device
    assert fsutil.fs_path("\\\\.\\pipe\\bea", force=True) == "\\\\.\\pipe\\bea"


def test_ensure_dir_creates_parents_and_accepts_existing_directories(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    fsutil.ensure_dir(target)
    assert target.is_dir()
    (target / "keep.bin").write_bytes(b"x")
    fsutil.ensure_dir(target)
    fsutil.ensure_dir(str(target))
    assert names_in(target) == ["keep.bin"]


def test_ensure_dir_refuses_a_file_in_the_way(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        fsutil.ensure_dir(tmp_path / "plain.txt")
    with pytest.raises(OSError):
        fsutil.ensure_dir(tmp_path / "plain.txt" / "below")
    assert (tmp_path / "plain.txt").read_bytes() == b"x"


def test_remove_file_reports_whether_something_was_deleted(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")
    assert fsutil.remove_file(target) is True
    assert not target.exists()
    assert fsutil.remove_file(target) is False
    assert fsutil.remove_file(str(target)) is False
    assert fsutil.remove_file(tmp_path / "no-such-folder" / "a.bin") is False


def test_remove_file_below_a_file_has_nothing_to_delete(tmp_path: Path) -> None:
    # A stale path whose folder has since become a file: there is no such file any more.
    (tmp_path / "plain.txt").write_bytes(b"x")
    assert fsutil.remove_file(tmp_path / "plain.txt" / "child.bin") is False
    assert (tmp_path / "plain.txt").read_bytes() == b"x"


def test_remove_file_treats_a_non_directory_parent_as_nothing_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What POSIX answers for the situation above (Windows says "path not found").
    def not_a_directory(path: str) -> None:
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)

    monkeypatch.setattr(os, "remove", not_a_directory)
    assert fsutil.remove_file(tmp_path / "plain.txt" / "child.bin") is False


def test_remove_file_does_not_hide_other_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", path)

    (tmp_path / "a.bin").write_bytes(b"x")
    monkeypatch.setattr(os, "remove", denied)
    with pytest.raises(PermissionError):
        fsutil.remove_file(tmp_path / "a.bin")


def test_deep_tree_beyond_260_characters_round_trips(tmp_path: Path) -> None:
    # Typical of <out>/atmosphere/contents/<id>/romfs/<engine path>: well past MAX_PATH.
    relative = "/".join(["segment_" + "x" * 40] * 7) + "/texture.bin"
    target = fsutil.resolve_relative(tmp_path, relative)
    assert len(str(target)) > 300
    fsutil.atomic_write_bytes(target, b"payload")
    assert Path(fsutil.fs_path(target)).read_bytes() == b"payload"
    assert fsutil.sha256_file(target) == hashlib.sha256(b"payload").hexdigest()

    walked = list(fsutil.walk_files(tmp_path))
    assert [(entry.path, entry.size) for entry in walked] == [(relative, 7)]

    copy = fsutil.resolve_relative(tmp_path / "copy", relative)
    assert fsutil.link_or_copy(target, copy) in ("link", "copy")
    assert Path(fsutil.fs_path(copy)).read_bytes() == b"payload"
    assert fsutil.remove_file(copy)
    assert not fsutil.remove_file(copy)
    assert fsutil.prune_empty_dirs(tmp_path / "copy") == 7


def test_atomic_output_replaces_only_on_success(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "out.png"
    with fsutil.atomic_output(target) as temporary:
        assert temporary.suffix == ".png"  # tools that sniff the extension keep working
        assert temporary.parent == Path(fsutil.fs_path(target)).parent
        assert temporary != Path(fsutil.fs_path(target))
        temporary.write_bytes(b"first")
        assert not target.exists()
    assert target.read_bytes() == b"first"

    with pytest.raises(RuntimeError), fsutil.atomic_output(target) as temporary:
        temporary.write_bytes(b"second")
        raise RuntimeError("boom")
    assert target.read_bytes() == b"first"
    assert sorted(path.name for path in target.parent.iterdir()) == ["out.png"]


def test_atomic_output_cleans_up_when_nothing_was_written(tmp_path: Path) -> None:
    target = tmp_path / "never.bin"
    with pytest.raises(FileNotFoundError), fsutil.atomic_output(target):
        pass  # the block "succeeded" but produced no file: the rename must fail loudly
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("out.png", ".png"),
        ("archive.tar.gz", ".gz"),
        ("data.v1.2.BIN", ".BIN"),
        (".config.yml", ".yml"),
        ("README", None),
        (".gitignore", None),
    ],
)
def test_atomic_output_temporary_is_a_hidden_sibling(
    tmp_path: Path, name: str, extension: str | None
) -> None:
    target = tmp_path / name
    with fsutil.atomic_output(target) as temporary:
        assert temporary.parent == Path(fsutil.fs_path(target)).parent
        assert temporary.name != name
        if extension is not None:
            assert temporary.suffix == extension
        temporary.write_bytes(b"work in progress")
        # Until the block ends the file is there, but a walk does not report it.
        assert len(names_in(tmp_path)) == 1
        assert list(fsutil.walk_files(tmp_path)) == []
    assert names_in(tmp_path) == [name]
    assert [entry.path for entry in fsutil.walk_files(tmp_path)] == [name]


def test_atomic_output_shows_the_old_content_until_the_block_ends(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    with fsutil.atomic_output(target) as temporary:
        temporary.write_bytes(b"new and longer")
        assert target.read_bytes() == b"old"
    assert target.read_bytes() == b"new and longer"
    assert names_in(tmp_path) == ["out.bin"]


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, ValueError, OSError])
def test_atomic_output_cleans_up_whatever_interrupts_the_block(
    tmp_path: Path, error: type[BaseException]
) -> None:
    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    with pytest.raises(error), fsutil.atomic_output(target) as temporary:
        temporary.write_bytes(b"half written")
        raise error()
    assert target.read_bytes() == b"old"
    assert names_in(tmp_path) == ["out.bin"]


def test_atomic_output_handles_names_close_to_the_length_limit(tmp_path: Path) -> None:
    # 250 characters: a legal file name that leaves no room for a longer sibling name.
    name = "n" * 246 + ".bin"
    target = tmp_path / name
    try:
        Path(fsutil.fs_path(target)).write_bytes(b"probe")
    except OSError:
        pytest.skip("this filesystem does not take 250-character names")
    assert fsutil.remove_file(target)

    with fsutil.atomic_output(target) as temporary:
        assert temporary.suffix == ".bin"
        assert temporary.name != name
        assert len(temporary.name) <= 255
        temporary.write_bytes(b"payload")
        assert list(fsutil.walk_files(tmp_path)) == []
    assert Path(fsutil.fs_path(target)).read_bytes() == b"payload"
    assert names_in(tmp_path) == [name]

    # A failed write leaves nothing behind either.
    with pytest.raises(RuntimeError), fsutil.atomic_output(target) as temporary:
        temporary.write_bytes(b"half")
        raise RuntimeError("boom")
    assert names_in(tmp_path) == [name]
    assert fsutil.link_or_copy(target, tmp_path / "copy" / name) in ("link", "copy")
    assert names_in(tmp_path / "copy") == [name]


def test_atomic_output_handles_long_non_ascii_names(tmp_path: Path) -> None:
    # 80 three-byte characters: 244 bytes with the extension, under the 255-byte limit of
    # POSIX filesystems, where the temporary sibling has to fit as well.
    name = "あ" * 80 + ".bin"
    target = tmp_path / name
    try:
        Path(fsutil.fs_path(target)).write_bytes(b"probe")
    except OSError:
        pytest.skip("this filesystem does not take such a name")
    assert fsutil.remove_file(target)

    with fsutil.atomic_output(target) as temporary:
        assert temporary.suffix == ".bin"
        assert len(temporary.name.encode("utf-8")) <= 255
        temporary.write_bytes(b"payload")
    assert Path(fsutil.fs_path(target)).read_bytes() == b"payload"
    assert names_in(tmp_path) == [name]


def test_replacing_a_directory_fails_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory in the way is not a transient lock: waiting for it would only stall.
    (tmp_path / "taken").mkdir()
    naps: list[float] = []
    monkeypatch.setattr(fsutil.time, "sleep", naps.append)
    with pytest.raises(OSError):
        fsutil.atomic_write_bytes(tmp_path / "taken", b"data")
    assert naps == []
    assert (tmp_path / "taken").is_dir()
    assert names_in(tmp_path) == ["taken"]


def test_final_rename_rides_out_a_transient_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    refusals = [PermissionError("locked"), PermissionError("locked")]

    def locked_at_first(source: Path, destination: Path) -> None:
        if refusals:
            raise refusals.pop()
        real_replace(source, destination)

    naps: list[float] = []
    monkeypatch.setattr(os, "replace", locked_at_first)
    monkeypatch.setattr(fsutil, "_REPLACE_ATTEMPTS", 5)
    monkeypatch.setattr(fsutil.time, "sleep", naps.append)
    fsutil.atomic_write_bytes(tmp_path / "out.bin", b"data")
    assert (tmp_path / "out.bin").read_bytes() == b"data"
    assert len(naps) == 2
    assert all(nap > 0 for nap in naps)
    assert names_in(tmp_path) == ["out.bin"]


def test_final_rename_gives_up_on_a_lock_that_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_locked(source: Path, destination: Path) -> None:
        raise PermissionError("locked")

    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    naps: list[float] = []
    monkeypatch.setattr(os, "replace", always_locked)
    monkeypatch.setattr(fsutil, "_REPLACE_ATTEMPTS", 4)
    monkeypatch.setattr(fsutil.time, "sleep", naps.append)
    with pytest.raises(PermissionError):
        fsutil.atomic_write_bytes(target, b"new")
    assert len(naps) == 3  # four attempts: no nap after the last one
    assert target.read_bytes() == b"old"
    assert names_in(tmp_path) == ["out.bin"]


def test_atomic_write_bytes_accepts_any_bytes_like_object(tmp_path: Path) -> None:
    fsutil.atomic_write_bytes(tmp_path / "bytes.bin", b"\x00\x01\xff")
    fsutil.atomic_write_bytes(tmp_path / "array.bin", bytearray(b"\x00\x01\xff"))
    fsutil.atomic_write_bytes(tmp_path / "view.bin", memoryview(b"__\x00\x01\xff__")[2:5])
    fsutil.atomic_write_bytes(str(tmp_path / "empty.bin"), b"")
    for name in ("bytes.bin", "array.bin", "view.bin"):
        assert (tmp_path / name).read_bytes() == b"\x00\x01\xff"
    assert (tmp_path / "empty.bin").read_bytes() == b""
    # Overwriting replaces the whole file, even with something shorter.
    fsutil.atomic_write_bytes(tmp_path / "bytes.bin", b"z")
    assert (tmp_path / "bytes.bin").read_bytes() == b"z"


def test_atomic_write_text_uses_lf_and_utf8(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    fsutil.atomic_write_text(target, "linha 1\nação\n")
    assert target.read_bytes() == "linha 1\nação\n".encode()


def test_atomic_write_text_never_translates_line_endings(tmp_path: Path) -> None:
    # The caller decides: a config file that came with CRLF must leave with CRLF.
    target = tmp_path / "mixed.txt"
    fsutil.atomic_write_text(target, "unix\nwindows\r\nold mac\r")
    assert target.read_bytes() == b"unix\nwindows\r\nold mac\r"
    fsutil.atomic_write_text(target, "")
    assert target.read_bytes() == b""


def test_atomic_write_text_honours_the_encoding(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    fsutil.atomic_write_text(target, "ação\n", encoding="utf-16-le")
    assert target.read_bytes() == "ação\n".encode("utf-16-le")
    fsutil.atomic_write_text(target, "ação\n", encoding="latin-1")
    assert target.read_bytes() == b"a\xe7\xe3o\n"
    with pytest.raises(UnicodeEncodeError):
        fsutil.atomic_write_text(target, "あ", encoding="ascii")
    # Nothing was written: the old content is intact and no temporary is left.
    assert target.read_bytes() == b"a\xe7\xe3o\n"
    assert names_in(tmp_path) == ["note.txt"]


def test_link_or_copy_replaces_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new content")
    destination = tmp_path / "dest" / "file.bin"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    mode = fsutil.link_or_copy(source, destination)
    assert mode in ("link", "copy")
    assert destination.read_bytes() == b"new content"
    # Rewriting the source through the atomic helpers must not alter the linked copy.
    fsutil.atomic_write_bytes(source, b"rewritten")
    assert destination.read_bytes() == b"new content"


def test_link_or_copy_says_what_it_did(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "out" / "deep" / "file.bin"
    mode = fsutil.link_or_copy(source, destination)  # parent directories are created
    assert destination.read_bytes() == b"payload"
    assert os.path.samefile(source, destination) is (mode == "link")
    assert source.read_bytes() == b"payload"


def test_link_or_copy_copies_when_linking_is_impossible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(source: str, destination: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "out" / "file.bin"
    assert fsutil.link_or_copy(source, destination) == "copy"
    assert destination.read_bytes() == b"payload"
    assert not os.path.samefile(source, destination)
    # A copy is replaced like anything else.
    source.write_bytes(b"second payload")
    assert fsutil.link_or_copy(source, destination) == "copy"
    assert destination.read_bytes() == b"second payload"
    assert names_in(tmp_path / "out") == ["file.bin"]


def test_link_or_copy_twice_leaves_no_temporary(tmp_path: Path) -> None:
    # The second call finds the destination already linked to the source: renaming one
    # name of a file over another name of the same file is a no-op on POSIX.
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "out" / "file.bin"
    first = fsutil.link_or_copy(source, destination)
    assert fsutil.link_or_copy(source, destination) == first
    assert fsutil.link_or_copy(source, destination) == first
    assert names_in(tmp_path / "out") == ["file.bin"]
    assert destination.read_bytes() == b"payload"


def test_link_or_copy_leaves_no_temporary_when_the_rename_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def posix_rename(source: Path, destination: Path) -> None:
        # rename(2): "If oldpath and newpath are existing hard links referring to the
        # same file, then rename() does nothing, and returns a success status."
        if os.path.exists(destination) and os.path.samefile(source, destination):
            return
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", posix_rename)
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    destination = tmp_path / "out" / "file.bin"
    fsutil.link_or_copy(source, destination)
    fsutil.link_or_copy(source, destination)
    assert names_in(tmp_path / "out") == ["file.bin"]
    assert destination.read_bytes() == b"payload"
    assert source.read_bytes() == b"payload"


def test_link_or_copy_of_a_missing_source_changes_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "out" / "file.bin"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    with pytest.raises(FileNotFoundError):
        fsutil.link_or_copy(tmp_path / "missing.bin", destination)
    assert destination.read_bytes() == b"old"
    assert names_in(tmp_path / "out") == ["file.bin"]


def test_sha256_file_hashes_the_whole_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert fsutil.sha256_file(empty) == hashlib.sha256(b"").hexdigest()
    # Larger than one read chunk, and not a multiple of it.
    data = bytes(range(256)) * 9000 + b"tail"
    assert len(data) > 2 * 1024 * 1024
    big = tmp_path / "big.bin"
    big.write_bytes(data)
    digest = fsutil.sha256_file(str(big))
    assert digest == hashlib.sha256(data).hexdigest()
    assert len(digest) == 64 and digest == digest.lower()
    with pytest.raises(FileNotFoundError):
        fsutil.sha256_file(tmp_path / "missing.bin")


def test_walk_files_is_sorted_and_skips_temporaries_and_symlinks(tmp_path: Path) -> None:
    for relative in ["b/2.bin", "b/1.bin", "a.bin", "B.bin", "c/d/e.bin", "b.txt"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    (tmp_path / "empty").mkdir()
    (tmp_path / "half.bea-tmp-deadbeef.bin").write_bytes(b"interrupted")
    # Without the symlink privilege (common on Windows) there is simply nothing to skip.
    with contextlib.suppress(OSError, NotImplementedError):
        os.symlink(tmp_path / "a.bin", tmp_path / "link.bin")

    walked = list(fsutil.walk_files(tmp_path))
    names = [entry.path for entry in walked]
    assert names == sorted(names, key=lambda name: name.split("/"))
    assert set(names) == {"B.bin", "a.bin", "b.txt", "b/1.bin", "b/2.bin", "c/d/e.bin"}
    by_name = {entry.path: entry for entry in walked}
    assert by_name["c/d/e.bin"].size == len("c/d/e.bin")
    assert by_name["a.bin"].mtime_ns > 0


def test_walk_files_on_empty_and_missing_roots(tmp_path: Path) -> None:
    assert list(fsutil.walk_files(tmp_path)) == []
    with pytest.raises(FileNotFoundError):
        list(fsutil.walk_files(tmp_path / "missing"))


def test_walk_files_order_is_by_name_within_each_directory(tmp_path: Path) -> None:
    # Worked out by hand, one directory at a time, in code-point order:
    #   root:  "B" < "Z.bin" < "a" < "a-" < "a.bin" < "a0" < "\u00df.bin"
    # Upper case sorts before lower case, a directory sorts by its own name (so "a"
    # comes before "a-" although "/" is greater than "-"), and non-ASCII names sort last.
    expected = [
        "B/x.bin",
        "Z.bin",
        "a/b.bin",
        "a/c/deep.bin",
        "a/d.bin",
        "a-/x.bin",
        "a.bin",
        "a0/x.bin",
        "\u00df.bin",
    ]
    for relative in reversed(expected):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert [entry.path for entry in fsutil.walk_files(tmp_path)] == expected
    # Sorting the paths as plain strings would give another order: "/" is not a letter.
    assert sorted(expected) != expected
    # The order does not depend on how the root is spelled.
    assert [entry.path for entry in fsutil.walk_files(str(tmp_path))] == expected
    assert [entry.path for entry in fsutil.walk_files(tmp_path / "a" / "..")] == expected


def test_walk_files_reports_size_and_modification_time(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "five.bin").write_bytes(b"12345")
    (tmp_path / "empty.bin").write_bytes(b"")
    os.utime(tmp_path / "sub" / "five.bin", ns=(1_600_000_000_000_000_000,) * 2)
    walked = list(fsutil.walk_files(tmp_path))
    assert walked == [
        fsutil.WalkedFile("empty.bin", 0, os.stat(tmp_path / "empty.bin").st_mtime_ns),
        fsutil.WalkedFile("sub/five.bin", 5, 1_600_000_000_000_000_000),
    ]
    path, size, mtime_ns = walked[1]
    assert (path, size, mtime_ns) == (walked[1].path, walked[1].size, walked[1].mtime_ns)


def test_walk_files_relative_root_and_file_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tree" / "sub").mkdir(parents=True)
    (tmp_path / "tree" / "sub" / "a.bin").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert [entry.path for entry in fsutil.walk_files("tree")] == ["sub/a.bin"]
    # A file is not a tree.
    with pytest.raises(OSError):
        list(fsutil.walk_files(tmp_path / "tree" / "sub" / "a.bin"))


def test_walk_files_does_not_follow_directory_links(tmp_path: Path) -> None:
    # A link to a directory (a symbolic link, or a junction on Windows) could lead out of
    # the tree, or back into it forever.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a")
    (root / "sub" / "b.bin").write_bytes(b"b")
    link_directory(root / "sub" / "link", outside)
    link_directory(root / "loop", root)
    try:
        assert [entry.path for entry in fsutil.walk_files(root)] == ["a.bin", "sub/b.bin"]
    finally:
        # Do not leave a looping link behind for whatever scans the temporary directory.
        unlink_directory(root / "loop")


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/abs/path",
        "../escape",
        "a/../../escape",
        "a//b",
        "a/./b",
        "a\\b",
        "C:/windows",
        "a/..",
        ".",
        "..",
        "/",
        "a/",
        "./a",
        "a/b/",
        "//server/share/file",
        "\\\\server\\share\\file",
        "C:",
        "C:file.bin",
        "c:/windows",
        # A drive or a stream name further down is as bad as one in front: on Windows
        # "a/D:/x" joins to "D:x", and "a/x.bin:s" opens another stream of "a/x.bin".
        "a/D:/x",
        "a/D:x",
        "a/C:/x",
        "a/b/C:",
        "a/x.bin:stream",
        "a/b:c/d",
        # No file name holds a NUL: opening such a path fails with a ValueError of its own.
        "a\x00b",
        "a/b\x00/c",
    ],
)
def test_resolve_relative_rejects_unsafe_paths(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        fsutil.resolve_relative(tmp_path, relative)


@pytest.mark.parametrize(
    "relative",
    [
        "a.bin",
        "a/b/c.bin",
        "romfs/Data/Level 1/map (final).pak",
        ".hidden/.config",
        "a..b/c...d",
        "name.with.many.dots.bin",
        "~/not-a-home",
        "$HOME/%TEMP%/literal",
        "\u00e9t\u00e9/\u3042.bin",
    ],
)
def test_resolve_relative_stays_below_the_root(tmp_path: Path, relative: str) -> None:
    for root in (tmp_path, str(tmp_path), tmp_path / "does" / "not" / "exist"):
        resolved = fsutil.resolve_relative(root, relative)
        assert isinstance(resolved, Path)
        assert resolved.relative_to(root).parts == tuple(relative.split("/"))
    # A relative root stays relative: the result is still "root plus the segments".
    assert fsutil.resolve_relative("out", relative) == Path("out").joinpath(*relative.split("/"))


def test_resolve_relative_joins_clean_paths(tmp_path: Path) -> None:
    assert fsutil.resolve_relative(tmp_path, "a/b/c.bin") == tmp_path / "a" / "b" / "c.bin"
    assert (
        fsutil.resolve_relative(tmp_path, "file with spaces.bin")
        == tmp_path / "file with spaces.bin"
    )


def test_prune_empty_dirs_keeps_root_and_non_empty_dirs(tmp_path: Path) -> None:
    (tmp_path / "keep" / "inner").mkdir(parents=True)
    (tmp_path / "keep" / "inner" / "file.bin").write_bytes(b"x")
    (tmp_path / "gone" / "deeper" / "deepest").mkdir(parents=True)
    assert fsutil.prune_empty_dirs(tmp_path) == 3
    assert (tmp_path / "keep" / "inner" / "file.bin").exists()
    assert not (tmp_path / "gone").exists()
    assert tmp_path.exists()
    assert fsutil.prune_empty_dirs(tmp_path / "missing") == 0


def test_prune_empty_dirs_counts_every_directory_it_removes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for relative in ["a/b/c", "a/d", "e", "full/empty-1", "full/empty-2/x/y"]:
        (root / relative).mkdir(parents=True)
    (root / "full" / "file.bin").write_bytes(b"x")
    # By hand: a, a/b, a/b/c, a/d, e, full/empty-1, full/empty-2, .../x, .../x/y = 9.
    assert fsutil.prune_empty_dirs(str(root)) == 9
    assert names_in(root) == ["full"]
    assert names_in(root / "full") == ["file.bin"]
    # Nothing left to do; an empty root is never removed.
    assert fsutil.prune_empty_dirs(root) == 0
    (root / "full" / "file.bin").unlink()
    assert fsutil.prune_empty_dirs(root) == 1
    assert root.is_dir() and names_in(root) == []
    assert fsutil.prune_empty_dirs(root) == 0
    assert root.is_dir()


def test_prune_empty_dirs_keeps_directories_that_hold_a_leftover(tmp_path: Path) -> None:
    # An interrupted write is invisible to walks, but the directory is not empty.
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "half.bea-tmp-deadbeef.bin").write_bytes(b"interrupted")
    assert list(fsutil.walk_files(tmp_path)) == []
    assert fsutil.prune_empty_dirs(tmp_path) == 0
    assert names_in(tmp_path / "busy") == ["half.bea-tmp-deadbeef.bin"]


def test_prune_empty_dirs_on_something_that_is_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_bytes(b"x")
    assert fsutil.prune_empty_dirs(tmp_path / "plain.txt") == 0
    assert (tmp_path / "plain.txt").read_bytes() == b"x"


def test_prune_empty_dirs_never_follows_or_removes_directory_links(tmp_path: Path) -> None:
    # What is behind a link does not belong to the tree: BEA did not create it.
    outside = tmp_path / "outside"
    (outside / "empty").mkdir(parents=True)
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "gone" / "deeper").mkdir(parents=True)
    link_directory(root / "sub" / "link", outside)

    # "sub" holds the link, so it is not empty; only "gone/deeper" and "gone" go away.
    assert fsutil.prune_empty_dirs(root) == 2
    assert names_in(root) == ["sub"]
    assert names_in(root / "sub") == ["link"]
    assert names_in(outside) == ["empty"]
    assert os.path.samefile(root / "sub" / "link", outside)
