"""The check each part holds its packaged words to (step 9.1), and core's own agent block held to it: a name the
module lacks, a verb or an option the command lacks, and a command line its parser refuses each fail, naming the
file."""

from pathlib import Path

import pytest

from basis.core import command, documented, tables

BLOCK = Path(command.__file__).parent / "templates" / "sync" / "agents.md"


@pytest.fixture
def repo(tmp_path: Path) -> command.Repo:
    return command.Repo(tmp_path / "pyproject.toml", {})


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "page.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_cores_agent_block_gives_only_real_commands(tmp_path: Path, repo: command.Repo) -> None:
    assert documented.problems([BLOCK], modules={}, repo=repo, scratch=tmp_path) == []


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Declare it with `tables.Precision(10, 2)`.", "tables.Precision is not in basis.core.tables"),
        ("Run `uv run basis frobnicate`.", "basis has no verb 'frobnicate'"),
        ("Run `basis check --fast` before a commit.", "basis check takes no option --fast"),
        ("```sh\nuv run basis review --commit\n```", "expected one argument"),
        ("A synopsis: `basis check [--staged] [--quick]`.", "basis check takes no option --quick"),
    ],
    ids=["a made-up name", "a made-up verb", "a made-up option", "a line its parser refuses", "a synopsis's made-up option"],
)
def test_a_name_or_a_command_that_is_not_real_fails(tmp_path: Path, repo: command.Repo, text: str, said: str) -> None:
    page = written(tmp_path, text)
    found = documented.problems([page], modules={"tables": tables}, repo=repo, scratch=tmp_path)
    assert len(found) == 1
    assert found[0].startswith(f"{page}: ")
    assert said in found[0]


def test_real_names_synopses_shell_and_tables_pass(tmp_path: Path, repo: command.Repo) -> None:
    text = """`tables.Column` and `tables.Changes`; the ledger `tables.publishes`; a file `core/tables.py`.

```sh
uv run basis check --staged 2>/dev/null   # a comment
uv run basis review --commit HEAD | head
```

`basis check --full [--settings-from DIR] [--no-qa]`, and `basis --help`.
"""
    assert documented.problems([written(tmp_path, text)], modules={"tables": tables}, repo=repo, scratch=tmp_path, tables=("publishes",)) == []
