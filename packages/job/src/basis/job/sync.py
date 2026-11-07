"""The agent files a job repo carries from the installed basis-job: its block in `AGENTS.md` and the `job-*` skills in
`.agents/skills/`, which `basis sync` rewrites and `basis check` checks."""

from pathlib import Path

from basis.core import sync as core_sync
from basis.core.sync import AgentFiles
from basis.job.version import VERSION

_PACKAGE = Path(__file__).parent


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-job", block, core_sync.skills(_PACKAGE / "skills"), formerly=("etl-framework",), retired=RETIRED)


RETIRED = (".agents/skills/etl-new-job", ".agents/skills/etl-port-job", ".agents/skills/etl-why-failed", "tests/test_agent_files.py")
"""What basis-job wrote before step 1.5, without a marker: its skills under their `etl-` names, and the test that
compared the agent files with the installed version, which `basis check` does now."""


def stale(repo: Path) -> list[Path]:
    """The agent files that do not match the installed basis-job."""
    return core_sync.stale(repo, [agent_files()])


def write(repo: Path) -> list[Path]:
    """Rewrite every stale agent file, and return them."""
    return core_sync.write(repo, [agent_files()])
