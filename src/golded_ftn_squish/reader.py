"""Read indexed classic Squish messages from stable, quiescent areas."""

import codecs
import errno
import re
import stat
import struct
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import NoReturn

from golded_ftn import (
    ControlLine,
    MessageControlLines,
    MessageProvenance,
    ParsedMessage,
    ParserException,
    ReaderOptions,
    detect_charset,
    parse_body,
    parse_message,
    read_null_padded_field,
    synthetic_id,
    to_utf8,
)

from ._metadata import AddressError, parse_date, resolve_addresses

_INDEX = struct.Struct("<iII")
_FRAME = struct.Struct("<IiiIIIHH")
_MSGUID = 0x20000


def _fail(path: Path, offset: int, error: Exception) -> NoReturn:
    raise ParserException(
        f"Cannot parse Squish file {path} at offset {offset}: {error}"
    ) from error


def _require(condition: bool, path: Path, offset: int, message: str) -> None:
    if not condition:
        _fail(path, offset, ValueError(message))


def _slice(data: bytes, offset: int, length: int, path: Path) -> bytes:
    _require(
        0 <= offset <= len(data) and 0 <= length <= len(data) - offset,
        path,
        offset,
        f"Range of {length} bytes outside file of {len(data)} bytes",
    )
    return data[offset : offset + length]


def _file(base: Path, suffix: str) -> Path:
    candidates = [
        p
        for p in base.parent.iterdir()
        if p.stem == base.name and p.suffix.upper() == suffix
    ]
    if not candidates:
        raise FileNotFoundError(errno.ENOENT, "Missing Squish file", str(base) + suffix)
    _require(
        len(candidates) == 1,
        candidates[0],
        0,
        f"Ambiguous {suffix} files: {candidates}",
    )
    path = candidates[0]
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError(errno.EINVAL, "Squish source must be a regular file", str(path))
    return path


def _control_records(raw: bytes, path: Path, offset: int) -> list[tuple[bytes, int]]:
    first_null = raw.find(b"\x00")
    if first_null >= 0:
        _require(
            not raw[first_null:].strip(b"\x00"),
            path,
            offset + first_null,
            "Non-padding data after control terminator",
        )
        raw = raw[:first_null]
    if not raw:
        return []
    _require(raw.startswith(b"\x01"), path, offset, "Control block must begin with SOH")
    result: list[tuple[bytes, int]] = []
    position = 0
    for value in raw.split(b"\x01")[1:]:
        _require(bool(value), path, offset + position, "Empty control record")
        _require(
            b"\r" not in value and b"\n" not in value,
            path,
            offset + position,
            "Line break inside control record",
        )
        result.append((b"\x01" + value, offset + position))
        position += len(value) + 1
    return result


def _charset(
    records: list[tuple[bytes, int]],
    body: bytes,
    path: Path,
    body_offset: int,
    options: ReaderOptions,
) -> str:
    try:
        fallback = detect_charset(b"", options.fallback_charset)
    except (ValueError, LookupError) as error:
        _fail(path, 0, error)
    selected: str | None = None
    identity: str | bytes | None = None
    for raw, offset in [*records, (body, body_offset)]:
        for match in re.finditer(
            rb"\x01(?:CHRS|CHARSET):\s*([^\s\x00\x01]+)", raw, re.IGNORECASE
        ):
            declaration = match[0]
            charset = detect_charset(declaration, options.fallback_charset)
            cp = codecs.lookup(detect_charset(declaration, "CP850")).name
            utf = codecs.lookup(detect_charset(declaration, "UTF-8")).name
            key = cp if cp == utf else b"unknown:" + match[1].upper()
            _require(
                identity is None or identity == key,
                path,
                offset + match.start(),
                "Conflicting charset declarations",
            )
            if selected is None:
                selected, identity = charset, key
    return selected or fallback


def _decode(raw: bytes, charset: str, path: Path, offset: int) -> str:
    try:
        return to_utf8(raw, charset)
    except UnicodeDecodeError as error:
        _fail(path, offset + error.start, error)
    except (ValueError, LookupError) as error:
        _fail(path, offset, error)


def _parse_controls(text: str) -> MessageControlLines:
    parsed = parse_message(text)
    kludges: list[ControlLine] = []
    normalized: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", text):
        match = re.fullmatch(r"\x01([A-Za-z][A-Za-z0-9-]*)(?::\s*|\s+)(.*)", raw)
        if match:
            name, value = match[1].upper(), match[2].strip()
            kludges.append(ControlLine(name=name, value=value, raw=raw))
            normalized.append(
                f"{name}: {value}"
                if name in {"PATH", "SEEN-BY"}
                else f"\x01{name}: {value}"
            )
        else:
            normalized.append(raw)
    scalars = parse_message("\n".join(normalized))
    return replace(
        parsed,
        kludges=tuple(kludges),
        msgid=scalars.msgid,
        reply=scalars.reply,
        charset=scalars.charset,
        seen_by=scalars.seen_by,
        path=scalars.path,
    )


def _controls(
    records: list[tuple[bytes, int]],
    body: str,
    body_raw: bytes,
    charset: str,
    path: Path,
    body_offset: int,
) -> tuple[MessageControlLines, list[tuple[ControlLine, int]]]:
    decoded_records = [
        (_decode(raw, charset, path, offset), offset) for raw, offset in records
    ]
    header_text = "\n".join(text for text, _offset in decoded_records)
    header_controls = _parse_controls(header_text)
    body_controls = _parse_controls(body)
    seen: dict[str, str] = {}
    positioned_controls = [
        (control, offset)
        for text, offset in decoded_records
        for control in _parse_controls(text).kludges
    ]
    positions = {
        name: iter(
            body_offset + match.start()
            for match in re.finditer(
                rb"(?m)(?:^|(?<=[\r\n]))\x01" + name.encode("ascii") + rb"(?::|\s)",
                body_raw,
                re.IGNORECASE,
            )
        )
        for name in {control.name for control in body_controls.kludges}
    }
    for control in body_controls.kludges:
        if control.name in positions:
            offset = next(positions[control.name], body_offset)
            positioned_controls.append((control, offset))
    for control, offset in positioned_controls:
        if control.name in {"MSGID", "REPLY"}:
            _require(
                control.name not in seen or seen[control.name] == control.value,
                path,
                offset,
                f"Conflicting {control.name} values",
            )
            seen[control.name] = control.value
    combined = replace(
        body_controls,
        kludges=header_controls.kludges + body_controls.kludges,
        msgid=header_controls.msgid
        if header_controls.msgid is not None
        else body_controls.msgid,
        reply=header_controls.reply
        if header_controls.reply is not None
        else body_controls.reply,
        charset=header_controls.charset
        if header_controls.charset is not None
        else body_controls.charset,
        seen_by=header_controls.seen_by + body_controls.seen_by,
        path=header_controls.path + body_controls.path,
    )
    return combined, positioned_controls


def _date(raw: int, ftsc: bytes) -> datetime | None:
    if raw == 0:
        return parse_date(read_null_padded_field(ftsc, 0, 20))
    try:
        return datetime(
            1980 + ((raw >> 9) & 127),
            (raw >> 5) & 15,
            raw & 31,
            (raw >> 27) & 31,
            (raw >> 21) & 63,
            ((raw >> 16) & 31) * 2,
            tzinfo=UTC,
        )
    except ValueError:
        return None


class SquishReader:
    """Validate the active index prefix before returning messages in UID order.

    SQD and SQI must be stable; separate reads do not create an atomic snapshot.
    """

    def read(
        self,
        path: str | PathLike[str],
        options: ReaderOptions | None = None,
    ) -> Iterable[ParsedMessage]:
        base = Path(path)
        sqd, sqi = (_file(base, suffix) for suffix in (".SQD", ".SQI"))
        data, index = sqd.read_bytes(), sqi.read_bytes()
        _slice(data, 0, 256, sqd)
        _require(
            struct.unpack_from("<H", data, 0)[0] == 256,
            sqd,
            0,
            "Unsupported base header size",
        )
        _require(
            struct.unpack_from("<H", data, 130)[0] == 28,
            sqd,
            130,
            "Unsupported frame header size",
        )
        count = struct.unpack_from("<I", data, 4)[0]
        _require(
            len(index) % 12 == 0, sqi, len(index) // 12 * 12, "Truncated index record"
        )
        _slice(index, 0, count * 12, sqi)
        frames: list[tuple[int, int, int]] = []
        previous = 0
        for position in range(count):
            offset, uid, _hash = _INDEX.unpack_from(index, position * 12)
            _require(
                previous < uid <= 0xFFFFFFFF,
                sqi,
                position * 12 + 4,
                "UIDs must be positive, unique and increasing",
            )
            previous = uid
            _require(
                offset >= 256,
                sqi,
                position * 12,
                "Frame offset points into base header",
            )
            signature, _next, _prev, length, total, clen, kind, _reserved = (
                _FRAME.unpack(_slice(data, offset, 28, sqd))
            )
            _require(signature == 0xAFAE4453, sqd, offset, "Invalid frame signature")
            _require(
                kind == 0,
                sqd,
                offset + 24,
                "Active frame is free, compressed, updating or unsupported",
            )
            _require(
                238 <= total <= length,
                sqd,
                offset + 16,
                "Invalid frame payload lengths",
            )
            _require(
                clen <= total - 238,
                sqd,
                offset + 20,
                "Control block exceeds message payload",
            )
            _slice(data, offset + 28, length, sqd)
            frames.append((offset, offset + 28 + length, uid))
        ranges = sorted(frames)
        for previous_frame, current in zip(ranges, ranges[1:], strict=False):
            _require(
                current[0] >= previous_frame[1],
                sqd,
                current[0],
                "Reused or overlapping active frame",
            )
        return tuple(
            self._message(data, sqd, offset, uid, options or ReaderOptions())
            for offset, _end, uid in frames
        )

    @staticmethod
    def _message(
        data: bytes, path: Path, offset: int, uid: int, options: ReaderOptions
    ) -> ParsedMessage:
        _signature, _next, _prev, _length, total, clen, _kind, _reserved = (
            _FRAME.unpack_from(data, offset)
        )
        start = offset + 28
        fixed = _slice(data, start, 238, path)
        attributes = struct.unpack_from("<I", fixed, 0)[0]
        header_uid = struct.unpack_from("<I", fixed, 214)[0]
        _require(
            not attributes & _MSGUID or header_uid == uid,
            path,
            start + 214,
            "Header UID disagrees with index",
        )
        control_offset = start + 238
        records = _control_records(
            _slice(data, control_offset, clen, path), path, control_offset
        )
        body_offset = control_offset + clen
        body_raw = _slice(data, body_offset, total - 238 - clen, path)
        charset = _charset(records, body_raw, path, body_offset, options)
        body = parse_body(_decode(body_raw, charset, path, body_offset))
        controls, positioned_controls = _controls(
            records, body, body_raw, charset, path, body_offset
        )
        names = [
            _decode(read_null_padded_field(fixed, o, n), charset, path, start + o)
            for o, n in ((4, 36), (40, 36), (76, 72))
        ]
        addresses: dict[str, int] = {}
        for side, address_offset in (("from", 148), ("to", 156)):
            for component, value in zip(
                ("zone", "net", "node", "point"),
                struct.unpack_from("<4H", fixed, address_offset),
                strict=True,
            ):
                addresses[f"{side}_{component}"] = value
        try:
            sender, recipient = resolve_addresses(addresses, positioned_controls)
        except AddressError as error:
            _fail(path, error.offset, error)
        written = struct.unpack_from("<I", fixed, 164)[0]
        date = _date(written, fixed[218:238])
        reply_to, first_reply = struct.unpack_from("<II", fixed, 174)
        external_id = controls.msgid
        if external_id is None:
            external_id = synthetic_id(
                names[0], names[1], names[2], date.isoformat() if date else None, body
            )
        return ParsedMessage(
            msgno=uid,
            from_name=names[0],
            to_name=names[1],
            subject=names[2],
            body_text=body,
            attributes_raw=attributes,
            posted_at=date,
            external_id=external_id,
            from_address=str(sender) if sender is not None else None,
            to_address=str(recipient) if recipient is not None else None,
            reply_to_msgno=reply_to or None,
            reply1st_msgno=first_reply or None,
            control_lines=controls,
            provenance=MessageProvenance(
                source_type="squish",
                source_path=str(path),
                source_id=str(uid),
                source_offset=offset,
            ),
        )
