"""IPS and IPS32 binary patches: a cautious writer, a strict reader, an applier.

An IPS patch is a list of records that each say "write these bytes at this offset". It
is how BEA hands a change to an executable over to a console's custom firmware or to an
emulator without shipping any code: the loader finds a file named
``<module build ID>.ips`` and applies it to the decompressed, in-memory image of that
module. The file holds the bytes that change and nothing else. IPS32 is the same format
with wider offsets; it uses the ``.ips`` extension too and is recognized by its magic.

Format
------
Public documentation: the description of the IPS file format published by ZeroSoft and
the SnesLab wiki article "IPS file format" (records, RLE records, the truncation
extension); the documentation of the loader module of the Atmosphère custom firmware
(patch files named after the build ID, accepted in the IPS or the IPS32 format).

Every integer is unsigned and big-endian; nothing is padded or aligned::

                         IPS                     IPS32
    magic                "PATCH"                 "IPS32"
    record, repeated:
      offset             3 bytes                 4 bytes
      size               2 bytes                 2 bytes
      payload            "size" bytes            "size" bytes
    terminator           "EOF"                   "EEOF"
    truncation length    3 bytes, optional       (does not exist)

* A *literal* record has a size of 1 or more: its payload is written to the target
  starting at its offset.
* An *RLE* record has a size field of 0. Instead of a payload it carries a 2-byte run
  length (1 or more) and one fill byte, which is written "run length" times from the
  offset.
* The truncation length is an extension that some tools write after the terminator of
  a classic IPS patch: exactly three more bytes, the length the patched output is cut
  down to.
* One record carries at most ``0xFFFF`` bytes. A record starts at ``0xFFFFFF`` at most
  in IPS and at ``0xFFFFFFFF`` at most in IPS32.
* There is no checksum, no identification of the target and no "expected original
  bytes": a patch applied to the wrong file goes unnoticed. Whoever writes a patch has
  to check the target first.

The terminator ambiguity
------------------------
A reader looks for the terminator *before* it takes the next bytes for an offset, so no
record can start at the offset whose bytes spell the terminator: ``0x454F46`` in IPS,
``0x45454F46`` in IPS32 (:attr:`IpsFormat.forbidden_offset`). Only the place where a
record starts matters; payload bytes may hold those values freely.

Offsets of executable modules
-----------------------------
For an executable module, loaders count record offsets from the start of the module's
``0x100``-byte header, which they never patch: record offset = offset in the memory
image + ``0x100``. That bias is not added here. Callers add it, and :func:`apply_ips`
can take it away again.

What the writer emits
---------------------
:func:`build_ips` keeps to the part of the format that every loader BEA writes for is
known to accept, so that one file works everywhere:

* literal records only, sorted by offset, not overlapping, split at ``0xFFFF`` bytes;
  always the terminator; never an RLE record, never a truncation length;
* records are never merged across bytes that are not being changed, because the bytes
  in between would be bytes of the target copied into the patch;
* no record starts at the forbidden offset. A change that begins there continues the
  record that ends there when there is one; otherwise its record begins one byte
  earlier and rewrites that byte with the value it already has, the only byte of the
  target a patch can ever contain;
* classic IPS as long as every record written starts at or below ``0xFFFFFF``, IPS32
  otherwise.

Robustness
----------
:func:`parse_ips` takes no field at its word: every read is checked against the bytes
that are really there, so a truncated or damaged file ends in a
:class:`~bea.errors.FormatError` that names the offset in the patch file. RLE is the one
place where a file can ask for more memory than it occupies (eight bytes of file for up
to ``0xFFFF`` bytes of data), which is why the total is capped by
:data:`MAX_RLE_EXPANSION`.
"""

from __future__ import annotations

import enum
import itertools
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from bea.errors import FormatError

__all__ = [
    "IPS32_MAGIC",
    "IPS32_TERMINATOR",
    "IPS_MAGIC",
    "IPS_TERMINATOR",
    "MAX_RECORD_SIZE",
    "MAX_RLE_EXPANSION",
    "IpsFormat",
    "IpsPatch",
    "IpsRecord",
    "apply_ips",
    "build_ips",
    "describe_ips",
    "parse_ips",
]

#: The five bytes a classic IPS patch starts with.
IPS_MAGIC = b"PATCH"
#: What ends the record list of a classic IPS patch.
IPS_TERMINATOR = b"EOF"
#: The five bytes an IPS32 patch starts with.
IPS32_MAGIC = b"IPS32"
#: What ends the record list of an IPS32 patch.
IPS32_TERMINATOR = b"EEOF"
#: The most bytes one record can carry: its size field has 16 bits.
MAX_RECORD_SIZE = 0xFFFF
#: Sanity limit on what the RLE records of one patch may expand to, in bytes (64 MiB).
#: An RLE record turns eight bytes of file into up to ``0xFFFF`` bytes of data, so
#: without a limit a file of a few hundred kilobytes could ask for gigabytes. No real
#: patch comes close: everything a classic IPS patch can address is 16 MiB, and BEA
#: itself never writes RLE.
MAX_RLE_EXPANSION = 64 * 1024 * 1024

_SIZE_FIELD = 2  # width of a record's size field and of an RLE run length
_TRUNCATION_FIELD = 3  # width of the optional truncation length of classic IPS
# Where a record of an IPS32 patch must end at the latest. Beyond it lie offsets that no
# 32-bit offset can name, and a reader that computes in 32 bits would wrap around to the
# start of its target. Classic IPS has no such edge: a record may start at the last
# offset and run on past it.
_IPS32_END = 1 << 32


class IpsFormat(enum.StrEnum):
    """The two flavours of the format. A file announces its flavour with its magic."""

    #: The original format, with 3-byte record offsets.
    IPS = "ips"
    #: The variant with 4-byte record offsets.
    IPS32 = "ips32"

    @property
    def magic(self) -> bytes:
        """The five bytes a patch of this format starts with."""
        return IPS_MAGIC if self is IpsFormat.IPS else IPS32_MAGIC

    @property
    def terminator(self) -> bytes:
        """The bytes that end the record list."""
        return IPS_TERMINATOR if self is IpsFormat.IPS else IPS32_TERMINATOR

    @property
    def offset_size(self) -> int:
        """Width of a record offset, in bytes: 3 or 4."""
        return 3 if self is IpsFormat.IPS else 4

    @property
    def max_offset(self) -> int:
        """The highest offset a record can start at."""
        return (1 << (8 * self.offset_size)) - 1

    @property
    def forbidden_offset(self) -> int:
        """The one offset no record can start at: written out, it is the terminator."""
        return int.from_bytes(self.terminator, "big")


@dataclass(frozen=True, slots=True)
class IpsRecord:
    """One change: ``data`` is written to the target starting at ``offset``."""

    #: Where the first byte goes, in the address space of the patch (for an executable
    #: module that is the offset in the memory image plus ``0x100``).
    offset: int
    #: The bytes to write. An RLE record read from a file is stored expanded.
    data: bytes

    @property
    def end(self) -> int:
        """The offset just past the last byte written."""
        return self.offset + len(self.data)


@dataclass(frozen=True, slots=True)
class IpsPatch:
    """A parsed patch: what :func:`parse_ips` returns and :func:`apply_ips` applies."""

    format: IpsFormat
    #: The records in file order. A file may hold them in any order and may write the
    #: same byte more than once; the last write wins.
    records: tuple[IpsRecord, ...]
    #: Length the patched output is cut down to (an extension of classic IPS), or
    #: ``None`` when the file has no truncation length.
    truncate: int | None = None


def build_ips(
    records: Iterable[IpsRecord | tuple[int, bytes]],
    *,
    fmt: IpsFormat | None = None,
    original_byte: Callable[[int], int] | None = None,
) -> bytes:
    """Serialize changes as a patch file that every known loader accepts.

    ``records`` are the changes, as :class:`IpsRecord` or ``(offset, data)`` pairs, in
    any order. They are written sorted by offset, as literal records of at most
    :data:`MAX_RECORD_SIZE` bytes (longer data is split), followed by the terminator.
    Records that merely touch stay separate. No record is ever stretched over bytes
    that are not being changed, so the patch contains no byte of the target, with the
    single exception described below. Without any record the result is the smallest
    valid file: magic and terminator.

    ``fmt`` forces a format. By default the patch is classic IPS when every record that
    ends up in the file starts at or below ``0xFFFFFF``, and IPS32 otherwise.

    No record may start at :attr:`IpsFormat.forbidden_offset`. A change that begins
    there is appended to the record that ends there, if there is one (the data is then
    split somewhere else when it no longer fits one record). Otherwise its record begins
    one byte earlier and has to repeat the byte that the target holds at that place:
    ``original_byte(offset)`` returns the current value (0 to 255) of the target byte at
    ``offset``, in the same address space as the record offsets. It is called at most
    once, and only in that situation.

    Raises:
        TypeError: a record is neither an :class:`IpsRecord` nor an ``(offset, data)``
            pair, an offset is not an integer, data is not bytes, or ``original_byte``
            returned something that is not an integer.
        ValueError: empty data; a negative offset; overlapping records; an offset that
            the chosen or forced format cannot express (in IPS32 a record must also
            end at or below ``0x100000000``); a record that starts at the forbidden
            offset when no ``original_byte`` was given; a value outside 0..255
            returned by ``original_byte``.
    """
    changes = _normalize(records)
    if fmt is None:
        # Classic IPS is what every loader reads, so it is tried first. Only offsets and
        # sizes are looked at here: which format a patch needs never depends on the
        # caller having provided a callback.
        chosen = IpsFormat.IPS
        spans, borrowed = _layout(changes, chosen)
        if spans and spans[-1].offset > chosen.max_offset:  # the last start is the highest
            chosen = IpsFormat.IPS32
            spans, borrowed = _layout(changes, chosen)
    else:
        chosen = IpsFormat(fmt)
        spans, borrowed = _layout(changes, chosen)
    _check_limits(spans, chosen)

    # Everything the patch writes, in offset order. The spans cover exactly these bytes
    # in exactly this order, so the payloads are consecutive slices of this stream.
    pieces: list[bytes] = []
    for offset, data in changes:
        if borrowed is not None and offset == chosen.forbidden_offset:
            pieces.append(bytes([_original_value(original_byte, borrowed, chosen)]))
        pieces.append(data)
    stream = memoryview(b"".join(pieces))

    out = bytearray(chosen.magic)
    position = 0
    for span in spans:
        out += span.offset.to_bytes(chosen.offset_size, "big")
        out += span.size.to_bytes(_SIZE_FIELD, "big")
        out += stream[position : position + span.size]
        position += span.size
    out += chosen.terminator
    return bytes(out)


def parse_ips(data: bytes | bytearray | memoryview) -> IpsPatch:
    """Parse the bytes of an IPS or IPS32 patch file.

    The reader is strict. The magic selects the format. Then, record after record: the
    terminator ends the list; anything else in its place is the offset of a record,
    followed by its size and its payload, or, when the size is 0, by an RLE run length
    and a fill byte. An RLE record comes back expanded, as an ordinary
    :class:`IpsRecord`. After the terminator there may be nothing or, in classic IPS
    only, exactly the three bytes of a truncation length.

    The records are returned as they are in the file: in file order, overlapping ones
    included. Nothing is said about the target, which a patch does not identify.

    Raises:
        FormatError: unknown magic; the data ends before the terminator or in the
            middle of a field (the message names the field and its offset in the
            file); an RLE run length of 0; RLE records that expand to more than
            :data:`MAX_RLE_EXPANSION` bytes; bytes after the terminator other than the
            truncation length of a classic IPS patch.
        TypeError: ``data`` is not bytes.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError(f"expected the bytes of a patch file, got {type(data).__name__}")
    raw = bytes(data)
    fmt = _detect_format(raw)
    terminator = fmt.terminator
    position = len(fmt.magic)
    records: list[IpsRecord] = []
    expanded = 0
    while True:
        label = f"record #{len(records) + 1}"
        # The terminator is looked for first, as every reader does: this is the order
        # that makes one offset unusable (see the module docstring).
        field = _read(
            raw,
            position,
            fmt.offset_size,
            f"the terminator {terminator!r} (or the offset of {label})",
        )
        position += fmt.offset_size
        if field == terminator:
            break
        offset = int.from_bytes(field, "big")
        size = int.from_bytes(_read(raw, position, _SIZE_FIELD, f"the size of {label}"), "big")
        position += _SIZE_FIELD
        if size:
            payload = _read(raw, position, size, f"the payload of {label}")
            position += size
        else:
            run = int.from_bytes(
                _read(raw, position, _SIZE_FIELD, f"the RLE run length of {label}"), "big"
            )
            if run == 0:
                raise FormatError(
                    f"IPS: {label}: the RLE run length at offset 0x{position:X} is 0, "
                    f"expected 1 to 0x{MAX_RECORD_SIZE:X}"
                )
            # Checked before the run is expanded: the length comes from the file and
            # sizes an allocation that the file itself does not pay for.
            expanded += run
            if expanded > MAX_RLE_EXPANSION:
                raise FormatError(
                    f"IPS: {label}: with the RLE run length at offset 0x{position:X} the RLE "
                    f"records of this patch expand to more than 0x{MAX_RLE_EXPANSION:X} bytes"
                )
            position += _SIZE_FIELD
            fill = _read(raw, position, 1, f"the RLE fill byte of {label}")
            position += 1
            payload = fill * run
        records.append(IpsRecord(offset, payload))

    left = len(raw) - position
    truncate: int | None = None
    if fmt is IpsFormat.IPS and left == _TRUNCATION_FIELD:
        truncate = int.from_bytes(raw[position:], "big")
    elif left:
        allowed = (
            f"nothing, or a {_TRUNCATION_FIELD}-byte truncation length"
            if fmt is IpsFormat.IPS
            else "nothing (IPS32 has no truncation length)"
        )
        raise FormatError(
            f"IPS: {left} unexpected byte(s) after the terminator, at offset 0x{position:X}: "
            f"expected {allowed}"
        )
    return IpsPatch(fmt, tuple(records), truncate)


def apply_ips(target: bytes | bytearray, patch: IpsPatch | bytes, *, bias: int = 0) -> bytes:
    """Return a copy of ``target`` with ``patch`` applied to it.

    A plain applier for tests and tools. ``patch`` is a parsed patch or the bytes of a
    patch file. Each record is written at its offset minus ``bias``, in file order, so
    where records overlap the last one wins. ``bias`` is what a loader adds to the
    offsets of the buffer it patches: ``0x100`` when ``target`` is the memory image of
    an executable module. A truncation length is applied after the records, with the
    bias subtracted in the same way.

    The target never grows, and ``target`` itself is left untouched.

    Raises:
        FormatError: ``patch`` is the bytes of a malformed patch file (see
            :func:`parse_ips`).
        TypeError: ``target`` is not bytes, ``patch`` is neither a patch nor bytes, or
            ``bias`` is not an integer.
        ValueError: once the bias is subtracted, a record starts below 0 or reaches
            past the end of the target, or the truncation length is negative or larger
            than the target.
    """
    if not isinstance(target, bytes | bytearray | memoryview):
        raise TypeError(f"expected the target as bytes, got {type(target).__name__}")
    if isinstance(bias, bool) or not isinstance(bias, int):
        raise TypeError(f"expected an integer bias, got {type(bias).__name__}")
    if not isinstance(patch, IpsPatch):
        patch = parse_ips(patch)

    out = bytearray(target)
    for number, record in enumerate(patch.records, start=1):
        start = record.offset - bias
        end = start + len(record.data)
        if start < 0:
            raise ValueError(
                f"record #{number} at offset 0x{record.offset:X} starts before the target "
                f"once the bias {bias:#x} is subtracted"
            )
        if end > len(out):
            raise ValueError(
                f"record #{number} at offset 0x{record.offset:X} ({len(record.data)} byte(s), "
                f"bias {bias:#x}) reaches 0x{end:X}, past the end of the 0x{len(out):X}-byte "
                f"target"
            )
        out[start:end] = record.data
    if patch.truncate is not None:
        length = patch.truncate - bias
        if not 0 <= length <= len(out):
            raise ValueError(
                f"the truncation length 0x{patch.truncate:X} (bias {bias:#x}) does not lie "
                f"inside the 0x{len(out):X}-byte target"
            )
        del out[length:]
    return bytes(out)


def describe_ips(patch: IpsPatch) -> dict[str, Any]:
    """Summarize a patch for a report, as JSON-serializable data.

    The keys: ``format`` (``"ips"`` or ``"ips32"``), ``records`` (how many there are),
    ``patched_bytes`` (the sum of their sizes), ``lowest_offset`` and ``highest_offset``
    (the first and the last offset that a record writes; ``None`` for a patch without
    records) and ``truncate`` (the truncation length, or ``None``). Offsets are those
    of the records, bias included.
    """
    # A record without data (only a hand-made patch can have one) writes to no offset.
    written = [record for record in patch.records if record.data]
    return {
        "format": patch.format.value,
        "records": len(patch.records),
        "patched_bytes": sum(len(record.data) for record in written),
        "lowest_offset": min((record.offset for record in written), default=None),
        "highest_offset": max((record.end - 1 for record in written), default=None),
        "truncate": patch.truncate,
    }


# ------------------------------------------------------------------------ internals


class _Span(NamedTuple):
    """A contiguous range of offsets: one record of the file, or data yet to be cut."""

    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def _normalize(records: Iterable[IpsRecord | tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """Validate what :func:`build_ips` was given; return ``(offset, data)`` by offset.

    Raises:
        TypeError: a record, an offset or data of the wrong type.
        ValueError: empty data, a negative offset, or two records that overlap.
    """
    changes: list[tuple[int, bytes]] = []
    for index, item in enumerate(records):
        if isinstance(item, IpsRecord):
            offset, data = item.offset, item.data
        elif isinstance(item, tuple) and len(item) == 2:
            offset, data = item
        else:
            raise TypeError(
                f"record {index}: expected an IpsRecord or an (offset, data) pair, "
                f"got {type(item).__name__}"
            )
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError(
                f"record {index}: the offset must be an integer, got {type(offset).__name__}"
            )
        # Anything else would be turned into bytes of its own making by bytes(): an int
        # becomes that many zeros.
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError(f"record {index}: the data must be bytes, got {type(data).__name__}")
        if offset < 0:
            raise ValueError(f"record {index}: negative offset {offset}")
        payload = bytes(data)
        if not payload:
            raise ValueError(
                f"record {index} at offset 0x{offset:X}: empty data (a record changes at "
                f"least one byte)"
            )
        changes.append((offset, payload))

    changes.sort(key=lambda change: change[0])
    for (offset, data), (following, _) in itertools.pairwise(changes):
        if following < offset + len(data):
            raise ValueError(
                f"records overlap: the one at offset 0x{offset:X} ends at "
                f"0x{offset + len(data):X}, after the next one starts (0x{following:X})"
            )
    return changes


def _layout(changes: Sequence[tuple[int, bytes]], fmt: IpsFormat) -> tuple[list[_Span], int | None]:
    """Decide which records ``fmt`` needs for ``changes`` (sorted, not overlapping).

    Returns the span of every record, in file order, and the offset of the one byte of
    the target that the patch has to repeat (``None`` when there is none). Only offsets
    and sizes are looked at, never the data.
    """
    forbidden = fmt.forbidden_offset
    runs: list[_Span] = []
    borrowed: int | None = None
    for offset, data in changes:
        if offset != forbidden:
            runs.append(_Span(offset, len(data)))
        elif runs and runs[-1].end == forbidden:
            # The record before this one ends right here: this change continues it, and
            # no record has to start at the forbidden offset.
            runs[-1] = _Span(runs[-1].offset, runs[-1].size + len(data))
        else:
            # Nothing is written just before: start one byte earlier, on a byte that the
            # patch will rewrite with the value it already has. That byte is free: a
            # record covering it would end at the forbidden offset (the case above).
            borrowed = forbidden - 1
            runs.append(_Span(borrowed, 1 + len(data)))
    return [span for run in runs for span in _cut(run, forbidden)], borrowed


def _cut(run: _Span, forbidden: int) -> Iterator[_Span]:
    """Cut a run into records of at most :data:`MAX_RECORD_SIZE` bytes.

    ``run`` does not start at ``forbidden``, and no record yielded here does.
    """
    offset = run.offset
    while offset < run.end:
        size = min(MAX_RECORD_SIZE, run.end - offset)
        if offset + size == forbidden and forbidden < run.end:
            # The next record would start at the forbidden offset. Ending this one a
            # byte early moves the boundary, and the byte stays part of the run. At
            # least two bytes were about to be taken here, so one is always left.
            size -= 1
        yield _Span(offset, size)
        offset += size


def _check_limits(spans: Sequence[_Span], fmt: IpsFormat) -> None:
    """Refuse a layout that ``fmt`` cannot express.

    Raises:
        ValueError: a record starts beyond the highest offset of ``fmt`` or, in IPS32,
            ends beyond what 32 bits address.
    """
    for span in spans:
        if span.offset > fmt.max_offset:
            hint = (
                "; IPS32 can (leave fmt unset to have the format chosen)"
                if fmt is IpsFormat.IPS
                else ""
            )
            raise ValueError(
                f"{fmt.name} cannot express a record that starts at offset 0x{span.offset:X}: "
                f"its record offsets end at 0x{fmt.max_offset:X}{hint}"
            )
        if fmt is IpsFormat.IPS32 and span.end > _IPS32_END:
            raise ValueError(
                f"IPS32 cannot express the record at offset 0x{span.offset:X}: it would end "
                f"at 0x{span.end:X}, past 0x{_IPS32_END:X}"
            )


def _original_value(original_byte: Callable[[int], int] | None, offset: int, fmt: IpsFormat) -> int:
    """Return the value the target holds at ``offset``, as given by the caller's callback.

    Raises:
        TypeError: the callback returned something that is not an integer.
        ValueError: there is no callback, or it returned a value outside 0..255.
    """
    if original_byte is None:
        raise ValueError(
            f"a record starts at offset 0x{offset + 1:X}, which a reader of {fmt.name} takes "
            f"for the terminator {fmt.terminator!r}: the record has to start one byte "
            f"earlier and repeat the byte that the target holds at 0x{offset:X}. Pass "
            f"original_byte so that its value can be read"
        )
    value = original_byte(offset)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"original_byte(0x{offset:X}) must return an integer, got {type(value).__name__}"
        )
    if not 0 <= value <= 0xFF:
        raise ValueError(
            f"original_byte(0x{offset:X}) returned {value}, which is not the value of a byte "
            f"(0 to 255)"
        )
    return value


def _detect_format(raw: bytes) -> IpsFormat:
    """Return the format that the magic of ``raw`` announces.

    Raises:
        FormatError: ``raw`` starts with neither magic.
    """
    for fmt in IpsFormat:
        if raw.startswith(fmt.magic):
            return fmt
    found = raw[: len(IPS_MAGIC)]
    short = f" (the file is only {len(raw)} byte(s) long)" if len(raw) < len(IPS_MAGIC) else ""
    raise FormatError(
        f"IPS: wrong magic at offset 0x0: expected {IPS_MAGIC!r} or {IPS32_MAGIC!r}, "
        f"found {found!r}{short}"
    )


def _read(raw: bytes, position: int, size: int, what: str) -> bytes:
    """Return the ``size`` bytes of ``raw`` at ``position``.

    Raises:
        FormatError: fewer bytes are left; the message says ``what`` was being read.
    """
    chunk = raw[position : position + size]
    if len(chunk) != size:
        raise FormatError(
            f"IPS: truncated patch: {what} takes {size} byte(s) at offset 0x{position:X}, "
            f"only {len(chunk)} left (the file is 0x{len(raw):X} bytes long)"
        )
    return chunk
