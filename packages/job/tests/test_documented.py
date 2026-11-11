"""What basis-job's docs, skills and agent block teach agents is what the code does (step 9.1): every `etl.NAME` they
give exists, every `basis` command line they give parses with the real parser, and the reference's table of column
types is `basis.core.frames`'s."""

import re
from pathlib import Path

import basis.job as etl
from basis.core import command, documented, frames
from basis.job.cli import part

ROOT = Path(etl.__file__).parent
WORDS = [*sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "skills").glob("*/SKILL.md")), ROOT / "templates" / "agents.md"]
ANNOTATIONS = {"float": "`float`", "str": "`str`, or `Annotated[str, etl.MaxLength(N)]`", "bool": "`bool`", "date": "`date`", "datetime": "`Annotated[datetime, etl.Zone(NAME)]`"}


def test_every_name_and_command_the_job_words_give_is_real(tmp_path: Path) -> None:
    repo = command.Repo(tmp_path / "pyproject.toml", {"job": part()})
    assert documented.problems(WORDS, modules={"etl": etl}, repo=repo, scratch=tmp_path, tables=("publishes",)) == []


def test_the_references_column_types_are_the_frames_own() -> None:
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip("|").split(" | ")] for line in reference.splitlines() if re.match(r"^\| `(float|str|bool|date|Annotated\[(datetime|str))", line)]
    cells = {row[0]: row[1] for row in rows}
    dtypes = {kind: "numpy's `float64`" if found == frames.pa.float64() else f"Arrow's `{found}`" for kind, found in frames._TYPES.items()}  # noqa: SLF001 -- the one table this doc must agree with
    assert {kind: cells.get(ANNOTATIONS[kind], "").strip() for kind in dtypes} == dtypes
    assert set(cells) - set(ANNOTATIONS.values()) == {"`Annotated[str, etl.Json()]`"}


def test_the_agent_block_names_every_kind_of_column() -> None:
    block = " ".join((ROOT / "templates" / "agents.md").read_text(encoding="utf-8").split())
    assert "A column is a `float`, `str`, `bool`, `date` or `datetime`." in block
    assert list(frames._TYPES) == ["float", "str", "bool", "date", "datetime"]  # noqa: SLF001 -- the kinds the sentence above lists
