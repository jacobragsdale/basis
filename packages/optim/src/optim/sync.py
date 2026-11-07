"""What a model repo carries from the installed optim: its block in `AGENTS.md`, the contract every model keeps; the
`optim-*` skills, in `.agents/skills/` and their copies in `.claude/skills/`; and its line of the Dockerfile.
`basis sync` writes them with basis's own files, and `basis check` fails when one differs from what the installed optim
writes, so an upgrade brings its guidance with it."""

from pathlib import Path

import optim
from basis.core import sync as core_sync
from basis.core.sync import AgentFiles

PACKAGE = Path(__file__).parent
SKILLS = PACKAGE / "skills"
IMAGE = "#   a model: run MODEL --params models/MODEL/params/default.json --as-of DATE --publish\n"
"""The Dockerfile's line for models: the command a scheduler gives a model's container, which writes the accepted
orders only with `--publish`."""


def agent_files() -> AgentFiles:
    """optim's block, stamped with the installed version, so an upgrade makes it stale, its skills, and its line of the
    Dockerfile."""
    block = (PACKAGE / "templates" / "contract.md").read_text(encoding="utf-8").replace("{version}", optim.__version__)
    return AgentFiles("optim", block, core_sync.skills(SKILLS), image=IMAGE)


def stale(repo: Path) -> list[Path]:
    """The files basis and optim write that do not match the installed versions."""
    return core_sync.stale(repo, [agent_files()])


def write(repo: Path) -> list[Path]:
    """Rewrite every stale file basis and optim write, and return them."""
    return core_sync.write(repo, [agent_files()]).changed
