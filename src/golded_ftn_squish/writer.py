"""Offline classic Squish mutation with stable UIDs and byte revisions."""

from __future__ import annotations

import os
import re
import stat
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from types import TracebackType
from typing import Literal, NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

from golded_ftn import (
    UNSET,
    ConflictError,
    ControlLine,
    FtnAddress,
    MessageIdentity,
    MessagePatch,
    OutgoingMessage,
    PhysicalBaseIdentity,
    PreparedMutation,
    ReaderOptions,
    RecoveryError,
    RecoveryOperationKind,
    RevisionToken,
    RollbackError,
    SessionMessage,
    Unset,
    UnsupportedOperationError,
    WriterError,
    WriteResult,
    WriterOptions,
)
from golded_ftn._recovery import BaseRecoverableSession, FileMutationPlan, RecoveryStore
from golded_ftn._writer_io import IO, Transaction, locks, raw_revision, strict_encode

from .reader import _FRAME, _INDEX, SquishReader, _file, _frame, _require, _slice

_SIGNATURE = 0xAFAE4453
_MSGUID = 0x20000
_TZUTC_RE = re.compile(r"^([+-]?)(\d{2})(\d{2})$")
_ADAPTER_MESSAGES = {
    "dst_gap": "Squish local time falls in a DST gap without TZUTC",
    "dst_fold": "Squish local time is ambiguous at a DST fold without TZUTC",
    "aware_datetime": "Squish posted_at must be naive local wall time",
    "missing_timezone": "Squish posted_at requires TZUTC or an area timezone",
    "invalid_tzutc": "Invalid TZUTC value",
    "conflicting_tzutc": "Conflicting TZUTC controls",
    "year_out_of_range": "Squish UTC date must be in 1980..2069",
}


class SquishAdapterError(ValueError):
    """A message cannot be represented in Squish; the tosser should HELD it."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ADAPTER_MESSAGES[code])

    def __str__(self) -> str:
        return _ADAPTER_MESSAGES[self.code]

    def __repr__(self) -> str:
        return f"SquishAdapterError(code={self.code!r})"


@dataclass(frozen=True, slots=True, kw_only=True)
class SquishWriterOptions(WriterOptions):
    timezone: str | None = None
    extension_case: Literal["upper", "lower"] = "upper"

    def __post_init__(self) -> None:
        WriterOptions.__post_init__(self)
        if self.extension_case not in {"upper", "lower"}:
            raise ValueError("extension_case must be 'upper' or 'lower'")
        if self.timezone is None:
            return
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("timezone must be a non-empty IANA name")
        try:
            ZoneInfo(self.timezone)
        except Exception:
            raise ValueError("Unknown area timezone") from None


def _sidecar_suffixes(options: WriterOptions | None) -> tuple[str, ...]:
    suffixes = (".SQD", ".SQI", ".SQL")
    case = (
        options.extension_case if isinstance(options, SquishWriterOptions) else "upper"
    )
    if case == "lower":
        return tuple(suffix.lower() for suffix in suffixes)
    return suffixes


def _configured_timezone(options: WriterOptions) -> str | None:
    if isinstance(options, SquishWriterOptions):
        return options.timezone
    return None


def _parse_tzutc(value: str) -> int:
    match = _TZUTC_RE.fullmatch(value.strip())
    if match is None:
        raise SquishAdapterError("invalid_tzutc")
    sign, hours, minutes = match.groups()
    hour_i, minute_i = int(hours), int(minutes)
    if hour_i > 14 or minute_i > 59:
        raise SquishAdapterError("invalid_tzutc")
    offset = hour_i * 60 + minute_i
    return -offset if sign == "-" else offset


def _tzutc_minutes(controls: tuple[ControlLine, ...]) -> int | None:
    found: int | None = None
    seen = False
    for control in controls:
        if control.name.upper() != "TZUTC":
            continue
        parsed = _parse_tzutc(control.value)
        if seen and parsed != found:
            raise SquishAdapterError("conflicting_tzutc")
        found = parsed
        seen = True
    return found


def _tzutc_for_serialize(
    message: OutgoingMessage | MessagePatch,
    original_controls: bytes | None,
) -> int | None:
    if not isinstance(message.control_lines, Unset):
        if message.control_lines is None:
            return None
        return _tzutc_minutes(message.control_lines)
    if original_controls is not None:
        return _tzutc_minutes(_controls(original_controls))
    if isinstance(message, OutgoingMessage):
        return _tzutc_minutes(message.control_lines)
    return None


def _localize_wall_time(value: datetime, iana: str) -> datetime:
    naive = value.replace(tzinfo=None)
    try:
        tz = ZoneInfo(iana)
    except Exception:
        raise ValueError("Unknown area timezone") from None
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    wall_first = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    wall_second = second.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if wall_first != naive and wall_second != naive:
        raise SquishAdapterError("dst_gap")
    if first.utcoffset() != second.utcoffset():
        raise SquishAdapterError("dst_fold")
    return first.astimezone(UTC)


def _adapt_posted_at(
    value: datetime,
    *,
    tzutc_minutes: int | None,
    area_timezone: str | None,
) -> datetime:
    if value.tzinfo is not None:
        raise SquishAdapterError("aware_datetime")
    if tzutc_minutes is not None:
        offset = timezone(timedelta(minutes=tzutc_minutes))
        return value.replace(tzinfo=offset).astimezone(UTC)
    if area_timezone is not None:
        return _localize_wall_time(value, area_timezone)
    raise SquishAdapterError("missing_timezone")


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _sqd_write_ranges(old: bytes, new: bytes) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = [(0, min(256, len(new)))]
    if len(new) > len(old):
        ranges.append((len(old), len(new)))
    for offset in range(256, len(old), 256):
        end = min(offset + 256, len(old), len(new))
        if end <= offset:
            continue
        if new[offset:end] != old[offset:end]:
            ranges.append((offset, end))
    return _merge_ranges(ranges)


def _sqi_write_ranges(old: bytes, new: bytes) -> tuple[tuple[int, int], ...]:
    end = max(len(old), len(new))
    if end == 0:
        return ()
    return ((0, end),)


def _write_sqd_sqi(
    io: IO,
    sqd_fd: int,
    sqi_fd: int,
    old: bytes,
    data: bytearray,
    index: bytes,
) -> None:
    if len(data) > len(old):
        io.write(sqd_fd, len(old), bytes(data[len(old) :]))
    for offset in range(256, len(old), 256):
        chunk = bytes(data[offset : min(offset + 256, len(old))])
        if chunk != old[offset : offset + len(chunk)]:
            io.write(sqd_fd, offset, chunk)
    io.write(sqi_fd, 0, index)
    io.truncate(sqi_fd, len(index))
    io.write(sqd_fd, 0, bytes(data[:256]))


def _closed_recovery_error(code: str) -> NoReturn:
    raise RecoveryError(code=code) from None


def _locate_recovery_file(base: Path, suffix: str) -> Path:
    try:
        candidates = [
            item
            for item in base.parent.iterdir()
            if item.stem == base.name and item.suffix.upper() == suffix
        ]
    except OSError:
        _closed_recovery_error("unsafe_recovery_path")
    if len(candidates) != 1:
        raise RecoveryError(code="unsafe_recovery_path")
    return candidates[0]


def _verify_retained_path(path: Path, fd: int) -> None:
    try:
        path_stat = os.lstat(path)
        fd_stat = os.fstat(fd)
    except OSError:
        _closed_recovery_error("unsafe_recovery_path")
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryError(code="unsafe_recovery_path")
    if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
        raise RecoveryError(code="unsafe_recovery_path")


def _open_recovery_file(path: Path) -> int:
    flags = (
        os.O_RDWR
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NOCTTY", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError:
        _closed_recovery_error("unsafe_recovery_path")
    try:
        _verify_retained_path(path, fd)
    except RecoveryError:
        os.close(fd)
        raise
    return fd


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        _closed_recovery_error("unexpected_base_modification")


def _identity_for(base: Path, uid: int) -> MessageIdentity:
    return MessageIdentity(format="squish", base=str(base), msgno=uid)


def _session_message(
    data: bytes | bytearray,
    record: tuple[int, int, int],
    base: Path,
    sqd: Path,
    options: WriterOptions,
) -> SessionMessage:
    offset, uid, _hash_value = record
    _signature, _next, _prev, _length, total, controls, _kind, reserved = (
        _FRAME.unpack_from(data, offset)
    )
    identity = _identity_for(base, uid)
    raw = bytes(data[offset + 28 : offset + 28 + total])
    revision = raw_revision(
        identity,
        (offset,),
        struct.pack("<IIIHH", _length, total, controls, _kind, reserved),
        raw,
    )
    return SessionMessage(
        message=SquishReader._message(
            bytes(data),
            sqd,
            offset,
            uid,
            ReaderOptions(fallback_charset=options.target_charset),
        ),
        identity=identity,
        revision=revision,
    )


def _uint(value: int | None, bits: int, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must fit unsigned {bits} bits")
    return value


def _hash(raw: bytes) -> int:
    result = 0
    for byte in raw.split(b"\0", 1)[0]:
        byte = byte + 32 if 65 <= byte <= 90 else byte
        result = ((result << 4) + byte) & 0xFFFFFFFF
        high = result & 0xF0000000
        if high:
            result |= high >> 24
            result |= high
    return result & 0x7FFFFFFF


def _validate(
    data: bytes,
    index: bytes,
    sqd: Path,
    sqi: Path,
    options: ReaderOptions | None = None,
) -> list[tuple[int, int, int]]:
    _slice(data, 0, 256, sqd)
    _require(struct.unpack_from("<H", data)[0] == 256, sqd, 0, "Unsupported base size")
    _require(
        struct.unpack_from("<H", data, 130)[0] == 28, sqd, 130, "Unsupported frame size"
    )
    count, highest = struct.unpack_from("<II", data, 4)
    _require(count == highest, sqd, 8, "Highest message count disagrees")
    _require(
        len(index) % 12 == 0 and len(index) >= count * 12, sqi, 0, "Invalid index size"
    )
    end = struct.unpack_from("<i", data, 120)[0]
    _require(end == len(data), sqd, 120, "End frame does not match file size")
    physical: dict[int, tuple[int, ...]] = {}
    offset = 256
    while offset < end:
        values = _FRAME.unpack(_slice(data, offset, 28, sqd))
        signature, _next, _prev, length, total, controls, kind, _reserved = values
        _require(
            signature == _SIGNATURE and kind in {0, 1}, sqd, offset, "Invalid frame"
        )
        _slice(data, offset + 28, length, sqd)
        _require(
            total <= length and controls <= total, sqd, offset, "Invalid frame lengths"
        )
        physical[offset] = values
        offset += 28 + length
    _require(offset == end, sqd, offset, "Invalid physical frame boundary")
    records: list[tuple[int, int, int]] = []
    previous_uid = 0
    active: list[int] = []
    for position in range(count):
        record_offset, uid, recipient_hash = _INDEX.unpack_from(index, position * 12)
        _require(
            uid > previous_uid, sqi, position * 12, "UIDs not positive and increasing"
        )
        previous_uid = uid
        _require(
            record_offset in physical, sqi, position * 12, "Index points inside a frame"
        )
        _frame(data, sqd, sqi, position, record_offset)
        SquishReader._message(data, sqd, record_offset, uid, options or ReaderOptions())
        records.append((record_offset, uid, recipient_hash))
        active.append(record_offset)
    next_uid = struct.unpack_from("<I", data, 20)[0]
    _require(next_uid > previous_uid, sqd, 20, "Next UID does not exceed existing UIDs")
    seen: set[int] = set()
    for kind, first_offset, last_offset in ((0, 104, 108), (1, 112, 116)):
        first, last = struct.unpack_from("<ii", data, first_offset)
        chain: list[int] = []
        previous = 0
        current = first
        while current:
            _require(
                current in physical and current not in seen,
                sqd,
                current,
                "Broken or cyclic frame chain",
            )
            seen.add(current)
            frame = physical[current]
            _require(
                frame[2] == previous and frame[6] == kind,
                sqd,
                current,
                "Frame chain links disagree",
            )
            chain.append(current)
            previous, current = current, frame[1]
        _require(previous == last, sqd, last_offset, "Last frame disagrees with chain")
        if kind == 0:
            _require(
                chain == active, sqd, first_offset, "Active chain disagrees with index"
            )
    _require(seen == set(physical), sqd, 104, "Unlinked physical frame")
    return records


def _controls(raw: bytes) -> tuple[ControlLine, ...]:
    return tuple(
        ControlLine(name=match[1], value=match[2], raw=match[0])
        for match in re.finditer(
            r"\x01([A-Za-z][A-Za-z0-9-]*):\s*([^\x01\r\n\x00]*)", raw.decode("latin1")
        )
    )


def _encode(text: str, options: WriterOptions, declarations: bytes = b"") -> bytes:
    if "\0" in text:
        raise ValueError("Embedded NUL is not representable")
    declarations += text.encode("utf-8")
    return strict_encode(text, options, _controls(declarations))


def _control_bytes(controls: tuple[ControlLine, ...], options: WriterOptions) -> bytes:
    result = bytearray()
    for control in controls:
        name, value = control.name, control.value
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
            raise ValueError("Invalid control name")
        if any(character in value for character in "\x00\x01\r\n"):
            raise ValueError("Invalid control value")
        result += strict_encode(f"\x01{name}: {value}", options, controls)
    return bytes(result) + b"\0"


def _date(
    header: bytearray,
    value: datetime | None,
    *,
    tzutc_minutes: int | None = None,
    area_timezone: str | None = None,
) -> None:
    if value is None:
        header[164:168] = b"\0" * 4
        header[218:238] = b"\0" * 20
        return
    date = _adapt_posted_at(
        value, tzutc_minutes=tzutc_minutes, area_timezone=area_timezone
    )
    if not 1980 <= date.year <= 2069:
        raise SquishAdapterError("year_out_of_range")
    packed = (
        date.day
        | date.month << 5
        | (date.year - 1980) << 9
        | date.second // 2 << 16
        | date.minute << 21
        | date.hour << 27
    )
    struct.pack_into("<I", header, 164, packed)
    # Month spelling is fixed, independent of the process locale.
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    text = (
        f"{date.day:02d} {months[date.month - 1]} "
        f"{date.year % 100:02d}  {date:%H:%M:%S}"
    )
    header[218:238] = text.encode("ascii").ljust(20, b"\0")


def _address(header: bytearray, offset: int, value: FtnAddress | None) -> None:
    if value is not None and value.domain is not None:
        raise ValueError("Squish fixed addresses cannot represent a domain")
    values = (
        (0, 0, 0, 0)
        if value is None
        else (
            0 if value.zone is None else value.zone,
            value.net,
            value.node,
            value.point or 0,
        )
    )
    struct.pack_into(
        "<4H", header, offset, *(_uint(item, 16, "address") for item in values)
    )


def _serialize(
    message: OutgoingMessage | MessagePatch,
    uid: int,
    options: WriterOptions,
    original: tuple[bytes, bytes, bytes] | None = None,
) -> tuple[bytes, bytes, bytes]:
    header, controls, body = original or (bytes(238), b"\0", b"\0")
    fixed = bytearray(header)
    is_new = original is None
    for name, offset, size in (
        ("from_name", 4, 36),
        ("to_name", 40, 36),
        ("subject", 76, 72),
    ):
        value = getattr(message, name)
        if value is not UNSET:
            if value is None:
                raise ValueError(f"{name} cannot be cleared")
            raw = _encode(value, options, controls + body)
            if len(raw) >= size:
                raise ValueError(f"{name} exceeds {size - 1} encoded bytes")
            fixed[offset : offset + size] = raw.ljust(size, b"\0")
    if not isinstance(message.attributes_raw, Unset):
        attributes = _uint(message.attributes_raw, 32, "attributes_raw")
        struct.pack_into("<I", fixed, 0, attributes | (_MSGUID if is_new else 0))
    if is_new:
        struct.pack_into("<I", fixed, 214, uid)
    for name, offset in (("from_address", 148), ("to_address", 156)):
        value = getattr(message, name)
        if value is not UNSET:
            _address(fixed, offset, value)
    if not isinstance(message.posted_at, Unset):
        _date(
            fixed,
            message.posted_at,
            tzutc_minutes=_tzutc_for_serialize(
                message, original[1] if original is not None else None
            ),
            area_timezone=_configured_timezone(options),
        )
    if not isinstance(message.reply_to_msgno, Unset):
        struct.pack_into(
            "<I", fixed, 174, _uint(message.reply_to_msgno, 32, "reply_to_msgno")
        )
    if (
        not isinstance(message.reply_next_msgno, Unset)
        and message.reply_next_msgno is not None
    ):
        raise ValueError("Squish uses a reply list, not reply_next_msgno")
    if not isinstance(message.reply_list, Unset):
        replies = message.reply_list or ()
        if len(replies) > 9:
            raise ValueError("Squish supports at most nine reply UIDs")
        struct.pack_into(
            "<9I",
            fixed,
            178,
            *(_uint(item, 32, "reply UID") for item in replies),
            *((0,) * (9 - len(replies))),
        )
    if not isinstance(message.reply1st_msgno, Unset):
        first = _uint(message.reply1st_msgno, 32, "reply1st_msgno")
        if (
            not isinstance(message.reply_list, Unset)
            and (not is_new or first)
            and first != (message.reply_list[0] if message.reply_list else 0)
        ):
            raise ValueError("reply1st_msgno conflicts with reply_list")
        if not is_new or first:
            struct.pack_into("<I", fixed, 178, first)
    if not isinstance(message.control_lines, Unset):
        replacement_controls = _control_bytes(message.control_lines or (), options)
        if original is not None and message.external_id is UNSET:
            existing_ids = {
                control.value.encode("latin1").strip()
                for control in _controls(controls + body)
                if control.name.upper() == "MSGID"
            }
            replacement_ids = {
                control.value.encode("latin1").strip()
                for control in _controls(replacement_controls)
                if control.name.upper() == "MSGID"
            }
            if replacement_ids and replacement_ids != existing_ids:
                raise ValueError("MSGID control conflicts with omitted external_id")
            old_ids = [
                chunk
                for chunk in controls.rstrip(b"\0").split(b"\x01")
                if re.match(rb"MSGID(?::|\s)", chunk, re.IGNORECASE)
            ]
            replacement_controls = b"\x01".join(
                chunk
                for chunk in replacement_controls.rstrip(b"\0").split(b"\x01")
                if not re.match(rb"MSGID(?::|\s)", chunk, re.IGNORECASE)
            )
            for chunk in old_ids:
                replacement_controls += b"\x01" + chunk
            replacement_controls += b"\0"
        controls = replacement_controls
    if not isinstance(message.external_id, Unset) and (
        not is_new or message.external_id is not None
    ):
        controls = b"\x01".join(
            item
            for item in controls.rstrip(b"\0").split(b"\x01")
            if not re.match(rb"MSGID(?::|\s)", item, re.IGNORECASE)
        )
        if message.external_id is not None:
            value = message.external_id
            if any(character in value for character in "\x00\x01\r\n"):
                raise ValueError("Invalid external_id")
            controls += _encode(f"\x01MSGID: {value}", options, controls + body)
        controls = controls.rstrip(b"\0") + b"\0"
        if not is_new:
            body = (
                re.sub(
                    rb"(?:^|(?<=[\r\n]))\x01MSGID(?::|\s)[^\r\n\x00]*(?:\r?\n|\r)?",
                    b"",
                    body.rstrip(b"\0"),
                    flags=re.IGNORECASE,
                )
                + b"\0"
            )
    if not isinstance(message.body_text, Unset):
        if message.body_text is None:
            raise ValueError("body_text cannot be cleared")
        original_body = body
        body = (
            _encode(
                message.body_text.replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\n", "\r"),
                options,
                controls,
            )
            + b"\0"
        )
        if original is not None:
            preserved: list[bytes] = []
            for name, pattern in (
                ("routing_seen_by", rb"SEEN-BY:"),
                ("routing_path", rb"\x01PATH:"),
            ):
                if getattr(message, name) is UNSET:
                    preserved.extend(
                        line
                        for line in original_body.rstrip(b"\0").splitlines()
                        if re.match(pattern, line, re.IGNORECASE)
                    )
                    body = (
                        re.sub(
                            rb"(?:^|(?<=[\r\n]))"
                            + pattern
                            + rb"[^\r\n\x00]*(?:\r?\n|\r)?",
                            b"",
                            body.rstrip(b"\0"),
                            flags=re.IGNORECASE,
                        )
                        + b"\0"
                    )
            if preserved:
                body = body.rstrip(b"\0\r\n") + b"\r" + b"\r".join(preserved) + b"\0"
    for name, prefix in (
        ("routing_seen_by", "SEEN-BY: "),
        ("routing_path", "\x01PATH: "),
    ):
        value = getattr(message, name)
        if value is not UNSET and (not is_new or value):
            pattern = (
                rb"(?m)(?:^|(?<=[\r\n]))"
                + (rb"SEEN-BY:" if name == "routing_seen_by" else rb"\x01PATH:")
                + rb"[^\r\n\x00]*(?:\r?\n|\r)?"
            )
            body = re.sub(pattern, b"", body.rstrip(b"\0"), flags=re.IGNORECASE)
            if value:
                if any(any(char in item for char in "\x00\x01\r\n") for item in value):
                    raise ValueError("Invalid routing string")
                suffix = "\r".join(prefix + item for item in value)
                body = (
                    body.rstrip(b"\r\n")
                    + b"\r"
                    + _encode(suffix, options, controls + body)
                )
            body += b"\0"
    return bytes(fixed), controls, body


class SquishWriter:
    def __init__(self, *, io: IO | None = None) -> None:
        self.io = io or IO()

    def create(
        self, path: str | PathLike[str], options: WriterOptions | None = None
    ) -> None:
        base = Path(path)
        names = [Path(str(base) + suffix) for suffix in _sidecar_suffixes(options)]
        for candidate in base.parent.iterdir():
            if candidate.stem == base.name and candidate.suffix.upper() in {
                ".SQD",
                ".SQI",
                ".SQL",
            }:
                raise FileExistsError(candidate)
        header = bytearray(256)
        struct.pack_into("<H", header, 0, 256)
        struct.pack_into("<I", header, 20, 2)
        encoded_name = os.fsencode(base.name)
        if len(encoded_name) >= 80:
            raise ValueError("Squish base name exceeds 79 filesystem bytes")
        header[24:104] = encoded_name.ljust(80, b"\0")
        struct.pack_into("<i", header, 120, 256)
        struct.pack_into("<H", header, 130, 28)
        created: list[Path] = []
        try:
            for target, content in zip(names, (bytes(header), b"", b""), strict=True):
                fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                created.append(target)
                try:
                    self.io.write(fd, 0, content)
                    self.io.flush(fd)
                finally:
                    os.close(fd)
        except BaseException:
            for target in reversed(created):
                target.unlink()
            raise

    def open(
        self, path: str | PathLike[str], options: WriterOptions | None = None
    ) -> SquishSession:
        return SquishSession(Path(path), options or WriterOptions(), self.io)

    @contextmanager
    def recovery_session(
        self,
        path: str | PathLike[str],
        *,
        recovery_root: str | PathLike[str],
        options: WriterOptions | None = None,
    ) -> Iterator[SquishRecoverableSession]:
        opts = options or WriterOptions()
        base = Path(path).resolve()
        sqd = _locate_recovery_file(base, ".SQD")
        sqi = _locate_recovery_file(base, ".SQI")
        with locks.acquire(sqd, timeout=opts.lock_timeout) as sqd_fd:
            _verify_retained_path(sqd, sqd_fd)
            sqi_fd = _open_recovery_file(sqi)
            try:
                lock_stat = os.fstat(sqd_fd)
                store = RecoveryStore(
                    physical_base=PhysicalBaseIdentity(
                        format="squish",
                        canonical_key=os.path.realpath(sqd),
                        lock_inode=(lock_stat.st_dev, lock_stat.st_ino),
                    ),
                    recovery_root=recovery_root,
                    base_root=os.path.realpath(sqd.parent),
                    retained_fds={
                        ("base", sqd.name): sqd_fd,
                        ("base", sqi.name): sqi_fd,
                    },
                    io=self.io,
                )
                session = SquishRecoverableSession(
                    store=store,
                    base=base,
                    sqd=sqd,
                    sqi=sqi,
                    sqd_fd=sqd_fd,
                    sqi_fd=sqi_fd,
                    options=opts,
                    io=self.io,
                )
                try:
                    yield session
                finally:
                    session.close()
            finally:
                os.close(sqi_fd)


class SquishSession:
    def __init__(self, base: Path, options: WriterOptions, io: IO) -> None:
        self.base = base.resolve()
        self.options, self.io = options, io
        self.sqd, self.sqi = (_file(self.base, suffix) for suffix in (".SQD", ".SQI"))
        self._index_fd = os.open(self.sqi, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._closed = False
        self._poisoned = False

    def __enter__(self) -> SquishSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.close()
        return False

    def close(self) -> None:
        if not self._closed:
            os.close(self._index_fd)
            self._closed = True

    @contextmanager
    def _operation(self) -> Iterator[tuple[int, bytearray, list[tuple[int, int, int]]]]:
        if self._closed or self._poisoned:
            raise WriterError(
                "Squish session is closed or unusable after failed rollback"
            )
        with locks.acquire(self.sqd, timeout=self.options.lock_timeout) as fd:
            data = self.io.read(fd, 0, os.fstat(fd).st_size)
            index = self.io.read(self._index_fd, 0, os.fstat(self._index_fd).st_size)
            records = _validate(
                data,
                index,
                self.sqd,
                self.sqi,
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            try:
                yield fd, bytearray(data), records
            except RollbackError:
                self._poisoned = True
                raise

    def _identity(self, uid: int) -> MessageIdentity:
        return _identity_for(self.base, uid)

    def _read(
        self, data: bytes | bytearray, record: tuple[int, int, int]
    ) -> SessionMessage:
        return _session_message(data, record, self.base, self.sqd, self.options)

    def read(self, msgno: int) -> SessionMessage:
        with self._operation() as (_fd, data, records):
            for record in records:
                if record[1] == msgno:
                    return self._read(data, record)
            raise ConflictError("Squish message does not exist")

    def _target(
        self,
        data: bytearray,
        records: list[tuple[int, int, int]],
        identity: MessageIdentity,
        revision: RevisionToken,
    ) -> int:
        if identity != self._identity(identity.msgno) or revision.identity != identity:
            raise ConflictError("Squish identity belongs to another base")
        for position, record in enumerate(records):
            if record[1] == identity.msgno:
                if self._read(data, record).revision != revision:
                    raise ConflictError("Squish message changed")
                return position
        raise ConflictError("Squish message disappeared")

    def _commit(
        self,
        fd: int,
        old: bytes,
        data: bytearray,
        records: list[tuple[int, int, int]],
        operation: str,
    ) -> None:
        index = b"".join(_INDEX.pack(*record) for record in records)
        _validate(
            bytes(data),
            index,
            self.sqd,
            self.sqi,
            ReaderOptions(fallback_charset=self.options.target_charset),
        )
        with Transaction(self.io, str(self.base), operation) as transaction:
            transaction.watch(fd)
            transaction.watch(self._index_fd)
            # Publish payload before chain/index/base changes, all while locked.
            _write_sqd_sqi(self.io, fd, self._index_fd, old, data, index)

    @staticmethod
    def _chain(data: bytearray, offsets: list[int], kind: int) -> None:
        struct.pack_into(
            "<ii",
            data,
            104 if kind == 0 else 112,
            offsets[0] if offsets else 0,
            offsets[-1] if offsets else 0,
        )
        for position, offset in enumerate(offsets):
            struct.pack_into(
                "<ii",
                data,
                offset + 4,
                offsets[position + 1] if position + 1 < len(offsets) else 0,
                offsets[position - 1] if position else 0,
            )
            struct.pack_into("<H", data, offset + 24, kind)

    @staticmethod
    def _free(data: bytearray) -> list[int]:
        result: list[int] = []
        current = struct.unpack_from("<i", data, 112)[0]
        while current:
            result.append(current)
            current = struct.unpack_from("<i", data, current + 4)[0]
        return result

    def append(self, message: OutgoingMessage) -> WriteResult:
        # Validate model before touching the base.
        _serialize(message, 1, self.options)
        with self._operation() as (fd, data, records):
            old = bytes(data)
            uid = struct.unpack_from("<I", data, 20)[0]
            if uid >= 0xFFFFFFFF:
                raise ValueError("Squish UID space exhausted")
            maximum = struct.unpack_from("<I", data, 124)[0]
            if maximum and len(records) >= maximum:
                raise UnsupportedOperationError(
                    "Squish maxmsgs reached; automatic purge is outside writer scope"
                )
            fixed, controls, body = _serialize(message, uid, self.options)
            offset = self._append_frame(data, fixed, controls, body)
            records.append(
                (
                    offset,
                    uid,
                    _hash(fixed[40:76])
                    | (0x80000000 if struct.unpack_from("<I", fixed)[0] & 4 else 0),
                )
            )
            self._chain(data, [record[0] for record in records], 0)
            struct.pack_into("<II", data, 4, len(records), len(records))
            struct.pack_into("<I", data, 20, uid + 1)
            self._commit(fd, old, data, records, "append")
            result = self._read(data, records[-1])
            return WriteResult(identity=result.identity, revision=result.revision)

    @staticmethod
    def _append_frame(
        data: bytearray, fixed: bytes, controls: bytes, body: bytes
    ) -> int:
        offset = len(data)
        payload = fixed + controls + body
        if offset + 28 + len(payload) > 0x7FFFFFFF:
            raise ValueError("Squish file exceeds signed 32-bit offsets")
        data += (
            _FRAME.pack(
                _SIGNATURE, 0, 0, len(payload), len(payload), len(controls), 0, 0
            )
            + payload
        )
        struct.pack_into("<i", data, 120, len(data))
        return offset

    def update(
        self,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> WriteResult:
        with self._operation() as (fd, data, records):
            position = self._target(data, records, identity, expected_revision)
            old = bytes(data)
            offset, uid, _old_hash = records[position]
            values = _FRAME.unpack_from(data, offset)
            start, control_length, total = offset + 28, values[5], values[4]
            raw = bytes(data[start : start + total])
            fixed, controls, body = _serialize(
                patch,
                uid,
                self.options,
                (
                    raw[:238],
                    raw[238 : 238 + control_length],
                    raw[238 + control_length :],
                ),
            )
            content_changed = any(
                getattr(patch, field.name) is not UNSET
                for field in fields(patch)
                if field.name
                in {
                    "from_name",
                    "to_name",
                    "subject",
                    "body_text",
                    "control_lines",
                    "external_id",
                    "routing_seen_by",
                    "routing_path",
                }
            )
            if content_changed:
                free = self._free(data)
                new_offset = self._append_frame(data, fixed, controls, body)
                records[position] = (new_offset, uid, 0)
                self._chain(data, free + [offset], 1)
                self._chain(data, [record[0] for record in records], 0)
            else:
                data[start : start + 238] = fixed
                new_offset = offset
            recipient_hash = _hash(fixed[40:76]) | (
                0x80000000 if struct.unpack_from("<I", fixed)[0] & 4 else 0
            )
            records[position] = (new_offset, uid, recipient_hash)
            self._commit(fd, old, data, records, "update")
            result = self._read(data, records[position])
            return WriteResult(identity=result.identity, revision=result.revision)

    def delete(
        self, identity: MessageIdentity, expected_revision: RevisionToken
    ) -> MessageIdentity:
        with self._operation() as (fd, data, records):
            position = self._target(data, records, identity, expected_revision)
            old = bytes(data)
            free = self._free(data)
            offset, _uid, _hash_value = records.pop(position)
            self._chain(data, free + [offset], 1)
            self._chain(data, [record[0] for record in records], 0)
            struct.pack_into("<II", data, 4, len(records), len(records))
            self._commit(fd, old, data, records, "delete")
            return identity


def _recipient_hash(fixed: bytes) -> int:
    return _hash(fixed[40:76]) | (
        0x80000000 if struct.unpack_from("<I", fixed)[0] & 4 else 0
    )


def _content_fields_changed(patch: MessagePatch) -> bool:
    return any(
        getattr(patch, field.name) is not UNSET
        for field in fields(patch)
        if field.name
        in {
            "from_name",
            "to_name",
            "subject",
            "body_text",
            "control_lines",
            "external_id",
            "routing_seen_by",
            "routing_path",
        }
    )


class SquishRecoverableSession(BaseRecoverableSession):
    def __init__(
        self,
        *,
        store: RecoveryStore,
        base: Path,
        sqd: Path,
        sqi: Path,
        sqd_fd: int,
        sqi_fd: int,
        options: WriterOptions,
        io: IO,
    ) -> None:
        super().__init__(store)
        self.base = base
        self.sqd = sqd
        self.sqi = sqi
        self._sqd_fd = sqd_fd
        self._sqi_fd = sqi_fd
        self.options = options
        self.io = io

    def _reader_options(self) -> ReaderOptions:
        return ReaderOptions(fallback_charset=self.options.target_charset)

    def _require_prepare_ready(self) -> None:
        self.check_usable()
        self.store.require_binding()
        if self._active_mutation is not None:
            self.poison()
            raise RecoveryError(code="invalid_prepared_mutation")
        if self.store.pending():
            raise RecoveryError(code="invalid_prepared_mutation")

    def _load(self) -> tuple[bytearray, bytes, list[tuple[int, int, int]]]:
        data = bytearray(self.io.read(self._sqd_fd, 0, os.fstat(self._sqd_fd).st_size))
        index = self.io.read(self._sqi_fd, 0, os.fstat(self._sqi_fd).st_size)
        records = _validate(
            bytes(data), index, self.sqd, self.sqi, self._reader_options()
        )
        return data, index, records

    def _plans(
        self, old_sqd: bytes, new_sqd: bytes, old_sqi: bytes, new_sqi: bytes
    ) -> tuple[FileMutationPlan, FileMutationPlan]:
        return (
            FileMutationPlan(
                role="sqd",
                rel_path=self.sqd.name,
                fd=self._sqd_fd,
                planned_write_ranges=_sqd_write_ranges(old_sqd, new_sqd),
                post_min_len=min(len(old_sqd), len(new_sqd)),
                post_max_len=max(len(old_sqd), len(new_sqd)),
            ),
            FileMutationPlan(
                role="sqi",
                rel_path=self.sqi.name,
                fd=self._sqi_fd,
                planned_write_ranges=_sqi_write_ranges(old_sqi, new_sqi),
                post_min_len=min(len(old_sqi), len(new_sqi)),
                post_max_len=max(len(old_sqi), len(new_sqi)),
            ),
        )

    def _publish(
        self, old: bytes, data: bytearray, records: list[tuple[int, int, int]]
    ) -> None:
        index = b"".join(_INDEX.pack(*record) for record in records)
        _validate(bytes(data), index, self.sqd, self.sqi, self._reader_options())
        _write_sqd_sqi(self.io, self._sqd_fd, self._sqi_fd, old, data, index)
        self.io.flush(self._sqd_fd)
        self.io.flush(self._sqi_fd)
        _fsync_directory(self.sqd.parent)

    def read(self, msgno: int) -> SessionMessage:
        self.check_usable()
        data, _index, records = self._load()
        for record in records:
            if record[1] == msgno:
                return _session_message(data, record, self.base, self.sqd, self.options)
        raise ConflictError("Squish message does not exist")

    def prepare_append(
        self, transaction_id: UUID, message: OutgoingMessage
    ) -> PreparedMutation:
        self._require_prepare_ready()
        _serialize(message, 1, self.options)
        data, old_index, records = self._load()
        old = bytes(data)
        uid = struct.unpack_from("<I", data, 20)[0]
        if uid >= 0xFFFFFFFF:
            raise ValueError("Squish UID space exhausted")
        maximum = struct.unpack_from("<I", data, 124)[0]
        if maximum and len(records) >= maximum:
            raise UnsupportedOperationError(
                "Squish maxmsgs reached; automatic purge is outside writer scope"
            )
        fixed, controls, body = _serialize(message, uid, self.options)
        offset = SquishSession._append_frame(data, fixed, controls, body)
        records.append((offset, uid, _recipient_hash(fixed)))
        SquishSession._chain(data, [record[0] for record in records], 0)
        struct.pack_into("<II", data, 4, len(records), len(records))
        struct.pack_into("<I", data, 20, uid + 1)
        new_index = b"".join(_INDEX.pack(*record) for record in records)
        _validate(bytes(data), new_index, self.sqd, self.sqi, self._reader_options())
        result_record = records[-1]

        def apply_fn() -> WriteResult:
            self._publish(old, data, records)
            result = _session_message(
                data, result_record, self.base, self.sqd, self.options
            )
            return WriteResult(identity=result.identity, revision=result.revision)

        return self.register_prepared_mutation(
            transaction_id,
            RecoveryOperationKind.APPEND,
            self._plans(old, bytes(data), old_index, new_index),
            apply_fn,
        )

    def prepare_update(
        self,
        transaction_id: UUID,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> PreparedMutation:
        self._require_prepare_ready()
        data, old_index, records = self._load()
        if identity != _identity_for(self.base, identity.msgno) or (
            expected_revision.identity != identity
        ):
            raise ConflictError("Squish identity belongs to another base")
        position = None
        for index, record in enumerate(records):
            if record[1] == identity.msgno:
                current = _session_message(
                    data, record, self.base, self.sqd, self.options
                )
                if current.revision != expected_revision:
                    raise ConflictError("Squish message changed")
                position = index
                break
        if position is None:
            raise ConflictError("Squish message disappeared")
        old = bytes(data)
        offset, uid, _old_hash = records[position]
        values = _FRAME.unpack_from(data, offset)
        start, control_length, total = offset + 28, values[5], values[4]
        raw = bytes(data[start : start + total])
        fixed, controls, body = _serialize(
            patch,
            uid,
            self.options,
            (
                raw[:238],
                raw[238 : 238 + control_length],
                raw[238 + control_length :],
            ),
        )
        if _content_fields_changed(patch):
            free = SquishSession._free(data)
            new_offset = SquishSession._append_frame(data, fixed, controls, body)
            records[position] = (new_offset, uid, 0)
            SquishSession._chain(data, free + [offset], 1)
            SquishSession._chain(data, [record[0] for record in records], 0)
        else:
            data[start : start + 238] = fixed
            new_offset = offset
        records[position] = (new_offset, uid, _recipient_hash(fixed))
        new_index = b"".join(_INDEX.pack(*record) for record in records)
        _validate(bytes(data), new_index, self.sqd, self.sqi, self._reader_options())
        result_record = records[position]

        def apply_fn() -> WriteResult:
            self._publish(old, data, records)
            result = _session_message(
                data, result_record, self.base, self.sqd, self.options
            )
            return WriteResult(identity=result.identity, revision=result.revision)

        return self.register_prepared_mutation(
            transaction_id,
            RecoveryOperationKind.UPDATE,
            self._plans(old, bytes(data), old_index, new_index),
            apply_fn,
        )
