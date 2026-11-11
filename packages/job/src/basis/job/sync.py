"""What a job repo carries from the installed basis-job: its block in `AGENTS.md`, the `job-*` skills in
`.agents/skills/` and `.claude/skills/`, and its lines of the Dockerfile, which `basis sync` rewrites with basis's own
files, and `basis check` checks."""

from pathlib import Path

from basis.core import sync as core_sync
from basis.core.sync import AgentFiles
from basis.core.tools import Tools
from basis.job.version import VERSION

_PACKAGE = Path(__file__).parent


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-job", block, core_sync.skills(_PACKAGE / "skills"), image=IMAGE, formerly=("etl-framework",), retired=RETIRED, tools=TOOLS)


TOOLS = Tools(testpaths=("tests", "jobs"), per_file_ignores={"**/job.py": ("ARG001",)})
"""basis-job's share of a repo's tool settings: a job's tests are in its folder of `jobs/`, and a job's hooks share
one signature, so a hook may leave an argument unused."""


IMAGE = "#   a job: run JOB --params jobs/JOB/params/default.json --interval START..END --publish\n"
"""The Dockerfile's line for jobs: the command a scheduler gives a job's container."""


RETIRED = (".agents/skills/etl-new-job", ".agents/skills/etl-port-job", ".agents/skills/etl-why-failed", "tests/test_agent_files.py")
"""What basis-job wrote before step 1.5, without a marker: its skills under their `etl-` names, and the test that
compared the agent files with the installed version, which `basis check` does now."""


def stale(repo: Path) -> list[Path]:
    """The agent files that do not match the installed basis-job."""
    return core_sync.stale(repo, [agent_files()])


def write(repo: Path) -> list[Path]:
    """Rewrite every stale file basis writes, and return them."""
    return core_sync.write(repo, [agent_files()]).changed
