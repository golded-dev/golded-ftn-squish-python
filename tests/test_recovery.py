from __future__ import annotations

import os
import struct
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from golded_ftn import (
    CommitState,
    ConflictError,
    ControlLine,
    LockTimeoutError,
    MessagePatch,
    RecoveryError,
    WriterOptions,
)
from golded_ftn._writer_io import locks

from golded_ftn_squish import (
    SquishAdapterError,
    SquishWriter,
    SquishWriterOptions,
)
from golded_ftn_squish.writer import _sqi_write_ranges
from tests.test_writer import fixture, outgoing


def _messages(tmp_path: Path) -> Path:
    messages = tmp_path / "messages"
    messages.mkdir()
    return messages


def _recovery_root(tmp_path: Path) -> Path:
    return tmp_path / "recovery"


def _sql_bytes(base: Path) -> bytes:
    return Path(str(base) + ".SQL").read_bytes()


def test_recovery_session_accepts_concurrent(tmp_path: Path) -> None:
    base = fixture(_messages(tmp_path))
    with SquishWriter().recovery_session(
        base,
        recovery_root=_recovery_root(tmp_path),
        options=WriterOptions(concurrent=True),
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert session.pending() == ()


def test_prepare_apply_settle_committed_updates_under_held_lock(
    tmp_path: Path,
) -> None:
    base = fixture(_messages(tmp_path))
    sql_before = _sql_bytes(base)
    sqd = Path(str(base) + ".SQD")
    sqi = Path(str(base) + ".SQI")
    before_sqd, before_sqi = sqd.read_bytes(), sqi.read_bytes()
    writer = SquishWriter()
    with writer.recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        st = os.stat(sqd)
        assert session.physical_base.format == "squish"
        assert session.physical_base.lock_inode == (st.st_dev, st.st_ino)
        assert session.physical_base.canonical_key == os.path.realpath(sqd)

        mutation = session.prepare_append(uuid.uuid4(), outgoing(subject="Recovered"))
        ready = threading.Event()
        release = threading.Event()
        timed_out = threading.Event()

        def contend() -> None:
            ready.set()
            try:
                with locks.acquire(sqd, timeout=0.05):
                    pass
            except LockTimeoutError:
                timed_out.set()
            finally:
                release.wait(5)

        thread = threading.Thread(target=contend)
        thread.start()
        assert ready.wait(5)
        result = session.apply(mutation)
        assert timed_out.wait(5)
        session.settle(mutation, CommitState.COMMITTED)
        release.set()
        thread.join(5)
        assert result.identity.msgno == 100
        assert session.read(100).message.subject == "Recovered"
        assert session.pending() == ()
    assert sqd.read_bytes() != before_sqd
    assert sqi.read_bytes() != before_sqi
    assert struct.unpack_from("<II", sqd.read_bytes(), 4) == (2, 2)
    assert _sql_bytes(base) == sql_before


def test_settle_uncommitted_restores_chains_and_index(tmp_path: Path) -> None:
    messages = _messages(tmp_path)
    base = fixture(messages)
    sql_before = _sql_bytes(base)
    before = {
        path.name: path.read_bytes() for path in messages.iterdir() if path.is_file()
    }
    with SquishWriter().recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(uuid.uuid4(), outgoing())
        session.apply(mutation)
        assert session.read(100).message.subject == "Subject"
        session.settle(mutation, CommitState.UNCOMMITTED)
        assert session.pending() == ()
        with pytest.raises(ConflictError, match="does not exist"):
            session.read(100)
        current = session.read(3)
        assert current.identity.msgno == 3
    after = {
        path.name: path.read_bytes() for path in messages.iterdir() if path.is_file()
    }
    assert after["area.SQD"] == before["area.SQD"]
    assert after["area.SQI"] == before["area.SQI"]
    assert after["area.SQL"] == sql_before


def test_recover_uncommitted_restores_and_keeps_uid_stable(tmp_path: Path) -> None:
    base = fixture(_messages(tmp_path))
    sql_before = _sql_bytes(base)
    recovery_root = _recovery_root(tmp_path)
    writer = SquishWriter()
    tx_id = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx_id, outgoing(subject="Rolled"))
        applied = session.apply(mutation)
        assert applied.identity.msgno == 100
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        pending = session.pending()
        assert len(pending) == 1
        assert pending[0].transaction_id == tx_id
        assertion = session.issue_offline_assertion()
        recovered = session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
        assert tuple(item.transaction_id for item in recovered) == (tx_id,)
        assert session.pending() == ()
        with pytest.raises(ConflictError, match="does not exist"):
            session.read(100)
    assert _sql_bytes(base) == sql_before
    with writer.open(base) as crud:
        again = crud.append(outgoing(subject="Same UID"))
        assert again.identity.msgno == 100
        assert crud.read(3).identity.msgno == 3


def test_uid_stable_across_live_uncommitted_settle(tmp_path: Path) -> None:
    base = _messages(tmp_path) / "area"
    writer = SquishWriter()
    writer.create(base)
    Path(str(base) + ".SQL").write_bytes(b"lastread untouched")
    with writer.recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        first = session.prepare_append(uuid.uuid4(), outgoing())
        first_result = session.apply(first)
        assert first_result.identity.msgno == 2
        session.settle(first, CommitState.UNCOMMITTED)
        second = session.prepare_append(uuid.uuid4(), outgoing(subject="Retry"))
        second_result = session.apply(second)
        assert second_result.identity.msgno == 2
        session.settle(second, CommitState.COMMITTED)
        assert session.read(2).message.subject == "Retry"
    assert Path(str(base) + ".SQL").read_bytes() == b"lastread untouched"


def test_lastread_unchanged_through_recovery_update(tmp_path: Path) -> None:
    base = fixture(_messages(tmp_path))
    sql = Path(str(base) + ".SQL")
    sql.write_bytes(b"sql-secret-slot")
    with SquishWriter().recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        source = session.read(3)
        mutation = session.prepare_update(
            uuid.uuid4(),
            source.identity,
            MessagePatch(body_text="moved"),
            source.revision,
        )
        session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        assert session.read(3).identity.msgno == 3
        assert session.read(3).message.body_text == "moved"
    assert sql.read_bytes() == b"sql-secret-slot"


def test_dst_gap_without_tzutc_raises_explicit_tzutc_accepted(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = SquishWriter()
    writer.create(base)
    options = SquishWriterOptions(timezone="Europe/Copenhagen")
    gap = datetime(2026, 3, 29, 2, 30)
    fold = datetime(2026, 10, 25, 2, 30)
    with writer.open(base, options) as session:
        with pytest.raises(SquishAdapterError) as gap_error:
            session.append(outgoing(posted_at=gap))
        assert gap_error.value.code == "dst_gap"
        assert "Europe/Copenhagen" not in str(gap_error.value)
        with pytest.raises(SquishAdapterError) as fold_error:
            session.append(outgoing(posted_at=fold))
        assert fold_error.value.code == "dst_fold"
        accepted = session.append(
            outgoing(
                posted_at=gap,
                control_lines=(ControlLine(name="TZUTC", value="+0100", raw=""),),
            )
        )
        message = session.read(accepted.identity.msgno).message
        assert message.posted_at == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
        winter = session.append(outgoing(posted_at=datetime(2026, 1, 15, 12, 0)))
        assert session.read(winter.identity.msgno).message.posted_at == datetime(
            2026, 1, 15, 11, 0, tzinfo=UTC
        )


def test_utc_year_bounds_and_odd_seconds(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = SquishWriter()
    writer.create(base)
    too_old = outgoing(
        posted_at=datetime(1980, 1, 1, 1, 0),
        control_lines=(ControlLine(name="TZUTC", value="+0200", raw=""),),
    )
    in_range = outgoing(
        posted_at=datetime(1980, 1, 1, 2, 0),
        control_lines=(ControlLine(name="TZUTC", value="+0200", raw=""),),
    )
    odd = outgoing(
        posted_at=datetime(2026, 10, 4, 12, 34, 57),
        control_lines=(ControlLine(name="TZUTC", value="0000", raw=""),),
    )
    with writer.open(base) as session:
        with pytest.raises(SquishAdapterError) as too_old_error:
            session.append(too_old)
        assert too_old_error.value.code == "year_out_of_range"
        created = session.append(in_range)
        assert session.read(created.identity.msgno).message.posted_at == datetime(
            1980, 1, 1, 0, 0, tzinfo=UTC
        )
        odd_result = session.append(odd)
        packed = struct.unpack_from(
            "<I",
            Path(str(base) + ".SQD").read_bytes(),
            odd_result.revision.location[0] + 28 + 164,
        )[0]
        assert ((packed >> 16) & 31) * 2 == 56
        text = (
            Path(str(base) + ".SQD")
            .read_bytes()[odd_result.revision.location[0] + 28 + 218 :]
            .split(b"\0", 1)[0]
        )
        assert text.endswith(b"12:34:57")
        accepted = session.append(
            outgoing(
                posted_at=datetime(2069, 12, 31, 23, 59, 58),
                control_lines=(ControlLine(name="TZUTC", value="0000", raw=""),),
            )
        )
        assert session.read(accepted.identity.msgno).message.posted_at == datetime(
            2069, 12, 31, 23, 59, 58, tzinfo=UTC
        )


def test_naive_posted_at_without_timezone_still_rejected(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    with SquishWriter().open(base) as session:
        with pytest.raises(SquishAdapterError) as error:
            session.append(outgoing(posted_at=datetime(2026, 1, 15, 12, 0)))
        assert error.value.code == "missing_timezone"


def test_sqi_planned_ranges_cover_truncation_and_empty_index() -> None:
    assert _sqi_write_ranges(b"\0" * 24, b"\0" * 12) == ((0, 24),)
    assert _sqi_write_ranges(b"\0" * 12, b"") == ((0, 12),)
    assert _sqi_write_ranges(b"", b"") == ()


def test_symlink_sqi_is_unsafe_recovery_path(tmp_path: Path) -> None:
    messages = _messages(tmp_path)
    base = fixture(messages)
    sqi = Path(str(base) + ".SQI")
    target = messages / "index-target.SQI"
    sqi.rename(target)
    sqi.symlink_to(target.name)
    with pytest.raises(RecoveryError) as error:
        with SquishWriter().recovery_session(
            base, recovery_root=_recovery_root(tmp_path)
        ):
            raise AssertionError("session must not open a symlink index")
    assert error.value.code == "unsafe_recovery_path"
    assert str(sqi) not in str(error.value)
    assert str(target) not in str(error.value)


def test_apply_oserror_omits_path_from_error(tmp_path: Path) -> None:
    base = fixture(_messages(tmp_path))
    leaked = Path("/secret-squish-apply-oserror/area.SQD")
    with SquishWriter().recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(uuid.uuid4(), outgoing())
        session.sqd = leaked
        with pytest.raises(RecoveryError) as error:
            session.apply(mutation)
        assert error.value.code == "unexpected_base_modification"
        assert "secret-squish-apply-oserror" not in str(error.value)
        assert "secret-squish-apply-oserror" not in repr(error.value)
        session.settle(mutation, CommitState.UNCOMMITTED)


def test_shorter_sqi_uncommitted_restore_keeps_truncated_tail(tmp_path: Path) -> None:
    messages = _messages(tmp_path)
    base = fixture(messages)
    sqi = Path(str(base) + ".SQI")
    original = sqi.read_bytes()
    slack = b"\xff" * 24
    sqi.write_bytes(original + slack)
    with SquishWriter().recovery_session(
        base, recovery_root=_recovery_root(tmp_path)
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(uuid.uuid4(), outgoing())
        session.apply(mutation)
        assert len(sqi.read_bytes()) < len(original) + len(slack)
        session.settle(mutation, CommitState.UNCOMMITTED)
    assert sqi.read_bytes() == original + slack


def test_aware_posted_at_is_rejected(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    with SquishWriter().open(base) as session:
        with pytest.raises(SquishAdapterError) as error:
            session.append(outgoing(posted_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC)))
        assert error.value.code == "aware_datetime"
        assert "2026" not in str(error.value)


def test_dropped_tzutc_uses_area_timezone_not_leftover(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = SquishWriter()
    writer.create(base)
    options = SquishWriterOptions(timezone="Europe/Copenhagen")
    gap = datetime(2026, 3, 29, 2, 30)
    with writer.open(base, options) as session:
        created = session.append(
            outgoing(
                posted_at=datetime(2026, 1, 15, 12, 0),
                control_lines=(ControlLine(name="TZUTC", value="+0100", raw=""),),
            )
        )
        source = session.read(created.identity.msgno)
        with pytest.raises(SquishAdapterError) as error:
            session.update(
                source.identity,
                MessagePatch(
                    posted_at=gap,
                    control_lines=(ControlLine(name="PID", value="no-tzutc", raw=""),),
                ),
                source.revision,
            )
        assert error.value.code == "dst_gap"
        assert session.read(created.identity.msgno).revision == source.revision


def test_year_2070_is_squish_adapter_error(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = SquishWriter()
    writer.create(base)
    with writer.open(base) as session:
        with pytest.raises(SquishAdapterError) as error:
            session.append(
                outgoing(
                    posted_at=datetime(2070, 1, 1),
                    control_lines=(ControlLine(name="TZUTC", value="0000", raw=""),),
                )
            )
        assert error.value.code == "year_out_of_range"
        assert "2070" not in str(error.value)
        assert "2107" not in str(error.value)


def test_prepare_append_missing_binding_beats_dst_error(tmp_path: Path) -> None:
    base = _messages(tmp_path) / "area"
    writer = SquishWriter()
    writer.create(base)
    options = SquishWriterOptions(timezone="Europe/Copenhagen")
    with writer.recovery_session(
        base, recovery_root=_recovery_root(tmp_path), options=options
    ) as session:
        with pytest.raises(RecoveryError) as error:
            session.prepare_append(
                uuid.uuid4(), outgoing(posted_at=datetime(2026, 3, 29, 2, 30))
            )
        assert error.value.code == "missing_recovery_binding"
