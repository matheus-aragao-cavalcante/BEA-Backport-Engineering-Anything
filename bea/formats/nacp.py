"""``control.nacp``: the application control property (name, publisher, version).

A ``control.nacp`` describes an application to the system menu: its name and publisher
in every language it supports, the version text shown to the user, and a long list of
settings. It is *optional* input for BEA: most extracted game folders do not contain
one, and when one is there it only supplies what a person likes to see (a display name,
a publisher, a version). The authoritative program ID comes from ``main.npdm``.

Format
------
Public documentation: the switchbrew wiki, page "NACP". Every integer is little-endian.
A complete file is ``0x4000`` bytes long. What BEA reads::

    0x0000  0x3000 bytes  title block: the language entries (see below)
    0x302C  u32           SupportedLanguageFlag: bit i set = language i is supported
    0x3038  u64           PresenceGroupId
    0x3060  16 bytes      DisplayVersion: ASCII text, NUL-padded; it may fill the field,
                          which then has no terminator
    0x3070  u64           AddOnContentBaseId (normally the application ID + 0x1000)
    0x3078  u64           SaveDataOwnerId
    0x30B0  8 x u64       LocalCommunicationId
    0x30F8  u64           SeedForPseudoDeviceId
    0x3212  u8            ProgramIndex: which program of a multi-program application
    0x3215  u8            TitlesDataFormat: 0 = plain title block, 1 = compressed

One title entry is ``0x300`` bytes: a name of ``0x200`` bytes followed by a publisher of
``0x100`` bytes, both UTF-8, NUL-terminated and NUL-padded. An entry whose name is empty
is absent. Entry ``i`` and bit ``i`` of SupportedLanguageFlag belong to language ``i``:
:data:`LANGUAGES` names the languages 0 to 17, the indexes 18 to 31 are unassigned.

*Format 0* (the byte is zero in older files, which predate it): the block is 16 entries,
entry ``i`` at offset ``i * 0x300``.

*Format 1*: the block starts with a ``u16`` DataSize, between 1 and ``0x2FFE``, followed
by that many bytes of a raw DEFLATE stream (RFC 1951, without zlib or gzip wrapper) that
inflates to exactly ``0x6000`` bytes: 32 entries.

Some files hold a compressed block although their format byte is 0. A block announced as
plain is therefore test-inflated under the same limits and taken as compressed only when
the result is exactly ``0x6000`` bytes. Plain text does not pass that test by accident:
its first bytes would have to be the size and the beginning of a complete DEFLATE stream
of precisely that length.

The application ID is inferred
------------------------------
The file has no field for the ID of the application it describes. Several fields usually
hold that ID or a value derived from it, and :meth:`Nacp.inferred_application_id` lets
them vote. The result is an inference (a publisher may point any of those fields at
another title) and ranks below the program ID of ``main.npdm``.

Robustness
----------
* Text is never refused: bytes that do not decode are replaced with U+FFFD and the
  replacement is recorded in :attr:`Nacp.notes`, like every other non-fatal finding.
* A compressed block is inflated with a hard cap of one byte more than a valid block
  holds, so a stream built to expand to megabytes costs nothing; any other length, an
  unfinished stream, bytes left over after its end and decoder errors are all a
  :class:`~bea.errors.FormatError`.
* A file shorter than ``0x3070`` bytes (the end of DisplayVersion) is not a NACP. A file
  between that and ``0x4000`` bytes is read with the missing fields taken as zero; of a
  longer file only the first ``0x4000`` bytes are read. Both cases leave a note.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from bea.errors import FormatError
from bea.fsutil import fs_path

__all__ = [
    "LANGUAGES",
    "NACP_SIZE",
    "TITLE_ENTRY_SIZE",
    "Nacp",
    "NacpTitle",
    "build_nacp",
    "language_name",
    "parse_nacp",
    "read_nacp",
]

#: Size of a complete ``control.nacp``, in bytes.
NACP_SIZE = 0x4000
#: Size of one title entry (a name and a publisher), in bytes.
TITLE_ENTRY_SIZE = 0x300
#: Names of the languages 0 to 17, in index order: the order of the title entries and of
#: the bits of SupportedLanguageFlag.
LANGUAGES: tuple[str, ...] = (
    "AmericanEnglish",
    "BritishEnglish",
    "Japanese",
    "French",
    "German",
    "LatinAmericanSpanish",
    "Spanish",
    "Italian",
    "Dutch",
    "CanadianFrench",
    "Portuguese",
    "Russian",
    "Korean",
    "TraditionalChinese",
    "SimplifiedChinese",
    "BrazilianPortuguese",
    "Polish",
    "Thai",
)

#: Number of language indexes: the bits of SupportedLanguageFlag, and the entries of a
#: compressed title block.
_LANGUAGE_COUNT = 32
_DEFAULT_LANGUAGE = LANGUAGES[0]

_NAME_SIZE = 0x200
_PUBLISHER_SIZE = TITLE_ENTRY_SIZE - _NAME_SIZE
#: Size of the title block inside the file, whatever its format.
_TITLE_BLOCK_SIZE = 0x3000
_PLAIN_ENTRIES = _TITLE_BLOCK_SIZE // TITLE_ENTRY_SIZE
_INFLATED_SIZE = _LANGUAGE_COUNT * TITLE_ENTRY_SIZE
_DATA_SIZE = struct.Struct("<H")
#: Largest DEFLATE stream that fits the title block behind its size field.
_MAX_DEFLATE_SIZE = _TITLE_BLOCK_SIZE - _DATA_SIZE.size
_FORMAT_PLAIN = 0
_FORMAT_COMPRESSED = 1

_SUPPORTED_LANGUAGE_FLAG = 0x302C
_PRESENCE_GROUP_ID = 0x3038
_DISPLAY_VERSION = 0x3060
_DISPLAY_VERSION_SIZE = 0x10
_ADD_ON_CONTENT_BASE_ID = 0x3070
_SAVE_DATA_OWNER_ID = 0x3078
_LOCAL_COMMUNICATION_ID = 0x30B0
_LOCAL_COMMUNICATION_ID_COUNT = 8
_SEED_FOR_PSEUDO_DEVICE_ID = 0x30F8
_PROGRAM_INDEX = 0x3212
_TITLES_DATA_FORMAT = 0x3215

#: A file must reach the end of DisplayVersion to be taken for a NACP at all.
_MIN_SIZE = _DISPLAY_VERSION + _DISPLAY_VERSION_SIZE
#: ``read_nacp`` reads no more than this (1 MiB); a NACP is 16 KiB.
_READ_LIMIT = 1024 * 1024
_ID_LIMIT = 1 << 64

#: AddOnContentBaseId is normally the application ID plus this.
_ADD_ON_CONTENT_DISTANCE = 0x1000
#: The low 13 bits of an ID tell the programs, the update and the add-on content of one
#: application apart; cleared, they all give the ID of the application.
_ID_VARIANT_BITS = 0x1FFF
#: The range of application IDs, both ends included.
_FIRST_APPLICATION_ID = (0x0100 << 48) | 0x10000
_LAST_APPLICATION_ID = (0x0200 << 48) - 1


def language_name(index: int) -> str:
    """Return the name of language ``index``, the index of a title entry or of a flag bit.

    The indexes 0 to 17 are named by :data:`LANGUAGES`; 18 to 31 are unassigned and are
    called ``"Language<index>"``.

    Raises:
        ValueError: ``index`` is outside ``0..31``.
    """
    if not 0 <= index < _LANGUAGE_COUNT:
        raise ValueError(f"a language index is 0..{_LANGUAGE_COUNT - 1}, got {index}")
    return LANGUAGES[index] if index < len(LANGUAGES) else f"Language{index}"


_LANGUAGE_NAMES = frozenset(language_name(index) for index in range(_LANGUAGE_COUNT))


@dataclass(frozen=True, slots=True)
class NacpTitle:
    """The name and the publisher of the application in one language."""

    #: Index of the entry in the title block: the language index.
    index: int
    #: Name of that language (see :func:`language_name`).
    language: str
    name: str
    publisher: str


@dataclass(frozen=True, slots=True)
class Nacp:
    """What BEA reads from a ``control.nacp``."""

    #: The title entries that have a name, in index order.
    titles: tuple[NacpTitle, ...]
    #: Version text shown to the user, verbatim: free-form, not necessarily ``x.y.z``.
    display_version: str
    #: Names of the languages whose bit is set in SupportedLanguageFlag, in index order.
    supported_languages: tuple[str, ...]
    presence_group_id: int
    add_on_content_base_id: int
    save_data_owner_id: int
    #: Always 8 values.
    local_communication_ids: tuple[int, ...]
    seed_for_pseudo_device_id: int
    #: Which program of a multi-program application this file belongs to (usually 0).
    program_index: int
    #: ``True`` when the title entries were stored compressed.
    titles_compressed: bool
    #: Non-fatal findings, as full sentences: an unexpected file size, text that is not
    #: valid UTF-8, a compressed title block that is not announced as such ...
    notes: tuple[str, ...] = ()

    def title(self, preferred: str | None = None) -> NacpTitle | None:
        """Return the title entry to show to a person.

        That is the entry of the ``preferred`` language (a name from :data:`LANGUAGES`,
        ``"AmericanEnglish"`` by default) when the file has it, else the first entry in
        index order, else ``None`` (the file has no entry at all).

        Raises:
            ValueError: ``preferred`` is not the name of a language.
        """
        wanted = _DEFAULT_LANGUAGE if preferred is None else preferred
        if wanted not in _LANGUAGE_NAMES:
            raise ValueError(
                f"unknown language {wanted!r}: expected a name from LANGUAGES, "
                f"such as {_DEFAULT_LANGUAGE!r}"
            )
        for entry in self.titles:
            if entry.language == wanted:
                return entry
        return self.titles[0] if self.titles else None

    def inferred_application_id(self) -> int | None:
        """Guess the ID of the application from the fields that usually hold it.

        Five values vote, in this order: SaveDataOwnerId, PresenceGroupId,
        AddOnContentBaseId minus ``0x1000`` (when that field is at least ``0x1000``), the
        first LocalCommunicationId and SeedForPseudoDeviceId. A value that is zero does
        not vote. The low 13 bits of the others are cleared (they tell the programs, the
        update and the add-on content of one application apart), and what does not fall
        in the range of application IDs is dropped. The most frequent remaining value
        wins; between equally frequent values, the one that voted first.

        This is an inference: a publisher may point any of these fields at another
        title. A caller must rank it below the program ID of ``main.npdm``.

        Returns:
            The inferred application ID, or ``None`` when no field holds a usable value.
        """
        candidates = [self.save_data_owner_id, self.presence_group_id]
        if self.add_on_content_base_id >= _ADD_ON_CONTENT_DISTANCE:
            candidates.append(self.add_on_content_base_id - _ADD_ON_CONTENT_DISTANCE)
        candidates.extend(self.local_communication_ids[:1])
        candidates.append(self.seed_for_pseudo_device_id)

        votes: dict[int, int] = {}
        for candidate in candidates:
            if candidate == 0:
                continue
            application_id = candidate & ~_ID_VARIANT_BITS
            if _FIRST_APPLICATION_ID <= application_id <= _LAST_APPLICATION_ID:
                votes[application_id] = votes.get(application_id, 0) + 1
        if not votes:
            return None
        # A dict iterates in insertion order and max() keeps the first of several equal
        # maxima: a tie goes to the value that voted first.
        return max(votes, key=votes.__getitem__)

    def inferred_program_id(self) -> int | None:
        """Guess the program ID: the inferred application ID plus ProgramIndex.

        The same caveat applies as for :meth:`inferred_application_id`.
        """
        application_id = self.inferred_application_id()
        return None if application_id is None else application_id + self.program_index


def parse_nacp(data: bytes | bytearray | memoryview) -> Nacp:
    """Parse the content of a ``control.nacp``.

    Text is decoded leniently and sizes other than ``0x4000`` bytes are tolerated down
    to ``0x3070`` bytes; what was tolerated is listed in :attr:`Nacp.notes` (see the
    module documentation). A TitlesDataFormat other than 0 and 1 is not guessed at: the
    title entries are then left out, with a note.

    Raises:
        FormatError: ``data`` is shorter than ``0x3070`` bytes; or TitlesDataFormat is 1
            and the title block is not a valid compressed block: a DataSize outside
            ``1..0x2FFE``, a corrupt or unfinished DEFLATE stream, bytes left after its
            end, or a stream that inflates to anything but ``0x6000`` bytes.
    """
    # The part that matters is copied out and the views are released on the way out of
    # this block: a view kept alive by a traceback would leave a caller's bytearray
    # locked against resizing.
    with memoryview(data) as raw, raw.cast("B") as view:
        size = len(view)
        head = bytes(view[:NACP_SIZE])
    if size < _MIN_SIZE:
        raise FormatError(
            f"NACP: truncated file: only 0x{size:X} bytes, the fields read from it end at "
            f"0x{_MIN_SIZE:X} (a complete file is 0x{NACP_SIZE:X} bytes)"
        )

    notes: list[str] = []
    if size < NACP_SIZE:
        notes.append(
            f"The file is 0x{size:X} bytes long instead of 0x{NACP_SIZE:X}; the fields "
            f"that are missing were read as zero."
        )
    elif size > NACP_SIZE:
        notes.append(
            f"The file is 0x{size:X} bytes long instead of 0x{NACP_SIZE:X}; only its "
            f"first 0x{NACP_SIZE:X} bytes were read."
        )

    block, titles_compressed = _title_block(head, notes)
    titles = _read_titles(block, notes)

    raw_version = _until_nul(head[_DISPLAY_VERSION : _DISPLAY_VERSION + _DISPLAY_VERSION_SIZE])
    if not raw_version.isascii():
        notes.append(
            "The display version is not ASCII; the undecodable bytes were replaced with U+FFFD."
        )
    language_flag = _read_uint(head, _SUPPORTED_LANGUAGE_FLAG, 4)
    return Nacp(
        titles=titles,
        display_version=raw_version.decode("ascii", errors="replace"),
        supported_languages=tuple(
            language_name(index) for index in range(_LANGUAGE_COUNT) if language_flag >> index & 1
        ),
        presence_group_id=_read_uint(head, _PRESENCE_GROUP_ID, 8),
        add_on_content_base_id=_read_uint(head, _ADD_ON_CONTENT_BASE_ID, 8),
        save_data_owner_id=_read_uint(head, _SAVE_DATA_OWNER_ID, 8),
        local_communication_ids=tuple(
            _read_uint(head, _LOCAL_COMMUNICATION_ID + 8 * index, 8)
            for index in range(_LOCAL_COMMUNICATION_ID_COUNT)
        ),
        seed_for_pseudo_device_id=_read_uint(head, _SEED_FOR_PSEUDO_DEVICE_ID, 8),
        program_index=_read_uint(head, _PROGRAM_INDEX, 1),
        titles_compressed=titles_compressed,
        notes=tuple(notes),
    )


def read_nacp(path: str | os.PathLike[str]) -> Nacp:
    """Read and parse the ``control.nacp`` stored at ``path``.

    At most 1 MiB is read: a NACP is 16 KiB, so a file larger than that is something
    else and is refused instead of being loaded.

    Raises:
        FormatError: the file is larger than 1 MiB, or is not a valid NACP (see
            :func:`parse_nacp`).
        OSError: the file cannot be opened or read.
    """
    with open(fs_path(path), "rb") as handle:
        data = handle.read(_READ_LIMIT + 1)
    if len(data) > _READ_LIMIT:
        raise FormatError(
            f"NACP: the file is larger than 0x{_READ_LIMIT:X} bytes (1 MiB): not a NACP, "
            f"which holds 0x{NACP_SIZE:X} bytes"
        )
    return parse_nacp(data)


def build_nacp(
    titles: Mapping[int, tuple[str, str]],
    *,
    display_version: str = "",
    application_id: int = 0,
    supported_languages: Iterable[int] | None = None,
    program_index: int = 0,
    compressed: bool = False,
) -> bytes:
    """Build a ``control.nacp`` of exactly ``0x4000`` bytes.

    A writer for tests and for the synthetic-game generator. ``titles`` maps a language
    index to ``(name, publisher)``. A plain title block (the default) holds the indexes 0
    to 15; with ``compressed`` the block holds all 32 indexes, is stored as a raw DEFLATE
    stream behind its ``u16`` size, and TitlesDataFormat is set to 1. An entry given an
    empty name is written as it is; readers take such an entry for absent.

    ``supported_languages`` lists the language indexes whose bit is set in
    SupportedLanguageFlag; by default, those of ``titles``. When ``application_id`` is
    not 0 it is written to SaveDataOwnerId, PresenceGroupId, the first
    LocalCommunicationId and SeedForPseudoDeviceId, and ``application_id + 0x1000`` to
    AddOnContentBaseId: the fields :meth:`Nacp.inferred_application_id` looks at. Every
    byte not mentioned here is zero.

    Raises:
        TypeError: an index or an ID is not an integer, or a text is not a ``str``.
        ValueError: a language index outside ``0..31``, or above 15 without
            ``compressed``; a name longer than ``0x1FF`` UTF-8 bytes or a publisher longer
            than ``0xFF`` (each field keeps a terminator); a display version that is not
            ASCII or is longer than 16 bytes; a text that contains a NUL;
            ``application_id`` (or the AddOnContentBaseId derived from it) outside
            ``0..2**64-1``; ``program_index`` outside ``0..255``; compressed titles that
            do not fit the ``0x2FFE`` bytes of the block.
    """
    capacity = _LANGUAGE_COUNT if compressed else _PLAIN_ENTRIES
    block = bytearray(capacity * TITLE_ENTRY_SIZE)
    for index, (name, publisher) in titles.items():
        _check_language_index(index)
        if index >= capacity:
            raise ValueError(
                f"language index {index} ({language_name(index)}): a plain title block "
                f"holds the indexes 0..{capacity - 1} only; pass compressed=True"
            )
        label = f"of language index {index} ({language_name(index)})"
        raw_name = _encode_text(name, _NAME_SIZE - 1, f"the name {label}")
        raw_publisher = _encode_text(publisher, _PUBLISHER_SIZE - 1, f"the publisher {label}")
        # The "s" format pads with NULs up to the size of the field.
        struct.pack_into(
            f"{_NAME_SIZE}s{_PUBLISHER_SIZE}s",
            block,
            index * TITLE_ENTRY_SIZE,
            raw_name,
            raw_publisher,
        )

    raw_version = _encode_text(
        display_version, _DISPLAY_VERSION_SIZE, "the display version", encoding="ascii"
    )

    flagged: Iterable[int] = titles if supported_languages is None else supported_languages
    language_flag = 0
    for index in flagged:
        _check_language_index(index)
        language_flag |= 1 << index

    _check_integer(application_id, "application_id")
    add_on_content_base_id = application_id + _ADD_ON_CONTENT_DISTANCE if application_id else 0
    if not 0 <= application_id < _ID_LIMIT or add_on_content_base_id >= _ID_LIMIT:
        raise ValueError(
            f"application_id out of range (it and application_id + "
            f"0x{_ADD_ON_CONTENT_DISTANCE:X} must be 64-bit IDs): {application_id}"
        )
    _check_integer(program_index, "program_index")
    if not 0 <= program_index <= 0xFF:
        raise ValueError(f"program_index must be 0..255, got {program_index}")

    out = bytearray(NACP_SIZE)
    if compressed:
        deflater = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-zlib.MAX_WBITS)
        stream = deflater.compress(block) + deflater.flush()
        if len(stream) > _MAX_DEFLATE_SIZE:
            raise ValueError(
                f"the compressed titles take 0x{len(stream):X} bytes, the title block "
                f"holds at most 0x{_MAX_DEFLATE_SIZE:X}"
            )
        _DATA_SIZE.pack_into(out, 0, len(stream))
        struct.pack_into(f"{len(stream)}s", out, _DATA_SIZE.size, stream)
        out[_TITLES_DATA_FORMAT] = _FORMAT_COMPRESSED
    else:
        struct.pack_into(f"{_TITLE_BLOCK_SIZE}s", out, 0, bytes(block))
    struct.pack_into("<I", out, _SUPPORTED_LANGUAGE_FLAG, language_flag)
    struct.pack_into(f"{_DISPLAY_VERSION_SIZE}s", out, _DISPLAY_VERSION, raw_version)
    struct.pack_into("<Q", out, _ADD_ON_CONTENT_BASE_ID, add_on_content_base_id)
    for offset in (
        _SAVE_DATA_OWNER_ID,
        _PRESENCE_GROUP_ID,
        _LOCAL_COMMUNICATION_ID,
        _SEED_FOR_PSEUDO_DEVICE_ID,
    ):
        struct.pack_into("<Q", out, offset, application_id)
    out[_PROGRAM_INDEX] = program_index
    return bytes(out)


# ------------------------------------------------------------------------ internals


def _title_block(head: bytes, notes: list[str]) -> tuple[bytes, bool]:
    """Return the title entries of a file as plain bytes, and whether they were compressed.

    Raises:
        FormatError: the block is announced as compressed and is not a valid one.
    """
    stored = head[:_TITLE_BLOCK_SIZE]
    titles_format = _read_uint(head, _TITLES_DATA_FORMAT, 1)
    if titles_format == _FORMAT_COMPRESSED:
        return _inflate_titles(stored), True
    if titles_format != _FORMAT_PLAIN:
        notes.append(
            f"TitlesDataFormat (offset 0x{_TITLES_DATA_FORMAT:X}) is {titles_format}, a "
            f"value this version does not know (it knows 0 and 1): the title entries "
            f"were not read."
        )
        return b"", False
    # Announced as plain, which a compressed block sometimes is as well: see the module
    # documentation. Here a failed inflation is the normal case, not an error.
    try:
        inflated = _inflate_titles(stored)
    except FormatError:
        return stored, False
    notes.append(
        f"The title block is compressed although TitlesDataFormat (offset "
        f"0x{_TITLES_DATA_FORMAT:X}) is 0; it was read as a compressed block."
    )
    return inflated, True


def _inflate_titles(stored: bytes) -> bytes:
    """Inflate a compressed title block into the ``0x6000`` bytes of its 32 entries.

    Raises:
        FormatError: DataSize is outside ``1..0x2FFE``; the DEFLATE stream is corrupt,
            unfinished or followed by other bytes; it inflates to another length.
    """
    (data_size,) = _DATA_SIZE.unpack_from(stored)
    if not 1 <= data_size <= _MAX_DEFLATE_SIZE:
        raise FormatError(
            f"NACP: compressed title block: DataSize at offset 0x0 is 0x{data_size:X}, "
            f"expected 0x1..0x{_MAX_DEFLATE_SIZE:X}"
        )
    stream = stored[_DATA_SIZE.size : _DATA_SIZE.size + data_size]
    inflater = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
    try:
        # Never ask for more than one byte beyond a valid block: that is enough to tell
        # "too long" from "right", whatever a hostile stream would expand to.
        inflated = inflater.decompress(stream, _INFLATED_SIZE + 1)
    except zlib.error as exc:
        raise FormatError(
            f"NACP: compressed title block: the 0x{data_size:X} bytes at offset "
            f"0x{_DATA_SIZE.size:X} are not a valid DEFLATE stream ({exc})"
        ) from exc
    if len(inflated) > _INFLATED_SIZE:
        raise FormatError(
            f"NACP: compressed title block: the DEFLATE stream inflates to more than the "
            f"0x{_INFLATED_SIZE:X} bytes of {_LANGUAGE_COUNT} title entries"
        )
    if not inflater.eof:
        raise FormatError(
            f"NACP: compressed title block: the DEFLATE stream does not end within its "
            f"0x{data_size:X} bytes (0x{len(inflated):X} bytes inflated so far)"
        )
    if len(inflated) != _INFLATED_SIZE:
        raise FormatError(
            f"NACP: compressed title block: the DEFLATE stream inflates to "
            f"0x{len(inflated):X} bytes, expected 0x{_INFLATED_SIZE:X} "
            f"({_LANGUAGE_COUNT} title entries)"
        )
    if inflater.unused_data:
        raise FormatError(
            f"NACP: compressed title block: {len(inflater.unused_data)} of the "
            f"0x{data_size:X} bytes of DataSize are left after the end of the DEFLATE stream"
        )
    return inflated


def _read_titles(block: bytes, notes: list[str]) -> tuple[NacpTitle, ...]:
    """Return the entries of a plain (or inflated) title block that have a name."""
    titles = []
    for index in range(len(block) // TITLE_ENTRY_SIZE):
        start = index * TITLE_ENTRY_SIZE
        raw_name = _until_nul(block[start : start + _NAME_SIZE])
        if not raw_name:
            continue  # absent, whatever its publisher field holds
        raw_publisher = _until_nul(block[start + _NAME_SIZE : start + TITLE_ENTRY_SIZE])
        language = language_name(index)
        label = f"of title entry {index} ({language})"
        titles.append(
            NacpTitle(
                index=index,
                language=language,
                name=_decode_text(raw_name, f"name {label}", notes),
                publisher=_decode_text(raw_publisher, f"publisher {label}", notes),
            )
        )
    return tuple(titles)


def _until_nul(field: bytes) -> bytes:
    """Return the text of a NUL-padded field: its bytes up to the first NUL, if any."""
    end = field.find(b"\x00")
    return field if end < 0 else field[:end]


def _decode_text(raw: bytes, what: str, notes: list[str]) -> str:
    """Decode UTF-8 text, replacing what does not decode and leaving a note about it."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        notes.append(
            f"The {what} is not valid UTF-8; the undecodable bytes were replaced with U+FFFD."
        )
        return raw.decode("utf-8", errors="replace")


def _read_uint(head: bytes, offset: int, size: int) -> int:
    """Return the unsigned integer of ``size`` bytes at ``offset``.

    A field that a short file does not hold completely is missing and reads as zero: the
    bytes of half a number would be a value the file never stated.
    """
    field = head[offset : offset + size]
    return int.from_bytes(field, "little") if len(field) == size else 0


def _check_integer(value: int, what: str) -> None:
    """Refuse anything that is not an integer; ``bool`` is one only by accident."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer, got {type(value).__name__}")


def _check_language_index(index: int) -> None:
    """Refuse anything that is not a language index."""
    _check_integer(index, "a language index")
    if not 0 <= index < _LANGUAGE_COUNT:
        raise ValueError(f"a language index is 0..{_LANGUAGE_COUNT - 1}, got {index}")


def _encode_text(text: str, limit: int, what: str, *, encoding: str = "utf-8") -> bytes:
    """Encode a text for a field that holds at most ``limit`` bytes of it.

    Raises:
        TypeError: ``text`` is not a ``str``.
        ValueError: it cannot be encoded, is too long or contains a NUL.
    """
    if not isinstance(text, str):
        raise TypeError(f"{what} must be text, got {type(text).__name__}")
    try:
        raw = text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(f"{what} cannot be encoded as {encoding}: {exc.reason}") from exc
    if len(raw) > limit:
        raise ValueError(
            f"{what} takes {len(raw)} bytes as {encoding}, the field holds at most {limit}"
        )
    if b"\x00" in raw:
        raise ValueError(f"{what} contains a NUL character")
    return raw
