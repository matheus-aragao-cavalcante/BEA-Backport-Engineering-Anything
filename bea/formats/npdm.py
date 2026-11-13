"""``main.npdm``: the program metadata of an ExeFS, read for the program ID.

Every ExeFS holds a ``main.npdm`` next to its executable modules. It tells the system how
to start the process (address space, main thread, permissions) and which program it is.
BEA wants one thing from it: the **program ID** (the "title ID"), the ID the system runs
the process under and the name that LayeredFS mod folders carry. A few more header fields
are read for diagnostics. The permission descriptors are ignored, and the signature and
the public key of the ACID block are skipped: nothing is verified cryptographically and
no key is ever needed.

Format
------
Public documentation: the switchbrew wiki, page "NPDM". Every integer is little-endian.

The file has three parts: a META header at offset 0 and two blocks, ACID and ACI0, that
the header locates by offset and size. Their order and their positions differ from file
to file, so the header is always followed and nothing is assumed about where a block is.

META header, ``0x80`` bytes::

    +0x00  4 bytes   magic "META"
    +0x0C  u8        Flags0: bit 0 = 64-bit instructions (1 = AArch64, 0 = AArch32);
                     bits 1-3 = address-space type (3 is the usual value of 64-bit games)
    +0x0E  u8        main thread priority
    +0x0F  u8        main thread core number
    +0x14  u32       system resource size
    +0x18  u32       version
    +0x1C  u32       main thread stack size
    +0x20  16 bytes  name, NUL-padded text: the name of the *process* (normally the
                     literal text "Application"), not the title of the game
    +0x30  16 bytes  product code (usually zero)
    +0x70  u32       AciOffset
    +0x74  u32       AciSize
    +0x78  u32       AcidOffset
    +0x7C  u32       AcidSize

ACI0 block, at ``AciOffset``::

    +0x00  4 bytes   magic "ACI0"
    +0x10  u64       ProgramId: the ID the system runs the process under

ACID block, at ``AcidOffset``::

    +0x000  0x100 bytes  RSA signature
    +0x100  0x100 bytes  RSA public key
    +0x200  4 bytes      magic "ACID"
    +0x204  u32          size of the signed area (everything after the signature)
    +0x20C  u32          flags
    +0x210  u64          ProgramIdMin
    +0x218  u64          ProgramIdMax

The documentation also gives a second size for the signature and the key, ``0x180``
bytes each (3072-bit keys). That moves the magic to ``+0x300`` and the two IDs to
``+0x310`` and ``+0x318``; both layouts are read.

Both blocks end with the offset and size of three permission descriptors (``+0x20`` to
``+0x37`` of ACI0 and of the ACID header). BEA does not read them; :func:`build_npdm`
writes them empty.

What a program loader checks before it accepts a file: ``0x80 <= file size <= 0x8000``;
``AciSize >= 0x40`` and ``AcidSize >= 0x240``; both blocks start after the META header
and end inside the file; ``ProgramIdMin <= ProgramId <= ProgramIdMax``.

Robustness
----------
No offset or size is taken at its word: each one is checked against the bytes that are
really there before anything is read, so that a truncated or hostile file ends in a
:class:`~bea.errors.FormatError` and never in a crash.

Only what stands between BEA and the program ID is fatal: a file too short for the META
header, a wrong magic, an ACI0 block that lies outside the file, is too small to hold
the ID or lacks its own magic. Everything else a loader would object to (an unreadable
ACID block, an ID outside the range that block allows, an oversized file ...) is a
*note* on the result, :attr:`Npdm.notes`: the ID of an odd but readable file stays
available and the caller decides what the notes are worth.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from bea.errors import FormatError
from bea.fsutil import fs_path
from bea.hexutil import normalize_title_id

__all__ = [
    "MAX_NPDM_SIZE",
    "NPDM_MAGIC",
    "Npdm",
    "build_npdm",
    "is_npdm",
    "parse_npdm",
    "read_npdm",
]

#: The four bytes every NPDM starts with.
NPDM_MAGIC = b"META"
#: Largest NPDM a program loader accepts, in bytes.
MAX_NPDM_SIZE = 0x8000

# magic, Flags0, main thread priority, main thread core number, system resource size,
# version, main thread stack size, name, product code, AciOffset, AciSize, AcidOffset,
# AcidSize. The skipped bytes are reserved or of no interest here.
_META = struct.Struct("<4s8xBxBB4xIII16s16s48xIIII")
# magic, size of the signed area, flags, ProgramIdMin, ProgramIdMax, then offset and size
# of the three permission descriptors (filesystem access, service access, kernel).
_ACID_HEADER = struct.Struct("<4sI4xIQQIIIIII8x")
# magic, ProgramId, then offset and size of the same three descriptors.
_ACI_HEADER = struct.Struct("<4s12xQ8xIIIIII8x")

_ACI_MAGIC = b"ACI0"
_ACID_MAGIC = b"ACID"
#: Offset of the ProgramId inside the ACI0 block.
_ACI_PROGRAM_ID = 0x10
#: An ACI0 block must reach at least this far for the ProgramId to lie inside it.
_ACI_READABLE_SIZE = _ACI_PROGRAM_ID + 8
#: Size of a 2048-bit RSA signature or public key; two of them precede the ACID header.
_RSA_SIZE = 0x100
#: Where the ACID header may start inside its block: after a signature and a key of
#: 0x100 bytes each, or of 0x180 bytes each (3072-bit keys).
_ACID_HEADER_OFFSETS = (2 * _RSA_SIZE, 2 * 0x180)
#: Smallest ACID block a program loader accepts: signature, key and header.
_ACID_MIN_SIZE = _ACID_HEADER_OFFSETS[0] + _ACID_HEADER.size
#: ``read_npdm`` reads no more than this (1 MiB); an NPDM is a few kilobytes.
_READ_LIMIT = 1024 * 1024
_ID_LIMIT = 1 << 64
#: Longest name ``build_npdm`` writes: the field is 16 bytes and keeps a terminator.
_MAX_NAME_BYTES = 15
#: Highest address-space type that fits the three bits of Flags0.
_MAX_ADDRESS_SPACE = 7

# What build_npdm writes besides the IDs: the fixed values of its minimal file.
_BUILT_PRIORITY = 44
_BUILT_STACK_SIZE = 0x100000
_BUILT_ACID_FLAGS = 1  # bit 0: production
_BUILT_ACID_OFFSET = _META.size
_BUILT_ACI_OFFSET = _BUILT_ACID_OFFSET + _ACID_MIN_SIZE


@dataclass(frozen=True, slots=True)
class Npdm:
    """What BEA reads from a ``main.npdm``."""

    #: ProgramId of the ACI0 block: the ID the system runs the process under.
    program_id: int
    #: Name of the process (normally ``"Application"``). Diagnostic only: this is not
    #: the title of the game.
    name: str
    #: ``True`` for AArch64 code, ``False`` for AArch32.
    is_64bit: bool
    #: Address-space type, 0 to 7 (3 is the usual value of 64-bit games).
    address_space: int
    system_resource_size: int
    main_thread_stack_size: int
    #: Lowest program ID the ACID block allows; ``None`` when that block is missing or
    #: unreadable.
    program_id_min: int | None
    #: Highest program ID the ACID block allows; ``None`` like ``program_id_min``.
    program_id_max: int | None
    #: Non-fatal findings, as full sentences: an unreadable ACID block, a program ID
    #: outside the range that block allows, an oversized file, 32-bit code ...
    notes: tuple[str, ...] = ()

    @property
    def title_id(self) -> str:
        """The program ID as 16 uppercase hex digits, the form used to name mod folders."""
        return normalize_title_id(self.program_id)


def is_npdm(prefix: bytes) -> bool:
    """Tell whether a file that starts with ``prefix`` announces itself as an NPDM.

    Only the magic is looked at: :func:`parse_npdm` decides whether the file is valid.
    """
    return prefix[: len(NPDM_MAGIC)] == NPDM_MAGIC


def parse_npdm(data: bytes | bytearray | memoryview) -> Npdm:
    """Parse the content of a ``main.npdm``.

    The META header is read, then the two blocks it points at, wherever they are; what
    else ``data`` holds (padding, permission descriptors) is ignored. Whatever a program
    loader would object to but does not hide the program ID is reported in
    :attr:`Npdm.notes` instead of being raised (see the module documentation).

    Raises:
        FormatError: ``data`` does not start with ``META`` or is shorter than the 0x80-byte
            META header; the ACI0 block is smaller than 0x18 bytes (the program ID would
            not lie inside it), does not fit in ``data``, or does not start with ``ACI0``.
    """
    # Only small pieces are copied out, and the views are released on the way out of this
    # block, error or not: a view kept alive by a traceback would leave a caller's
    # bytearray locked against resizing.
    with memoryview(data) as raw, raw.cast("B") as view:
        size = len(view)
        magic = bytes(view[: len(NPDM_MAGIC)])
        # Compared over the bytes that are there: a file too short to hold the magic is
        # "truncated" only as long as what it does hold agrees with the magic.
        if magic != NPDM_MAGIC[: len(magic)]:
            raise FormatError(
                f"NPDM: wrong magic at offset 0x0: expected {NPDM_MAGIC!r}, found {magic!r}"
            )
        if size < _META.size:
            raise FormatError(
                f"NPDM: truncated header: the META header is 0x{_META.size:X} bytes, "
                f"only 0x{size:X} available"
            )
        (
            _magic,
            flags0,
            _priority,
            _core_number,
            system_resource_size,
            _version,
            main_thread_stack_size,
            raw_name,
            _product_code,
            aci_offset,
            aci_size,
            acid_offset,
            acid_size,
        ) = _META.unpack_from(view)

        if aci_size < _ACI_READABLE_SIZE:
            raise FormatError(
                f"NPDM: AciSize at offset 0x74 is 0x{aci_size:X}: an ACI0 block of less than "
                f"0x{_ACI_READABLE_SIZE:X} bytes cannot hold the program ID"
            )
        if aci_offset + aci_size > size:
            raise FormatError(
                f"NPDM: the ACI0 block (AciOffset 0x{aci_offset:X}, AciSize 0x{aci_size:X}) "
                f"ends at 0x{aci_offset + aci_size:X}, the file is only 0x{size:X} bytes long"
            )
        aci = bytes(view[aci_offset : aci_offset + _ACI_READABLE_SIZE])
        acid = _read_acid(view, acid_offset, acid_size)

    if aci[: len(_ACI_MAGIC)] != _ACI_MAGIC:
        raise FormatError(
            f"NPDM: wrong magic at offset 0x{aci_offset:X} (AciOffset): expected "
            f"{_ACI_MAGIC!r}, found {aci[: len(_ACI_MAGIC)]!r}"
        )
    program_id = int.from_bytes(aci[_ACI_PROGRAM_ID:], "little")

    notes: list[str] = []
    if size > MAX_NPDM_SIZE:
        notes.append(
            f"The file is 0x{size:X} bytes long; a program loader accepts at most "
            f"0x{MAX_NPDM_SIZE:X} bytes."
        )
    is_64bit = bool(flags0 & 1)
    if not is_64bit:
        notes.append("The program runs 32-bit code (AArch32): bit 0 of Flags0 is clear.")
    if aci_offset < _META.size:
        notes.append(
            f"The ACI0 block starts at offset 0x{aci_offset:X}, inside the META header; "
            f"a program loader refuses such a file."
        )
    if aci_size < _ACI_HEADER.size:
        notes.append(
            f"The ACI0 block is 0x{aci_size:X} bytes long; a program loader requires at "
            f"least 0x{_ACI_HEADER.size:X} bytes."
        )

    program_id_min: int | None = None
    program_id_max: int | None = None
    if isinstance(acid, str):
        notes.append(acid)
    else:
        program_id_min, program_id_max = acid
        if not program_id_min <= program_id <= program_id_max:
            notes.append(
                f"The program ID {program_id:016X} is outside the range "
                f"{program_id_min:016X}..{program_id_max:016X} that the ACID block allows; "
                f"a program loader refuses such a file."
            )

    return Npdm(
        program_id=program_id,
        # ASCII in practice. The name is only ever shown to a person, so bytes that do
        # not decode are replaced instead of being refused.
        name=raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        is_64bit=is_64bit,
        address_space=(flags0 >> 1) & _MAX_ADDRESS_SPACE,
        system_resource_size=system_resource_size,
        main_thread_stack_size=main_thread_stack_size,
        program_id_min=program_id_min,
        program_id_max=program_id_max,
        notes=tuple(notes),
    )


def read_npdm(path: str | os.PathLike[str]) -> Npdm:
    """Read and parse the ``main.npdm`` stored at ``path``.

    At most 1 MiB is read: an NPDM is a few kilobytes, so a larger file is something
    else and is refused instead of being loaded.

    Raises:
        FormatError: the file is larger than 1 MiB, or is not a valid NPDM (see
            :func:`parse_npdm`).
        OSError: the file cannot be opened or read.
    """
    with open(fs_path(path), "rb") as handle:
        data = handle.read(_READ_LIMIT + 1)
    if len(data) > _READ_LIMIT:
        raise FormatError(
            f"NPDM: the file is larger than 0x{_READ_LIMIT:X} bytes (1 MiB): not an NPDM, "
            f"which holds at most 0x{MAX_NPDM_SIZE:X} bytes"
        )
    return parse_npdm(data)


def build_npdm(
    program_id: int,
    *,
    name: str = "Application",
    is_64bit: bool = True,
    address_space: int = 3,
    program_id_min: int | None = None,
    program_id_max: int | None = None,
) -> bytes:
    """Build a minimal ``main.npdm`` of ``0x300`` bytes for ``program_id``.

    A writer for tests and for the synthetic-game generator. The file is a META header
    (main thread priority 44, a stack of 0x100000 bytes), an ACID block at ``0x80`` whose
    signature and key are zero, and an ACI0 block at ``0x2C0``; no block has permission
    descriptors. ``program_id_min`` and ``program_id_max`` default to ``program_id``.
    They are written as given, in any order and whether or not they enclose
    ``program_id``: a file that :func:`parse_npdm` has something to note about can be
    built on purpose.

    Raises:
        TypeError: an ID or ``address_space`` is not an integer, or ``name`` is not text.
        ValueError: an ID is outside ``0..2**64-1``; ``name`` is not ASCII, is longer
            than 15 bytes or contains a NUL; ``address_space`` is outside ``0..7``.
    """
    _check_id(program_id, "program_id")
    id_min = program_id if program_id_min is None else program_id_min
    id_max = program_id if program_id_max is None else program_id_max
    _check_id(id_min, "program_id_min")
    _check_id(id_max, "program_id_max")

    if not isinstance(name, str):
        raise TypeError(f"name must be text, got {type(name).__name__}")
    try:
        raw_name = name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"name must be ASCII, got {name!r}") from exc
    if len(raw_name) > _MAX_NAME_BYTES:
        raise ValueError(
            f"name is {len(raw_name)} bytes long, the field holds at most "
            f"{_MAX_NAME_BYTES} and a terminator: {name!r}"
        )
    if b"\x00" in raw_name:
        raise ValueError(f"name contains a NUL character: {name!r}")

    if isinstance(address_space, bool) or not isinstance(address_space, int):
        raise TypeError(f"address_space must be an integer, got {type(address_space).__name__}")
    if not 0 <= address_space <= _MAX_ADDRESS_SPACE:
        raise ValueError(f"address_space must be 0..{_MAX_ADDRESS_SPACE}, got {address_space}")

    meta = _META.pack(
        NPDM_MAGIC,
        (address_space << 1) | (1 if is_64bit else 0),
        _BUILT_PRIORITY,
        0,  # main thread core number
        0,  # system resource size
        0,  # version
        _BUILT_STACK_SIZE,
        raw_name,
        b"",  # product code
        _BUILT_ACI_OFFSET,
        _ACI_HEADER.size,
        _BUILT_ACID_OFFSET,
        _ACID_MIN_SIZE,
    )
    # No descriptors: each of the three is an empty range at the end of its block.
    acid_header = _ACID_HEADER.pack(
        _ACID_MAGIC,
        _RSA_SIZE + _ACID_HEADER.size,  # the signed area: the key and this header
        _BUILT_ACID_FLAGS,
        id_min,
        id_max,
        *(_ACID_MIN_SIZE, 0) * 3,
    )
    aci = _ACI_HEADER.pack(_ACI_MAGIC, program_id, *(_ACI_HEADER.size, 0) * 3)
    return meta + bytes(2 * _RSA_SIZE) + acid_header + aci


# ------------------------------------------------------------------------ internals


def _read_acid(view: memoryview, offset: int, size: int) -> tuple[int, int] | str:
    """Return ``(ProgramIdMin, ProgramIdMax)`` of the ACID block at ``offset``.

    When the block cannot be read, the return value is the note that says why (a full
    sentence) instead: nothing about this block is fatal.
    """
    if offset == 0 and size == 0:
        return (
            "The file has no ACID block (AcidOffset and AcidSize are zero): the range of "
            "program IDs it would allow is unknown."
        )
    where = f"AcidOffset 0x{offset:X}, AcidSize 0x{size:X}"
    if offset + size > len(view):
        return (
            f"The ACID block is unreadable: it ends at 0x{offset + size:X} ({where}), the "
            f"file is only 0x{len(view):X} bytes long."
        )
    if offset < _META.size:
        return f"The ACID block is unreadable: it starts inside the META header ({where})."
    if size < _ACID_MIN_SIZE:
        return (
            f"The ACID block is unreadable: a signature, a key and a header need at least "
            f"0x{_ACID_MIN_SIZE:X} bytes ({where})."
        )
    tried = []
    for header_offset in _ACID_HEADER_OFFSETS:
        if header_offset + _ACID_HEADER.size > size:
            break  # the block is too small for this layout (and for the larger ones)
        start = offset + header_offset
        tried.append(f"0x{start:X}")
        if bytes(view[start : start + len(_ACID_MAGIC)]) == _ACID_MAGIC:
            (
                _magic,
                _signed_size,
                _flags,
                program_id_min,
                program_id_max,
                *_descriptors,
            ) = _ACID_HEADER.unpack_from(view, start)
            return program_id_min, program_id_max
    return (
        f"The ACID block is unreadable: the magic {_ACID_MAGIC!r} is not at offset "
        f"{' or '.join(tried)} ({where})."
    )


def _check_id(value: int, what: str) -> None:
    """Refuse anything that is not a 64-bit unsigned ID."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer, got {type(value).__name__}")
    if not 0 <= value < _ID_LIMIT:
        raise ValueError(f"{what} out of range for a 64-bit ID: {value}")
