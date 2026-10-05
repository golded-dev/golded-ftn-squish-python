import subprocess
import sys
from pathlib import Path

import golded_ftn_squish
from tests._fixtures import area, frame


def test_runtime_exports() -> None:
    assert golded_ftn_squish.__all__ == [
        "SquishReader",
        "SquishSession",
        "SquishWriter",
    ]
    assert (Path(golded_ftn_squish.__file__).parent / "py.typed").is_file()


def test_readme_example(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    example = (
        (root / "README.md").read_text().split("```python\n", 1)[1].split("```", 1)[0]
    )
    directory = tmp_path / "messages"
    directory.mkdir()
    base = area(directory, frames=((3, frame()),))
    for suffix in (".SQD", ".SQI"):
        Path(str(base) + suffix).rename(directory / ("general" + suffix))
    result = subprocess.run(
        [sys.executable, "-c", example],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "3 Alice Hello"
