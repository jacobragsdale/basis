"""What basis-api's docs, skills and agent block teach agents is what the code does (step 9.1): every `api.NAME` they
give exists, and every `basis` command line they give parses with the real parser."""

from pathlib import Path

from basis import api
from basis.api.cli import part
from basis.core import command, documented

ROOT = Path(api.__file__).parent
WORDS = [*sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "skills").glob("*/SKILL.md")), ROOT / "templates" / "agents.md"]


def test_every_name_and_command_the_api_words_give_is_real(tmp_path: Path) -> None:
    repo = command.Repo(tmp_path / "pyproject.toml", {"api": part()})
    assert documented.problems(WORDS, modules={"api": api}, repo=repo, scratch=tmp_path) == []
