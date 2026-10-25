"""Create uses configured Squish casing; open/read use the files on disk."""

from __future__ import annotations

from pathlib import Path

import pytest
from golded_ftn import OutgoingMessage

from golded_ftn_squish import SquishReader, SquishWriter, SquishWriterOptions


def _message() -> OutgoingMessage:
    return OutgoingMessage(
        from_name="Alice",
        to_name="All",
        subject="case",
        body_text="hello\r",
    )


def test_create_default_writes_uppercase(tmp_path: Path) -> None:
    base = tmp_path / "area"
    SquishWriter().create(base)
    assert (tmp_path / "area.SQD").is_file()
    assert not (tmp_path / "area.sqd").exists() or (tmp_path / "area.sqd").samefile(
        tmp_path / "area.SQD"
    )


def test_create_lower_writes_lowercase_and_open_finds_it(tmp_path: Path) -> None:
    base = tmp_path / "area"
    SquishWriter().create(base, SquishWriterOptions(extension_case="lower"))
    assert (tmp_path / "area.sqd").is_file()
    names = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert "area.SQD" not in names or (tmp_path / "area.SQD").samefile(
        tmp_path / "area.sqd"
    )
    with SquishWriter().open(base) as session:
        session.append(_message())
    subjects = [message.subject for message in SquishReader().read(base)]
    assert subjects == ["case"]


def test_create_rejects_other_casing_as_already_there(tmp_path: Path) -> None:
    base = tmp_path / "area"
    SquishWriter().create(base, SquishWriterOptions(extension_case="lower"))
    with pytest.raises(FileExistsError):
        SquishWriter().create(base, SquishWriterOptions(extension_case="upper"))
