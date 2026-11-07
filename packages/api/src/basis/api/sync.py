"""What an API repo carries from the installed basis-api: its block in `AGENTS.md`, and its line of the Dockerfile, the
command that serves the API from the repo's image. `basis sync` writes them with basis's own files, and `basis check`
checks them."""

from pathlib import Path

from basis.api.version import VERSION
from basis.core.sync import AgentFiles

_PACKAGE = Path(__file__).parent

IMAGE = "#   an API: serve, and relay on a schedule where its databases declare an outbox\n"
"""The Dockerfile's line for the API: the command its container runs, as the cluster's deployment gives it, and the
command a scheduler gives the container that makes the calls its outboxes still hold."""


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-api", block, {}, image=IMAGE)
