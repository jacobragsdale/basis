"""What basis-dash's docs, skills and agent block teach agents is what the code does: every `bd.NAME` they give exists,
and every `basis` command line they give parses with the real parser."""

from pathlib import Path

import basis.dash as bd
from basis.core import command, documented
from basis.dash.cli import part

ROOT = Path(bd.__file__).parent
WORDS = [*sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "skills").glob("*/SKILL.md")), ROOT / "templates" / "agents.md", ROOT / "templates" / "repo" / "README.md"]


def test_every_name_and_command_the_dash_words_give_is_real(tmp_path: Path) -> None:
    repo = command.Repo(tmp_path / "pyproject.toml", {"dash": part()})
    assert documented.problems(WORDS, modules={"bd": bd}, repo=repo, scratch=tmp_path) == []
