from __future__ import annotations

import os
import struct
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from golded_ftn import (
    ConflictError,
    ControlLine,
    FtnAddress,
    LockTimeoutError,
    MessagePatch,
    OutgoingMessage,
    ParserException,
    RollbackError,
    UnsupportedOperationError,
    WriterError,
    WriterOptions,
)
from golded_ftn._writer_io import IO, locks

from golded_ftn_squish import SquishReader, SquishWriter
from tests._fixtures import area, frame, header, patch


def fixture(directory: Path) -> Path:
    base = area(
        directory,
        frames=(
            (
                3,
                frame(
                    fixed=header(attributes=0xA0020000, replies=tuple(range(10, 19))),
                    control=b"\x01X-UNKNOWN: opaque\0",
                ),
            ),
        ),
    )
    sqd = Path(str(base) + ".SQD")
    patch(sqd, 104, "<ii", 256, 256)
    Path(str(base) + ".SQL").write_bytes(b"lastread untouched")
    return base


def outgoing(**changes: Any) -> OutgoingMessage:
    values: dict[str, Any] = dict(
        from_name="Alice", to_name="Bob", subject="Subject", body_text="Body"
    )
    values.update(changes)
    return OutgoingMessage(**values)


def test_create_layout_no_overwrite(tmp_path: Path) -> None:
    writer = SquishWriter()
    base = tmp_path / "area"
    writer.create(base)
    data = Path(str(base) + ".SQD").read_bytes()
    assert len(data) == 256
    assert struct.unpack_from("<HH5I", data) == (256, 0, 0, 0, 0, 0, 2)
    assert struct.unpack_from("<i", data, 120)[0] == 256
    assert struct.unpack_from("<H", data, 130)[0] == 28
    assert Path(str(base) + ".SQI").read_bytes() == b""
    assert Path(str(base) + ".SQL").read_bytes() == b""
    with pytest.raises(FileExistsError):
        writer.create(base)


def test_independent_fixture_update_delete_and_unrelated_changes(
    tmp_path: Path,
) -> None:
    base = fixture(tmp_path)
    writer = SquishWriter()
    with writer.open(base) as first, writer.open(base) as second:
        original = first.read(3)
        appended = second.append(outgoing())
        # Updating the chain's next pointer does not invalidate the old revision.
        updated = first.update(
            original.identity,
            MessagePatch(attributes_raw=0xB0020000),
            original.revision,
        )
        assert updated.identity.msgno == 3
        current = first.read(3)
        with pytest.raises(ConflictError):
            second.delete(original.identity, original.revision)
        first.delete(appended.identity, appended.revision)
        first.delete(current.identity, current.revision)
        with pytest.raises(ConflictError):
            first.read(3)
    assert tuple(SquishReader().read(base)) == ()
    assert Path(str(base) + ".SQL").read_bytes() == b"lastread untouched"
    raw = Path(str(base) + ".SQD").read_bytes()
    assert struct.unpack_from("<II", raw, 4) == (0, 0)
    assert struct.unpack_from("<ii", raw, 104) == (0, 0)
    assert struct.unpack_from("<H", raw, 256 + 24)[0] == 1


@pytest.mark.parametrize("body", ["short", "long" * 1000])
def test_content_moves_frame_preserves_metadata_and_replies(
    tmp_path: Path, body: str
) -> None:
    base = fixture(tmp_path)
    before = Path(str(base) + ".SQD").read_bytes()
    with SquishWriter().open(base) as session:
        original = session.read(3)
        result = session.update(
            original.identity, MessagePatch(body_text=body), original.revision
        )
        message = session.read(3)
        assert result.identity == original.identity
        assert result.revision.location[0] == len(before)
        assert message.message.body_text == body
    after = Path(str(base) + ".SQD").read_bytes()
    start = len(before) + 28
    assert after[start : start + 238] == before[284:522]
    assert after[start + 178 : start + 214] == struct.pack("<9I", *range(10, 19))
    assert b"\x01X-UNKNOWN: opaque\0" in after[start + 238 :]
    assert struct.unpack_from("<H", after, 280)[0] == 1
    assert Path(str(base) + ".SQL").read_bytes() == b"lastread untouched"


def test_attribute_only_preserves_raw_text_and_unknown_metadata(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    before = Path(str(base) + ".SQD").read_bytes()
    with SquishWriter().open(base) as session:
        source = session.read(3)
        result = session.update(
            source.identity, MessagePatch(attributes_raw=0xE0020000), source.revision
        )
        assert result.revision.location == source.revision.location
    after = Path(str(base) + ".SQD").read_bytes()
    assert after[288:] == before[288:]
    assert struct.unpack_from("<I", after, 284)[0] == 0xE0020000


def test_append_fields_controls_routes_and_uid(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = SquishWriter()
    writer.create(base)
    with writer.open(base) as session:
        result = session.append(
            outgoing(
                external_id="2:236/0 abc",
                from_address=FtnAddress(zone=2, net=236, node=0, point=5),
                posted_at=datetime(2026, 10, 4, 12, 34, 56, tzinfo=UTC),
                reply_list=tuple(range(1, 10)),
                routing_seen_by=("236/0 10",),
                routing_path=("236/0",),
                control_lines=(ControlLine(name="PID", value="writer-test", raw=""),),
            )
        )
        assert result.identity.msgno == 2
        message = session.read(2).message
        assert message.external_id == "2:236/0 abc"
        assert message.reply1st_msgno == 1
        assert message.from_address == "2:236/0.5"
        assert message.posted_at == datetime(2026, 10, 4, 12, 34, 56, tzinfo=UTC)
        assert message.control_lines is not None
        assert message.control_lines.seen_by == ("236/0 10",)
        assert message.control_lines.path == ("236/0",)
    data = Path(str(base) + ".SQD").read_bytes()
    assert struct.unpack_from("<9I", data, 284 + 178) == tuple(range(1, 10))
    # GoldED's ASCII case-insensitive to-name hash, independently calculated.
    assert (
        struct.unpack_from("<iII", Path(str(base) + ".SQI").read_bytes())[2] == 0x6952
    )


@pytest.mark.parametrize(
    "message",
    [
        outgoing(from_name="A" * 36),
        outgoing(body_text="😀"),
        outgoing(attributes_raw=-1),
        outgoing(reply_list=tuple(range(10))),
        outgoing(from_address=FtnAddress(zone=65536, net=0, node=0)),
        outgoing(posted_at=datetime(1979, 1, 1, tzinfo=UTC)),
        outgoing(control_lines=(ControlLine(name="CHRS", value="UTF-8 4", raw=""),)),
    ],
)
def test_invalid_input_leaves_base_unchanged(
    tmp_path: Path, message: OutgoingMessage
) -> None:
    base = fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with (
        SquishWriter().open(base) as session,
        pytest.raises((ValueError, UnicodeError)),
    ):
        session.append(message)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_none_and_omitted_patch(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    with SquishWriter().open(base) as session:
        source = session.read(3)
        with pytest.raises(ValueError, match="cannot be cleared"):
            session.update(source.identity, MessagePatch(subject=None), source.revision)
        updated = session.update(
            source.identity,
            MessagePatch(reply_list=None, posted_at=None),
            source.revision,
        )
        current = session.read(3)
        assert current.message.subject == "Hello"
        assert current.message.reply1st_msgno is None
        assert current.message.posted_at is None
        assert updated.identity == source.identity


@pytest.mark.parametrize(
    "offset,fmt,values",
    [
        (104, "<i", (0,)),
        (256 + 4, "<i", (256,)),
        (120, "<i", (999,)),
        (20, "<I", (3,)),
        (112, "<i", (256,)),
    ],
)
def test_reject_corruption_before_mutation(
    tmp_path: Path, offset: int, fmt: str, values: tuple[int, ...]
) -> None:
    base = fixture(tmp_path)
    path = Path(str(base) + ".SQD")
    patch(path, offset, fmt, *values)
    before = path.read_bytes()
    with SquishWriter().open(base) as session, pytest.raises(ParserException):
        session.append(outgoing())
    assert path.read_bytes() == before


class FailOnceIO(IO):
    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.steps = 0

    def _step(self) -> None:
        self.steps += 1
        if self.steps == self.fail_at:
            raise OSError("injected")

    def write(self, fd: int, offset: int, data: bytes) -> None:
        self._step()
        super().write(fd, offset, data)

    def truncate(self, fd: int, size: int) -> None:
        self._step()
        super().truncate(fd, size)

    def flush(self, fd: int) -> None:
        self._step()
        super().flush(fd)


@pytest.mark.parametrize("fail_at", range(1, 9))
def test_rollback_each_append_step(tmp_path: Path, fail_at: int) -> None:
    base = fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    io = FailOnceIO(fail_at)
    with SquishWriter(io=io).open(base) as session:
        try:
            session.append(outgoing())
        except OSError:
            assert all(path.read_bytes() == data for path, data in before.items())
            assert session.read(3).message.subject == "Hello"
        else:
            assert fail_at > io.steps


def test_failed_rollback_poison_session(tmp_path: Path) -> None:
    class BrokenIO(IO):
        def write(self, fd: int, offset: int, data: bytes) -> None:
            raise OSError("always fails")

    base = fixture(tmp_path)
    with SquishWriter(io=BrokenIO()).open(base) as session:
        with pytest.raises(RollbackError, match="append.*rollback failed"):
            session.append(outgoing())
        with pytest.raises(WriterError, match="unusable"):
            session.read(3)


def test_lock_timeout_controlled_holder(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    ready, release = threading.Event(), threading.Event()

    def hold() -> None:
        with locks.acquire(Path(str(base) + ".SQD")):
            ready.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(5)
    try:
        with (
            SquishWriter().open(base, WriterOptions(lock_timeout=0.02)) as session,
            pytest.raises(LockTimeoutError),
        ):
            session.read(3)
    finally:
        release.set()
        thread.join(5)


def test_concurrent_rejected(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    with pytest.raises(UnsupportedOperationError):
        SquishWriter().open(base, WriterOptions(concurrent=True))


def test_two_processes_lock_timeout_and_mutation(tmp_path: Path) -> None:
    import subprocess
    import sys

    base = fixture(tmp_path)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from golded_ftn._writer_io import locks; "
            "with_lock=locks.acquire(sys.argv[1]); with_lock.__enter__(); "
            "print('locked',flush=True); sys.stdin.readline(); "
            "with_lock.__exit__(None,None,None)",
            str(base) + ".SQD",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with (
            SquishWriter().open(base, WriterOptions(lock_timeout=0.02)) as session,
            pytest.raises(LockTimeoutError),
        ):
            session.append(outgoing())
    finally:
        holder.communicate("release\n", timeout=5)
    assert holder.returncode == 0
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from golded_ftn import OutgoingMessage; "
            "from golded_ftn_squish import SquishWriter; "
            "s=SquishWriter().open(sys.argv[1]); "
            "s.append(OutgoingMessage(from_name='Process',to_name='Bob',"
            "subject='Second',body_text='body')); s.close()",
            str(base),
        ],
        check=True,
    )
    with SquishWriter().open(base) as session:
        assert session.read(100).message.from_name == "Process"


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("fail_at", range(1, 13))
def test_rollback_update_delete_boundaries(
    tmp_path: Path, operation: str, fail_at: int
) -> None:
    base = fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    io = FailOnceIO(fail_at)
    with SquishWriter(io=io).open(base) as session:
        source = session.read(3)
        try:
            if operation == "update":
                session.update(
                    source.identity,
                    MessagePatch(body_text="new text" * 100),
                    source.revision,
                )
            else:
                session.delete(source.identity, source.revision)
        except OSError:
            assert all(path.read_bytes() == content for path, content in before.items())
            assert session.read(3).revision == source.revision
        else:
            assert fail_at > io.steps


def test_utf8_options_and_external_id_clear(tmp_path: Path) -> None:
    base = fixture(tmp_path)
    with SquishWriter().open(base, WriterOptions(target_charset="UTF-8")) as session:
        result = session.append(outgoing(body_text="你好", external_id="original"))
        assert session.read(result.identity.msgno).message.body_text == "你好"
        updated = session.update(
            result.identity, MessagePatch(external_id=None), result.revision
        )
        current = session.read(updated.identity.msgno).message
        assert current.control_lines is not None
        assert current.control_lines.msgid is None
        assert current.body_text == "你好"


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL probe is POSIX-specific")
def test_kill_after_frame_write_documents_partial_output(tmp_path: Path) -> None:
    import subprocess
    import sys

    base = fixture(tmp_path)
    path = Path(str(base) + ".SQD")
    index = Path(str(base) + ".SQI")
    before, before_index = path.read_bytes(), index.read_bytes()
    script = """
import os, signal, sys
from golded_ftn import OutgoingMessage
from golded_ftn._writer_io import IO
from golded_ftn_squish import SquishWriter
class KillAfterWrite(IO):
    def write(self, fd, offset, data):
        super().write(fd, offset, data)
        os.kill(os.getpid(), signal.SIGKILL)
with SquishWriter(io=KillAfterWrite()).open(sys.argv[1]) as session:
    session.append(OutgoingMessage(
        from_name='A', to_name='B', subject='C', body_text='D'))
"""
    killed = subprocess.run([sys.executable, "-c", script, str(base)], check=False)
    assert killed.returncode == -9
    after = path.read_bytes()
    assert after[: len(before)] == before
    assert len(after) > len(before)
    assert index.read_bytes() == before_index
    assert [message.msgno for message in SquishReader().read(base)] == [3]
    with (
        SquishWriter().open(base) as session,
        pytest.raises(ParserException, match="End frame"),
    ):
        session.read(3)


def test_body_patch_preserves_omitted_routing_bytes(tmp_path: Path) -> None:
    base = area(
        tmp_path, frames=((3, frame(body=b"old\rSEEN-BY: 1/2  3\r\x01PATH: 1/4\0")),)
    )
    patch(Path(str(base) + ".SQD"), 104, "<ii", 256, 256)
    with SquishWriter().open(base) as session:
        source = session.read(3)
        changed = session.update(
            source.identity, MessagePatch(body_text="new"), source.revision
        )
        raw = Path(str(base) + ".SQD").read_bytes()
        assert (
            b"new\rSEEN-BY: 1/2  3\r\x01PATH: 1/4\0"
            in raw[changed.revision.location[0] :]
        )


@pytest.mark.parametrize("first", [None, 0, 9])
def test_conflicting_explicit_reply_fields_rejected(
    tmp_path: Path, first: int | None
) -> None:
    base = fixture(tmp_path)
    before = Path(str(base) + ".SQD").read_bytes()
    with SquishWriter().open(base) as session:
        source = session.read(3)
        with pytest.raises(ValueError, match="conflicts"):
            session.update(
                source.identity,
                MessagePatch(reply_list=(5, 6), reply1st_msgno=first),
                source.revision,
            )
    assert Path(str(base) + ".SQD").read_bytes() == before


def test_control_patch_preserves_omitted_external_id(tmp_path: Path) -> None:
    base = area(
        tmp_path, frames=((3, frame(control=b"\x01MSGID: 2:1/2 old\x01PID: old\0")),)
    )
    patch(Path(str(base) + ".SQD"), 104, "<ii", 256, 256)
    with SquishWriter().open(base) as session:
        source = session.read(3)
        result = session.update(
            source.identity, MessagePatch(control_lines=()), source.revision
        )
        current = session.read(3)
        assert current.message.external_id == "2:1/2 old"
        assert (
            b"\x01MSGID: 2:1/2 old"
            in Path(str(base) + ".SQD").read_bytes()[result.revision.location[0] :]
        )
        before = Path(str(base) + ".SQD").read_bytes()
        with pytest.raises(ValueError, match="MSGID.*conflicts"):
            session.update(
                current.identity,
                MessagePatch(
                    control_lines=(
                        ControlLine(name="MSGID", value="different", raw=""),
                    )
                ),
                current.revision,
            )
        assert Path(str(base) + ".SQD").read_bytes() == before
