"""optim's guidance for agents in this repo, `AGENTS.md`'s optim block and the `optim-*` skills in `.agents/skills/`,
matches the installed optim: `uv run optim sync` rewrites them after an upgrade."""

from pathlib import Path

from optim.testing import check_agent_files


def test_the_agent_files_match_the_installed_optim() -> None:
    check_agent_files(Path(__file__).parents[1])
