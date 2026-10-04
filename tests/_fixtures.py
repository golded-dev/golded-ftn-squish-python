"""Synthetic classic Squish records from the public byte layout."""

import struct
from pathlib import Path


def packed_date(
    year: int = 2026,
    month: int = 10,
    day: int = 4,
    hour: int = 12,
    minute: int = 34,
    second: int = 56,
) -> int:
    return (
        day
        | month << 5
        | (year - 1980) << 9
        | (second // 2) << 16
        | minute << 21
        | hour << 27
    )


def header(
    *,
    uid: int = 3,
    attributes: int = 0x20000,
    sender: bytes = b"Alice",
    recipient: bytes = b"Bob",
    subject: bytes = b"Hello",
    orig: tuple[int, int, int, int] = (2, 236, 0, 5),
    dest: tuple[int, int, int, int] = (2, 236, 10, 0),
    date: int | None = None,
    ftsc: bytes = b"",
    utc_offset: int = 60,
    reply_to: int = 0,
    replies: tuple[int, ...] = (),
) -> bytes:
    data = bytearray(238)
    struct.pack_into("<I", data, 0, attributes)
    for offset, size, value in (
        (4, 36, sender),
        (40, 36, recipient),
        (76, 72, subject),
    ):
        data[offset : offset + min(size, len(value))] = value[:size]
    struct.pack_into("<4H", data, 148, *orig)
    struct.pack_into("<4H", data, 156, *dest)
    struct.pack_into(
        "<IIhI",
        data,
        164,
        packed_date() if date is None else date,
        0,
        utc_offset,
        reply_to,
    )
    struct.pack_into("<9I", data, 178, *(replies + (0,) * (9 - len(replies))))
    struct.pack_into("<I", data, 214, uid)
    data[218 : 218 + min(len(ftsc), 20)] = ftsc[:20]
    return bytes(data)


def frame(
    *,
    uid: int = 3,
    control: bytes = b"",
    body: bytes = b"Hello\rworld\x00",
    fixed: bytes | None = None,
    kind: int = 0,
    slack: bytes = b"",
) -> bytes:
    payload = (header(uid=uid) if fixed is None else fixed) + control + body
    return (
        struct.pack(
            "<IiiIIIHH",
            0xAFAE4453,
            0,
            0,
            len(payload) + len(slack),
            len(payload),
            len(control),
            kind,
            0,
        )
        + payload
        + slack
    )


def area(
    directory: Path,
    *,
    frames: tuple[tuple[int, bytes], ...] = (),
    tail: bytes = b"",
    data_suffix: str = ".SQD",
    index_suffix: str = ".SQI",
) -> Path:
    base = directory / "area"
    data = bytearray(256)
    struct.pack_into("<HH5I", data, 0, 256, 0, len(frames), len(frames), 0, 0, 100)
    struct.pack_into("<H", data, 130, 28)
    index = bytearray()
    for uid, record in frames:
        index += struct.pack("<iII", len(data), uid, 0)
        data += record
    struct.pack_into("<i", data, 120, len(data))
    Path(str(base) + data_suffix).write_bytes(data)
    Path(str(base) + index_suffix).write_bytes(index + tail)
    return base


def patch(path: Path, offset: int, fmt: str, *values: int) -> None:
    data = bytearray(path.read_bytes())
    struct.pack_into(fmt, data, offset, *values)
    path.write_bytes(data)
