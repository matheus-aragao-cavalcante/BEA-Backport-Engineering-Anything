"""PFS0 archives: listing what a packaged dump contains.

A PFS0 archive is a flat list of named files stored one after the other, uncompressed.
It is the outer container of ``.nsp`` and ``.nsz`` game packages, and that outer layer
is not encrypted: its table of contents can be read without any key.

BEA goes no further than that table. It never decrypts anything and needs no keys; what
the files of a package hold stays opaque to it. This module exists so that, when BEA is
pointed at a package instead of an extracted folder, it can tell the user what the
package contains and that it has to be extracted first. Hence the API: a header parser,
a file reader that only ever touches the header and, for tests and for the
synthetic-game generator, a writer and a way to read one entry back.

Format
------
Public documentation: the switchbrew wiki, page "NCA", section "PFS0" (its field names
are given in parentheses). Every integer is little-endian.

Header, ``0x10`` bytes::

    +0x00  4 bytes  magic "PFS0"
    +0x04  u32      entry count (EntryCount)
    +0x08  u32      string table size, in bytes (StringTableSize)
    +0x0C  u32      reserved

Entry table, ``entry count`` records of ``0x18`` bytes, right after the header::

    +0x00  u64      offset of the data, relative to the start of the data region (Offset)
    +0x08  u64      size of the data, in bytes (Size)
    +0x10  u32      offset of the name inside the string table (StringOffset)
    +0x14  u32      reserved

String table, ``string table size`` bytes, right after the entry table: the names,
UTF-8, each one ended by a NUL. The table is usually zero-padded (writers like to align
what follows), and the last name is not always terminated: it may end with the table.

Data region: everything from ``0x10 + entry count * 0x18 + string table size`` on. That
sum is the size of the *complete header*, the only part of a file this module needs.

The wiki lists the PFS0 fields without comment. Its "XCI" page puts the same header and
the same first three entry fields into words for the sibling HFS0 container: the offset
of a file is its offset "in Data", and the string table is "00-padded" so that the data
starts aligned.

Robustness
----------
A header is never taken at its word. The counts are checked against sanity limits
(:data:`MAX_ENTRIES`, :data:`MAX_STRING_TABLE`, :data:`MAX_NAME_BYTES`) and against the
bytes that are really there before anything is allocated or sliced, so that a truncated
or hostile file ends in a :class:`~bea.errors.FormatError` and never in a crash, an
out-of-memory condition or a long wait. Reserved fields are ignored, and so is the way
the entries are laid out in the data region (gaps, order, overlaps): none of it matters
for a listing.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from io import FileIO

from bea.errors import FormatError
from bea.fsutil import fs_path

__all__ = [
    "MAX_ENTRIES",
    "MAX_NAME_BYTES",
    "MAX_STRING_TABLE",
    "PFS0_MAGIC",
    "Pfs0Archive",
    "Pfs0Entry",
    "build_pfs0",
    "header_size",
    "is_pfs0",
    "parse_pfs0",
    "read_entry",
    "read_pfs0",
]

#: The four bytes every PFS0 archive starts with.
PFS0_MAGIC = b"PFS0"
#: Sanity limit on the entry count. A package holds a handful of files; a header that
#: announces more than this is corrupt or hostile and is refused before anything is
#: allocated for it.
MAX_ENTRIES = 65536
#: Sanity limit on the size of the string table, in bytes (16 MiB).
MAX_STRING_TABLE = 16 * 1024 * 1024
#: Sanity limit on the length of one name, in UTF-8 bytes. Without it a header could
#: point thousands of entries into one long run of bytes that has no terminator and have
#: the same megabytes decoded, and kept, once per entry. No common filesystem stores a
#: name this long (255 UTF-16 units are at most 765 UTF-8 bytes).
MAX_NAME_BYTES = 0x300

_HEADER = struct.Struct("<4sIII")  # magic, entry count, string table size, reserved
_ENTRY = struct.Struct("<QQII")  # data offset, data size, name offset, reserved


@dataclass(frozen=True, slots=True)
class Pfs0Entry:
    """One file of a PFS0 archive: its name and where its bytes are, not the bytes."""

    #: A plain file name (never a path), unique inside its archive.
    name: str
    #: Absolute offset of the data, counted from the start of the archive file.
    offset: int
    #: Size of the data, in bytes.
    size: int


@dataclass(frozen=True, slots=True)
class Pfs0Archive:
    """The table of contents of a PFS0 archive."""

    #: The entries, in the order of the entry table.
    entries: tuple[Pfs0Entry, ...]
    #: Absolute offset of the data region, which is also the size of the complete header.
    data_offset: int

    def entry(self, name: str) -> Pfs0Entry | None:
        """Return the entry called ``name`` (exact, case-sensitive match), or ``None``."""
        for candidate in self.entries:
            if candidate.name == name:
                return candidate
        return None


def is_pfs0(prefix: bytes) -> bool:
    """Tell whether a file that starts with ``prefix`` announces itself as a PFS0 archive.

    Only the magic is looked at: :func:`parse_pfs0` decides whether the archive is valid.
    """
    return bytes(prefix[: len(PFS0_MAGIC)]) == PFS0_MAGIC


def header_size(prefix: bytes) -> int:
    """Return how many bytes the complete header of an archive occupies.

    ``prefix`` holds at least the first ``0x10`` bytes of the archive. The complete header
    is the fixed header, the entry table and the string table: what :func:`parse_pfs0`
    needs, and where the data region starts.

    Raises:
        FormatError: wrong magic, fewer than ``0x10`` bytes, or an entry count or string
            table size above :data:`MAX_ENTRIES` or :data:`MAX_STRING_TABLE`.
    """
    count, table_size = _read_counts(prefix)
    return _HEADER.size + count * _ENTRY.size + table_size


def parse_pfs0(
    data: bytes | bytearray | memoryview, *, file_size: int | None = None
) -> Pfs0Archive:
    """Parse the complete header of a PFS0 archive into its table of contents.

    ``data`` holds at least the complete header (see :func:`header_size`). Anything after
    it is ignored, so the whole file may be passed just as well; no entry data is read.

    Every name must be a plain file name: valid UTF-8, not empty, not ``.`` or ``..``,
    free of ``/`` and ``\\``, at most :data:`MAX_NAME_BYTES` bytes long and used by one
    entry only. When ``file_size`` (the real size of the file) is given, every entry must
    also lie inside the file, which is how a package that was cut short is recognized.

    Raises:
        FormatError: wrong magic; a count above the limits; ``data`` shorter than the
            complete header; a name offset outside the string table; an invalid or
            duplicate name; with ``file_size``, the first entry (in table order) that
            does not fit in the file, reported by name.
        ValueError: ``file_size`` is negative.
    """
    if file_size is not None and file_size < 0:
        raise ValueError(f"file_size cannot be negative, got {file_size}")
    # The header is copied out and the views are released on the way out of this block,
    # error or not. A view that outlived it (a traceback keeps frames alive) would leave
    # a caller's bytearray locked against resizing: awkward for "read more, try again".
    with memoryview(data) as raw, raw.cast("B") as view:
        count, table_size = _read_counts(bytes(view[: _HEADER.size]))
        table_offset = _HEADER.size + count * _ENTRY.size
        data_offset = table_offset + table_size
        if len(view) < data_offset:
            raise FormatError(
                f"PFS0: truncated header: {count} entries and a 0x{table_size:X}-byte "
                f"string table need 0x{data_offset:X} bytes, only 0x{len(view):X} available"
            )
        records = bytes(view[_HEADER.size : table_offset])
        table = bytes(view[table_offset:data_offset])

    entries: list[Pfs0Entry] = []
    first_use: dict[str, int] = {}
    for index, (relative_offset, size, name_offset, _reserved) in enumerate(
        _ENTRY.iter_unpack(records)
    ):
        name = _read_name(table, name_offset, index)
        previous = first_use.setdefault(name, index)
        if previous != index:
            raise FormatError(
                f"PFS0: {_entry_label(index)}: duplicate name {name!r}, "
                f"already used by entry {previous}"
            )
        entries.append(Pfs0Entry(name, data_offset + relative_offset, size))

    if file_size is not None:
        if file_size < data_offset:
            raise FormatError(
                f"PFS0: truncated package: the header alone takes 0x{data_offset:X} bytes, "
                f"the file is only 0x{file_size:X} bytes long"
            )
        for entry in entries:
            if entry.offset + entry.size > file_size:
                raise FormatError(f"PFS0: truncated package: {_past_the_end(entry, file_size)}")
    return Pfs0Archive(tuple(entries), data_offset)


def read_pfs0(path: str | os.PathLike[str]) -> Pfs0Archive:
    """Read the table of contents of the PFS0 archive stored at ``path``.

    Only the header is read from the file: its first ``0x10`` bytes, then the rest of
    it. The data region is never touched, whatever its size. The entries are checked
    against the real size of the file, so a package that was cut short is reported.

    Raises:
        FormatError: the file is not a valid PFS0 archive or is truncated (see
            :func:`parse_pfs0`).
        OSError: the file cannot be opened or read.
    """
    # Unbuffered on purpose: a buffered reader would fetch a whole buffer of the data
    # region behind a small header.
    with open(fs_path(path), "rb", buffering=0) as handle:
        file_size = os.fstat(handle.fileno()).st_size
        prefix = _read_up_to(handle, _HEADER.size)
        needed = header_size(prefix)
        # Never ask for more than the file holds, whatever the header announces; a header
        # that does not fit in its file is then reported by parse_pfs0.
        header = prefix + _read_up_to(handle, min(needed, file_size) - len(prefix))
    return parse_pfs0(header, file_size=file_size)


def read_entry(path: str | os.PathLike[str], entry: Pfs0Entry) -> bytes:
    """Return the bytes of one entry of the PFS0 archive stored at ``path``.

    The bytes come back exactly as stored. This exists for tests and for synthetic
    archives; BEA has no use for the (encrypted) content of a real package.

    Raises:
        FormatError: the entry does not lie inside the file.
        OSError: the file cannot be opened or read.
        ValueError: the entry has a negative offset or size.
    """
    if entry.offset < 0 or entry.size < 0:
        raise ValueError(f"an entry cannot have a negative offset or size: {entry!r}")
    with open(fs_path(path), "rb", buffering=0) as handle:
        file_size = os.fstat(handle.fileno()).st_size
        # Checked before reading: the size comes from a header and sizes an allocation.
        if entry.offset + entry.size > file_size:
            raise FormatError(f"PFS0: {_past_the_end(entry, file_size)}")
        handle.seek(entry.offset)
        data = _read_up_to(handle, entry.size)
    if len(data) != entry.size:
        raise FormatError(
            f"PFS0: entry {entry.name!r}: only 0x{len(data):X} of its 0x{entry.size:X} bytes "
            f"could be read at offset 0x{entry.offset:X} (the file changed while it was read)"
        )
    return data


def build_pfs0(files: Sequence[tuple[str, bytes]], *, string_table_alignment: int = 0x20) -> bytes:
    """Build a PFS0 archive that holds ``files``, given as ``(name, data)`` pairs.

    A writer for tests and for the synthetic-game generator. The entries keep the given
    order; every name is stored NUL-terminated; the string table is zero-padded so that
    the size of the complete header (the offset of the data region) is a multiple of
    ``string_table_alignment`` (``1`` disables the padding); the data of the files
    follows back to back. Whatever this function returns, :func:`parse_pfs0` accepts.

    Raises:
        ValueError: a name is not a plain file name (see :func:`parse_pfs0`), cannot be
            encoded as UTF-8 or is used twice; ``string_table_alignment`` is smaller
            than 1; or the archive would exceed :data:`MAX_ENTRIES` or
            :data:`MAX_STRING_TABLE`.
    """
    if string_table_alignment < 1:
        raise ValueError(f"string_table_alignment must be at least 1, got {string_table_alignment}")
    if len(files) > MAX_ENTRIES:
        raise ValueError(f"{len(files)} files: a PFS0 archive holds at most {MAX_ENTRIES}")

    records = bytearray()
    table = bytearray()
    seen: set[str] = set()
    data_size = 0
    for index, (name, data) in enumerate(files):
        problem = _name_problem(name)
        if problem is not None:
            raise ValueError(f"file {index}: name {name!r} {problem}")
        try:
            encoded = name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"file {index}: name {name!r} cannot be encoded as UTF-8") from exc
        if len(encoded) > MAX_NAME_BYTES:
            raise ValueError(
                f"file {index}: a name of {len(encoded)} bytes is longer than "
                f"{MAX_NAME_BYTES} bytes"
            )
        if name in seen:
            raise ValueError(f"file {index}: duplicate name {name!r}")
        seen.add(name)
        records += _ENTRY.pack(data_size, len(data), len(table), 0)
        table += encoded + b"\x00"
        data_size += len(data)

    unpadded = _HEADER.size + len(records) + len(table)
    padding = -unpadded % string_table_alignment
    # Checked before the padding is allocated: the alignment is the caller's to choose.
    if len(table) + padding > MAX_STRING_TABLE:
        raise ValueError(
            f"a string table of {len(table) + padding} bytes is larger than "
            f"{MAX_STRING_TABLE} bytes"
        )
    table += bytes(padding)
    return b"".join(
        [
            _HEADER.pack(PFS0_MAGIC, len(files), len(table), 0),
            records,
            table,
            *(data for _name, data in files),
        ]
    )


# ------------------------------------------------------------------------ internals


def _read_counts(prefix: bytes) -> tuple[int, int]:
    """Validate the fixed header and return ``(entry count, string table size)``."""
    magic = bytes(prefix[: len(PFS0_MAGIC)])
    # Compared over the bytes that are there: a file too short to hold the magic is
    # "truncated" only as long as what it does hold agrees with the magic.
    if magic != PFS0_MAGIC[: len(magic)]:
        raise FormatError(
            f"PFS0: wrong magic at offset 0x0: expected {PFS0_MAGIC!r}, found {magic!r}"
        )
    if len(prefix) < _HEADER.size:
        raise FormatError(
            f"PFS0: truncated header: the fixed header is 0x{_HEADER.size:X} bytes, "
            f"only 0x{len(prefix):X} available"
        )
    _magic, count, table_size, _reserved = _HEADER.unpack_from(prefix)
    if count > MAX_ENTRIES:
        raise FormatError(
            f"PFS0: entry count at offset 0x4 is {count}, above the limit of {MAX_ENTRIES}"
        )
    if table_size > MAX_STRING_TABLE:
        raise FormatError(
            f"PFS0: string table size at offset 0x8 is 0x{table_size:X}, "
            f"above the limit of 0x{MAX_STRING_TABLE:X}"
        )
    return count, table_size


def _read_name(table: bytes, start: int, index: int) -> str:
    """Return the name that entry ``index`` points at (offset ``start`` of the table).

    Raises:
        FormatError: the offset is outside the table, or what is there is not a plain
            file name.
    """
    if start >= len(table):
        raise FormatError(
            f"PFS0: {_entry_label(index)}: name offset 0x{start:X} is outside the string "
            f"table (0x{len(table):X} bytes)"
        )
    # The search stops one byte past the longest name allowed, which bounds the work per
    # entry however large the table is.
    limit = start + MAX_NAME_BYTES + 1
    end = table.find(b"\x00", start, limit)
    if end < 0:
        # No terminator in reach: the last name of the table may do without one,
        # anything else is too long to be a name.
        end = min(len(table), limit)
    raw = table[start:end]
    if len(raw) > MAX_NAME_BYTES:
        raise FormatError(
            f"PFS0: {_entry_label(index)}: the name at string table offset 0x{start:X} "
            f"is longer than {MAX_NAME_BYTES} bytes"
        )
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError(
            f"PFS0: {_entry_label(index)}: the name at string table offset 0x{start:X} "
            f"is not valid UTF-8 ({exc.reason} at string table offset 0x{start + exc.start:X})"
        ) from exc
    problem = _name_problem(name)
    if problem is not None:
        raise FormatError(
            f"PFS0: {_entry_label(index)}: the name {name!r} at string table offset "
            f"0x{start:X} {problem}"
        )
    return name


def _name_problem(name: str) -> str | None:
    """Return why ``name`` cannot name a container entry, or ``None`` when it can.

    An entry is a file directly inside the archive, so its name must be a plain file
    name: nothing that reads as a path or that could point outside a folder.
    """
    if not name:
        return "is empty"
    if name in (".", ".."):
        return "is a directory reference, not a file name"
    if "/" in name or "\\" in name:
        return "contains a path separator"
    if "\x00" in name:
        return "contains a NUL character"
    return None


def _entry_label(index: int) -> str:
    """Name an entry record in an error message: its index and where it is in the file."""
    return f"entry {index} (offset 0x{_HEADER.size + index * _ENTRY.size:X})"


def _past_the_end(entry: Pfs0Entry, file_size: int) -> str:
    """Say that ``entry`` does not fit in a file of ``file_size`` bytes."""
    return (
        f"entry {entry.name!r} (offset 0x{entry.offset:X}, size 0x{entry.size:X}) ends at "
        f"0x{entry.offset + entry.size:X}, the file is only 0x{file_size:X} bytes long"
    )


def _read_up_to(handle: FileIO, size: int) -> bytes:
    """Read ``size`` bytes from an unbuffered file, fewer only when the file ends first.

    One ``read`` of an unbuffered file may return less than was asked for, so the
    request is repeated until it is satisfied or the file is exhausted.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = handle.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
