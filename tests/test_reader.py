import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from golded_ftn import (
    MessageBaseReader,
    ParsedMessage,
    ParserException,
    ReaderOptions,
    synthetic_id,
)

from golded_ftn_squish import SquishReader
from tests._fixtures import area, frame, header, packed_date, patch


def read(base: Path) -> tuple[ParsedMessage, ...]:
    return tuple(SquishReader().read(base))


def test_public_empty_and_missing(tmp_path: Path) -> None:
    reader: MessageBaseReader = SquishReader()
    assert tuple(reader.read(area(tmp_path))) == ()
    with pytest.raises(FileNotFoundError):
        reader.read(tmp_path / "missing")


def test_full_message(tmp_path: Path) -> None:
    base = area(
        tmp_path,
        frames=(
            (
                3,
                frame(
                    control=b"\x01MSGID: 2:236/0 abc\x01PID: test\x00",
                    fixed=header(reply_to=1, replies=(9, 11, 12)),
                ),
            ),
        ),
    )
    (message,) = read(base)
    assert message.msgno == 3
    assert (message.from_name, message.to_name, message.subject) == (
        "Alice",
        "Bob",
        "Hello",
    )
    assert message.body_text == "Hello\nworld"
    assert message.external_id == "2:236/0 abc"
    assert message.attributes_raw == 0x20000
    assert message.from_address == "2:236/0.5"
    assert message.to_address == "2:236/10"
    assert message.posted_at == datetime(2026, 10, 4, 12, 34, 56, tzinfo=UTC)
    assert message.reply_to_msgno == 1 and message.reply1st_msgno == 9
    assert message.reply_next_msgno is None
    assert message.control_lines is not None
    assert [c.name for c in message.control_lines.kludges] == ["MSGID", "PID"]
    assert message.provenance is not None
    assert (
        message.provenance.source_type,
        message.provenance.source_path,
        message.provenance.source_id,
        message.provenance.source_offset,
    ) == ("squish", str(base) + ".SQD", "3", 256)
    assert message.area_code is message.area_name is None


def test_index_authority_and_allocated_tail(tmp_path: Path) -> None:
    base = area(
        tmp_path,
        frames=((3, frame()), (17, frame(uid=17))),
        tail=b"\x00" * 24 + struct.pack("<iII", 0, 0xFFFFFFFF, 0xFFFFFFFF),
    )
    with Path(str(base) + ".SQD").open("ab") as handle:
        handle.write(frame(uid=1, kind=1))
    assert [m.msgno for m in read(base)] == [3, 17]


def test_mixed_extensions(tmp_path: Path) -> None:
    assert read(
        area(tmp_path, frames=((3, frame()),), data_suffix=".sQd", index_suffix=".sQi")
    )


def test_ambiguous_and_nonregular(tmp_path: Path) -> None:
    base = area(tmp_path)
    other = Path(str(base) + ".sqd")
    other.write_bytes(b"")
    if len(list(tmp_path.iterdir())) < 3:
        pytest.skip("Case-insensitive filesystem")
    with pytest.raises(ParserException, match="Ambiguous"):
        read(base)
    other.unlink()
    Path(str(base) + ".SQD").unlink()
    Path(str(base) + ".SQD").mkdir()
    with pytest.raises(OSError):
        read(base)


@pytest.mark.parametrize(
    ("target", "offset", "fmt", "value"),
    [
        ("SQD", 0, "H", 255),
        ("SQD", 130, "H", 29),
        ("SQD", 4, "I", 2),
        ("SQD", 256, "I", 0),
        ("SQD", 268, "I", 237),
        ("SQD", 272, "I", 237),
        ("SQD", 276, "I", 999),
        ("SQD", 270, "H", 65535),
        ("SQI", 0, "i", -1),
        ("SQI", 0, "i", 1),
        ("SQI", 0, "i", 9999),
        ("SQI", 4, "I", 0),
        ("SQD", 498, "I", 99),
    ],
)
def test_bad_structure(
    tmp_path: Path, target: str, offset: int, fmt: str, value: int
) -> None:
    base = area(tmp_path, frames=((3, frame()),))
    path = Path(str(base) + "." + target)
    patch(path, offset, "<" + fmt, value)
    with pytest.raises(ParserException) as failure:
        read(base)
    assert str(base) in str(failure.value)
    assert "offset" in str(failure.value)
    assert failure.value.__cause__ is not None


@pytest.mark.parametrize("kind", [1, 2, 3, 99])
def test_non_normal_frames(tmp_path: Path, kind: int) -> None:
    with pytest.raises(ParserException):
        read(area(tmp_path, frames=((3, frame(kind=kind)),)))


@pytest.mark.parametrize("target", ["SQD", "SQI"])
def test_truncation(tmp_path: Path, target: str) -> None:
    base = area(tmp_path, frames=((3, frame()),))
    path = Path(str(base) + "." + target)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ParserException):
        read(base)


@pytest.mark.parametrize("uids", [(3, 3), (4, 3)])
def test_uid_order(tmp_path: Path, uids: tuple[int, int]) -> None:
    with pytest.raises(ParserException):
        read(area(tmp_path, frames=tuple((uid, frame(uid=uid)) for uid in uids)))


def test_header_uid_ignored_without_flag(tmp_path: Path) -> None:
    (message,) = read(
        area(tmp_path, frames=((3, frame(fixed=header(uid=55, attributes=1))),))
    )
    assert message.msgno == 3 and message.attributes_raw == 1


def test_reused_offset_and_overlap(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7))))
    patch(Path(str(base) + ".SQI"), 12, "<i", 256)
    with pytest.raises(ParserException):
        read(base)
    base = area(tmp_path, frames=((3, frame(slack=b"!")), (7, frame(uid=7))))
    patch(Path(str(base) + ".SQD"), 268, "<I", 266 + 28 + 266)
    with pytest.raises(ParserException):
        read(base)


def test_slack_and_ignored_links(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame(slack=b"arbitrary")),))
    patch(Path(str(base) + ".SQD"), 260, "<i", -999)
    patch(Path(str(base) + ".SQD"), 20, "<I", 1)
    assert read(base)[0].body_text == "Hello\nworld"


@pytest.mark.parametrize(
    "control",
    [
        b"",
        b"\x00",
        b"\x00\x00",
        b"\x01PID: test",
        b"\x01PID: test\x00",
        b"\x01PID: test\x00\x00",
        b"\x01PID: test\x01PID: other\x00",
    ],
)
def test_valid_controls(tmp_path: Path, control: bytes) -> None:
    assert read(area(tmp_path, frames=((3, frame(control=control)),)))


@pytest.mark.parametrize(
    "control",
    [
        b"PID: test",
        b"\x01",
        b"\x01PID: test\x01",
        b"\x01\x01PID: test",
        b"\x00junk",
        b"\x01PID: test\x00junk",
        b"\x01PID: test\rjunk",
    ],
)
def test_bad_controls(tmp_path: Path, control: bytes) -> None:
    with pytest.raises(ParserException):
        read(area(tmp_path, frames=((3, frame(control=control)),)))


@pytest.mark.parametrize(
    ("date", "ftsc", "expected"),
    [
        (0, b"", None),
        (0, b"04 Oct 26  13:14:15", datetime(2026, 10, 4, 13, 14, 15)),
        (0, b"bad", None),
        (packed_date(month=0), b"04 Oct 26  13:14:15", None),
        (packed_date(month=2, day=30), b"", None),
        (packed_date(hour=31), b"", None),
        (packed_date(minute=63), b"", None),
    ],
)
def test_dates(
    tmp_path: Path, date: int, ftsc: bytes, expected: datetime | None
) -> None:
    (message,) = read(
        area(tmp_path, frames=((3, frame(fixed=header(date=date, ftsc=ftsc))),))
    )
    assert message.posted_at == expected


def test_decode_control_precedence_and_body_metadata(tmp_path: Path) -> None:
    control = b"\x01CHRS: UTF-8 4\x01MSGID: same\x01MSGID: same\x01AREA: TEST\x00"
    body = "\x01MSGID: same\rSEEN-BY: 236/1\rPATH: 236/2\rÆøå".encode()
    (message,) = read(
        area(
            tmp_path,
            frames=(
                (
                    3,
                    frame(
                        control=control, body=body, fixed=header(sender="Åse".encode())
                    ),
                ),
            ),
        )
    )
    assert message.from_name == "Åse" and message.body_text.endswith("Æøå")
    assert message.external_id == "same" and message.area_code is None
    assert message.control_lines is not None
    assert message.control_lines.seen_by == ("236/1",)
    assert len([c for c in message.control_lines.kludges if c.name == "MSGID"]) == 3


@pytest.mark.parametrize(
    ("control", "body"),
    [
        (b"\x01CHRS: UTF-8", b"\x01CHRS: CP850\rtext"),
        (b"\x01MSGID: a\x01MSGID: b", b""),
        (b"\x01MSGID: a", b"\x01MSGID: b"),
        (b"\x01REPLY: a", b"\x01REPLY: b"),
        (b"\x01CHRS: UTF-8", b"\xff"),
    ],
)
def test_metadata_conflicts(tmp_path: Path, control: bytes, body: bytes) -> None:
    with pytest.raises(ParserException):
        read(area(tmp_path, frames=((3, frame(control=control, body=body)),)))


def test_cp850_fallback_and_synthetic_id(tmp_path: Path) -> None:
    (message,) = read(area(tmp_path, frames=((3, frame(body="Æøå".encode("cp850"))),)))
    assert message.body_text == "Æøå"
    assert message.external_id == synthetic_id(
        "Alice", "Bob", "Hello", "2026-10-04T12:34:56+00:00", "Æøå"
    )


def test_fallback_options(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame(body="Å".encode())),))
    assert (
        tuple(SquishReader().read(base, ReaderOptions(fallback_charset="UTF-8")))[
            0
        ].body_text
        == "Å"
    )
    with pytest.raises(ParserException):
        tuple(
            SquishReader().read(base, ReaderOptions(fallback_charset="invalid-codec"))
        )


def test_address_supplement_and_conflict(tmp_path: Path) -> None:
    fixed = header(orig=(0, 236, 0, 0), dest=(0, 236, 10, 0))
    (message,) = read(
        area(
            tmp_path,
            frames=(
                (3, frame(fixed=fixed, control=b"\x01INTL 2:236/10 2:236/0\x01FMPT 5")),
            ),
        )
    )
    assert message.from_address == "2:236/0.5" and message.to_address == "2:236/10"
    with pytest.raises(ParserException):
        read(area(tmp_path, frames=((3, frame(control=b"\x01FMPT 8")),)))


def test_actual_overlap_with_valid_both_extents(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7))))
    data_path = Path(str(base) + ".SQD")
    original_length = struct.unpack_from("<I", data_path.read_bytes(), 268)[0]
    patch(data_path, 268, "<I", original_length + 1)
    with pytest.raises(ParserException, match="overlapping"):
        read(base)


def test_physical_frame_order_and_old_unindexed_frame(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7))))
    index_path = Path(str(base) + ".SQI")
    index = index_path.read_bytes()
    first_offset, second_offset = (
        struct.unpack_from("<i", index, 0)[0],
        struct.unpack_from("<i", index, 12)[0],
    )
    patch(index_path, 0, "<i", second_offset)
    patch(index_path, 12, "<i", first_offset)
    data_path = Path(str(base) + ".SQD")
    patch(data_path, first_offset + 28 + 214, "<I", 7)
    patch(data_path, second_offset + 28 + 214, "<I", 3)
    with data_path.open("ab") as handle:
        handle.write(frame(uid=55))
    assert [message.msgno for message in read(base)] == [3, 7]
    first_message = read(base)[0]
    assert first_message.provenance is not None
    assert first_message.provenance.source_offset == second_offset


def test_read_returns_no_partial_messages(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7, kind=3))))
    with pytest.raises(ParserException):
        SquishReader().read(base)


def test_short_base_header(tmp_path: Path) -> None:
    base = area(tmp_path)
    Path(str(base) + ".SQD").write_bytes(b"\x00" * 255)
    with pytest.raises(ParserException):
        read(base)


def test_nul_padded_names_and_missing_addresses(tmp_path: Path) -> None:
    fixed = header(sender=b"Alice\x00garbage", orig=(0, 0, 0, 0), dest=(0, 0, 0, 0))
    (message,) = read(area(tmp_path, frames=((3, frame(fixed=fixed)),)))
    assert message.from_name == "Alice"
    assert message.from_address is message.to_address is None
    assert message.reply_to_msgno is message.reply1st_msgno is None


def test_routing_and_generic_controls_preserve_order(tmp_path: Path) -> None:
    control = b"\x01PID: one\x01PID: two\x01FLAGS DIR\x01PATH: 236/1\x01PATH: 236/2"
    body = b"PATH: 236/3\r\x01PATH: 236/4\rPATH: 236/5\rSEEN-BY: 236/6"
    (message,) = read(area(tmp_path, frames=((3, frame(control=control, body=body)),)))
    assert message.control_lines is not None
    assert message.control_lines.path == ("236/1", "236/2", "236/3", "236/4", "236/5")
    assert message.control_lines.seen_by == ("236/6",)
    assert [c.value for c in message.control_lines.kludges if c.name == "PID"] == [
        "one",
        "two",
    ]
    assert [c.value for c in message.control_lines.kludges if c.name == "FLAGS"] == [
        "DIR"
    ]


def test_charset_conflict_has_control_offset(tmp_path: Path) -> None:
    first = b"\x01CHRS: UTF-8 4"
    second = b"\x01CHRS: CP850 2"
    base = area(tmp_path, frames=((3, frame(control=first + second)),))
    with pytest.raises(ParserException, match=f"offset {256 + 28 + 238 + len(first)}:"):
        read(base)


def test_charset_aliases_and_unknown_fallback(tmp_path: Path) -> None:
    for controls in (
        b"\x01CHRS: UTF-8\x01CHARSET: UTF8",
        b"\x01CHRS: UNKNOWN\x01CHRS: UNKNOWN",
    ):
        base = area(tmp_path, frames=((3, frame(control=controls, body="Å".encode())),))
        (message,) = tuple(
            SquishReader().read(base, ReaderOptions(fallback_charset="UTF-8"))
        )
        assert message.body_text == "Å"


def test_body_only_msgid_and_reply(tmp_path: Path) -> None:
    (message,) = read(
        area(tmp_path, frames=((3, frame(body=b"\x01MSGID: body\r\x01REPLY: parent")),))
    )
    assert message.external_id == "body"
    assert message.control_lines is not None and message.control_lines.reply == "parent"


def test_decoding_failure_in_name(tmp_path: Path) -> None:
    base = area(
        tmp_path,
        frames=((3, frame(control=b"\x01CHRS: UTF-8", fixed=header(sender=b"\xff"))),),
    )
    with pytest.raises(ParserException, match="offset 288:") as error:
        read(base)
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


def test_timestamp_ignores_environment_and_writer_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = area(tmp_path, frames=((3, frame(fixed=header(utc_offset=-600))),))
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    assert read(base)[0].posted_at == datetime(2026, 10, 4, 12, 34, 56, tzinfo=UTC)
    monkeypatch.setenv("TZ", "Europe/Copenhagen")
    assert read(base)[0].posted_at == datetime(2026, 10, 4, 12, 34, 56, tzinfo=UTC)


def test_timezone_aware_and_naive_date_limits(tmp_path: Path) -> None:
    (message,) = read(
        area(
            tmp_path,
            frames=(
                (
                    3,
                    frame(
                        fixed=header(
                            date=packed_date(
                                year=2107,
                                month=12,
                                day=31,
                                hour=23,
                                minute=59,
                                second=58,
                            )
                        )
                    ),
                ),
            ),
        )
    )
    assert message.posted_at == datetime(2107, 12, 31, 23, 59, 58, tzinfo=UTC)
    (message,) = read(
        area(
            tmp_path,
            frames=((3, frame(fixed=header(date=0, ftsc=b"31 Dec 69  23:59:59"))),),
        )
    )
    assert message.posted_at == datetime(2069, 12, 31, 23, 59, 59)


def test_maximum_unsigned_uid(tmp_path: Path) -> None:
    (message,) = read(area(tmp_path, frames=((0xFFFFFFFF, frame(uid=0xFFFFFFFF)),)))
    assert message.msgno == 0xFFFFFFFF


def test_decode_error_exact_byte_offset(tmp_path: Path) -> None:
    control = b"\x01CHRS: UTF-8\x00"
    base = area(tmp_path, frames=((3, frame(control=control, body=b"abc\xff")),))
    with pytest.raises(
        ParserException, match=f"offset {256 + 28 + 238 + len(control) + 3}:"
    ):
        read(base)


def test_header_id_conflict_exact_record_offset(tmp_path: Path) -> None:
    first = b"\x01MSGID: first"
    base = area(tmp_path, frames=((3, frame(control=first + b"\x01MSGID: second")),))
    with pytest.raises(ParserException, match=f"offset {256 + 28 + 238 + len(first)}:"):
        read(base)


def test_seen_by_mixed_order(tmp_path: Path) -> None:
    (message,) = read(
        area(
            tmp_path, frames=((3, frame(body=b"SEEN-BY: first\r\x01SEEN-BY: second")),)
        )
    )
    assert message.control_lines is not None
    assert message.control_lines.seen_by == ("first", "second")


def test_body_id_conflict_offset_after_utf8_crlf(tmp_path: Path) -> None:
    control = b"\x01CHRS: UTF-8\x01MSGID: first\x00"
    prefix = "Æøå\r\n".encode()
    base = area(
        tmp_path,
        frames=((3, frame(control=control, body=prefix + b"\x01MSGID: second")),),
    )
    expected = 256 + 28 + 238 + len(control) + len(prefix)
    with pytest.raises(ParserException, match=f"offset {expected}:"):
        read(base)


@pytest.mark.parametrize(
    "control",
    [
        b"\x01FMPT 6",
        b"\x01TOPT 9",
        b"\x01INTL 2:236/11 2:236/0",
    ],
)
def test_address_conflict_exact_header_control_offset(
    tmp_path: Path, control: bytes
) -> None:
    prefix = b"\x01CHRS: UTF-8\x01PID: test"
    base = area(
        tmp_path,
        frames=(
            (3, frame(control=prefix + control, fixed=header(dest=(2, 236, 10, 5)))),
        ),
    )
    expected = 256 + 28 + 238 + len(prefix)
    with pytest.raises(ParserException, match=f"offset {expected}:"):
        read(base)


def test_address_conflict_exact_body_line_offset(tmp_path: Path) -> None:
    control = b"\x01CHRS: UTF-8\x00"
    prefix = "Æøå\r\n".encode()
    base = area(
        tmp_path, frames=((3, frame(control=control, body=prefix + b"\x01FMPT 6")),)
    )
    expected = 256 + 28 + 238 + len(control) + len(prefix)
    with pytest.raises(ParserException, match=f"offset {expected}:"):
        read(base)


def test_canonical_base_layout_keep_days_128_frame_size_130(tmp_path: Path) -> None:
    base = tmp_path / "canonical"
    base_header = struct.pack(
        "<HH5I80s5iIHH124s",
        256,
        0,
        0,
        0,
        0,
        0,
        1,
        b"canonical",
        0,
        0,
        0,
        0,
        256,
        0,
        14,
        28,
        b"\x00" * 124,
    )
    assert len(base_header) == 256
    assert base_header[128:130] == b"\x0e\x00"
    assert base_header[130:132] == b"\x1c\x00"
    Path(str(base) + ".SQD").write_bytes(base_header)
    Path(str(base) + ".SQI").write_bytes(b"")
    assert read(base) == ()
    patch(Path(str(base) + ".SQD"), 128, "<HH", 28, 0)
    with pytest.raises(ParserException, match="offset 130:.*frame header size"):
        read(base)


def test_distinct_non_ascii_unknown_charset_tokens_conflict(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame(control=b"\x01CHRS: \xff\x01CHRS: \xfe")),))
    with pytest.raises(ParserException, match="Conflicting charset"):
        read(base)


def test_identical_non_ascii_unknown_charset_token_uses_fallback(
    tmp_path: Path,
) -> None:
    base = area(
        tmp_path,
        frames=(
            (
                3,
                frame(
                    control=b"\x01CHRS: \xff\x01CHRS: \xff", body="Æ".encode("cp850")
                ),
            ),
        ),
    )
    (message,) = read(base)
    assert message.body_text == "Æ"
