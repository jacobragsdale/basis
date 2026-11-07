"""The agent files a job repo carries from the installed basis-job: its block in `AGENTS.md` and the `etl-*` skills in
`.agents/skills/`, which `etl sync` rewrites and each repo's `tests/test_agent_files.py` checks."""

from pathlib import Path

from basis.core import sync as core_sync
from basis.core.sync import AgentFiles
from basis.job.version import VERSION

_PACKAGE = Path(__file__).parent


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-job", "etl", block, core_sync.skills(_PACKAGE / "skills"), formerly=("etl-framework",))


def stale(repo: Path) -> list[Path]:
    """The agent files that do not match the installed basis-job."""
    return core_sync.stale(repo, agent_files())


def write(repo: Path) -> list[Path]:
    """Rewrite every stale agent file, and return them."""
    return core_sync.write(repo, agent_files())
