"""What a Dash app's repo carries from the installed basis-dash: its block in `AGENTS.md`, the `dash-*` skills in
`.agents/skills/` and `.claude/skills/`, its line of the Dockerfile, the command that serves the app from the repo's
image, and its share of the repo's tool settings: `bd`, the one name `basis.dash` is imported as, and Dash's own pytest
plugin off, which drives a browser. `basis sync` writes them with basis's own files, and `basis check` checks them."""

from pathlib import Path

from basis.core import sync as core_sync
from basis.core.sync import AgentFiles
from basis.core.tools import Tools
from basis.dash.version import VERSION

_PACKAGE = Path(__file__).parent

IMAGE = "#   a Dash app: serve\n"
"""The Dockerfile's line for the app: the command its container runs, as the cluster's deployment gives it."""

TOOLS = Tools(testpaths=("tests", "*/tests"), aliases={"basis.dash": "bd"}, plugins_off=("dash",))
"""basis-dash's share of a repo's tool settings: an app's tests are in its package's `tests/`; `basis.dash` is imported
as `bd`, so it never collides with `dash` itself; and Dash's pytest plugin, `dash`, which drives a browser with
Selenium and whose hooks fail pytest 9's warnings, stays off."""


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-dash", block, core_sync.skills(_PACKAGE / "skills"), image=IMAGE, tools=TOOLS)
