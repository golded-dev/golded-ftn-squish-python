"""Archive recovery is explicit and observable through core ReaderIssue callbacks."""

from pathlib import Path

import pytest
from golded_ftn import ParserException, ReaderIssue, ReaderOptions

from golded_ftn_squish import SquishReader
from tests._fixtures import area, frame, header, patch


def test_archive_options_require_observer() -> None:
    with pytest.raises(ValueError):
        ReaderOptions(archive_mode=True)


def test_archive_ignores_empty_segments_with_issues(tmp_path: Path) -> None:
    base = area(
        tmp_path, frames=((3, frame(control=b"\x01PID: test\x01\x01MSGID: test\x01")),)
    )
    with pytest.raises(ParserException):
        SquishReader().read(base)
    issues: list[ReaderIssue] = []
    messages = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert messages[0].external_id == "test"
    assert [issue.action for issue in issues] == ["recovered", "recovered"]
    assert [issue.code for issue in issues] == ["empty_control_segment"] * 2
    assert all(
        issue.source_type == "squish" and issue.source_id == "3" for issue in issues
    )
    assert all(issue.source_path == str(base) + ".SQD" for issue in issues)


def test_archive_uid_index_authority(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame(fixed=header(uid=99))),))
    issues: list[ReaderIssue] = []
    messages = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert messages[0].msgno == 3
    assert len(issues) == 1 and issues[0].code == "header_uid_mismatch"
    assert issues[0].action == "recovered" and issues[0].source_offset == 498


def test_archive_ascii_fallback(tmp_path: Path) -> None:
    control = b"\x01CHRS: ASCII 1\x00"
    body = "Ø".encode("cp850")
    base = area(tmp_path, frames=((3, frame(control=control, body=body)),))
    with pytest.raises(ParserException):
        SquishReader().read(base)
    issues: list[ReaderIssue] = []
    (message,) = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert message.body_text == "Ø"
    assert len(issues) == 1 and issues[0].code == "ascii_decode_fallback"
    assert issues[0].source_offset == 256 + 28 + 238 + len(control)
    assert issues[0].action == "recovered"
    assert "Ø" not in issues[0].detail


@pytest.mark.parametrize(
    "bad_frame",
    [
        frame(uid=7, control=b"\x01MSGID: private-a\x01MSGID: private-b"),
        frame(uid=7, control=b"\x01CHRS: UTF-8", body=b"\xff"),
        frame(uid=7, kind=2),
    ],
)
def test_archive_skips_damaged_indexed_records(
    tmp_path: Path, bad_frame: bytes
) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, bad_frame), (9, frame(uid=9))))
    issues: list[ReaderIssue] = []
    messages = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [message.msgno for message in messages] == [3, 9]
    assert len(issues) == 1 and issues[0].action == "skipped"
    assert issues[0].source_id == "7"
    assert "private-" not in issues[0].detail


def test_archive_stops_at_bad_index_uid(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7)), (9, frame(uid=9))))
    patch(Path(str(base) + ".SQI"), 16, "<I", 3)
    issues: list[ReaderIssue] = []
    messages = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [message.msgno for message in messages] == [3]
    assert len(issues) == 1 and issues[0].action == "stopped"
    assert issues[0].source_path == str(base) + ".SQI" and issues[0].source_offset == 16


def test_archive_stops_at_truncated_index_after_valid_prefix(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame()), (7, frame(uid=7))))
    index = Path(str(base) + ".SQI")
    index.write_bytes(index.read_bytes()[:-1])
    issues: list[ReaderIssue] = []
    messages = tuple(
        SquishReader().read(
            base, ReaderOptions(archive_mode=True, on_issue=issues.append)
        )
    )
    assert [message.msgno for message in messages] == [3]
    assert len(issues) == 1 and issues[0].action == "stopped"
    assert issues[0].source_path == str(index) and issues[0].source_offset == 12


def test_archive_global_header_failure_is_observed(tmp_path: Path) -> None:
    base = area(tmp_path)
    patch(Path(str(base) + ".SQD"), 130, "<H", 0)
    issues: list[ReaderIssue] = []
    assert (
        tuple(
            SquishReader().read(
                base, ReaderOptions(archive_mode=True, on_issue=issues.append)
            )
        )
        == ()
    )
    assert len(issues) == 1 and issues[0].action == "stopped"
    assert issues[0].source_id is None


def test_archive_callback_exception_propagates(tmp_path: Path) -> None:
    base = area(tmp_path, frames=((3, frame(control=b"\x01\x01PID: test")),))

    def callback(issue: ReaderIssue) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        SquishReader().read(base, ReaderOptions(archive_mode=True, on_issue=callback))


def test_archive_does_not_suppress_filesystem_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SquishReader().read(
            tmp_path / "absent",
            ReaderOptions(archive_mode=True, on_issue=lambda issue: None),
        )


def test_archive_ascii_fallback_accepts_core_alias(tmp_path: Path) -> None:
    base = area(
        tmp_path, frames=((3, frame(control=b"\x01CHRS: ASCII 1", body=b"\x82")),)
    )
    issues: list[ReaderIssue] = []
    message = tuple(
        SquishReader().read(
            base,
            ReaderOptions(
                archive_mode=True, on_issue=issues.append, fallback_charset="IBMPC"
            ),
        )
    )[0]
    assert message.body_text == "é"
    assert [i.code for i in issues] == ["ascii_decode_fallback"]
