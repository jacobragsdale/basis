"""What an API repo carries from the installed basis-api: its block in `AGENTS.md`, its line of the Dockerfile, the
command that serves the API from the repo's image, and its `openapi.json`, which basis-api makes from the repo's
service. `basis sync` writes them with basis's own files, and `basis check` checks them, so a route that changes
without its document fails the commit hook until `basis sync` writes it again."""

import logging
from pathlib import Path

from basis.api.service import load
from basis.api.version import VERSION
from basis.core.errors import BasisError
from basis.core.sync import AgentFiles, SyncError
from basis.core.tools import Tools

_PACKAGE = Path(__file__).parent
DOCUMENT = "openapi.json"
"""Where an API repo commits its OpenAPI document: at its root, as `basis.api.openapi.FILE`."""

IMAGE = "#   an API: serve, and relay on a schedule where its databases declare an outbox\n"
"""The Dockerfile's line for the API: the command its container runs, as the cluster's deployment gives it, and the
command a scheduler gives the container that makes the calls its outboxes still hold."""

log = logging.getLogger(__name__)


def agent_files() -> AgentFiles:
    block = (_PACKAGE / "templates" / "agents.md").read_text(encoding="utf-8").replace("{version}", VERSION)
    return AgentFiles("basis-api", block, {}, image=IMAGE, made={DOCUMENT: document}, tools=TOOLS)


TOOLS = Tools(testpaths=("tests", "*/tests"))
"""basis-api's share of a repo's tool settings: an API's tests are in its package's `tests/`, by layer."""


def document() -> str:
    """The repo's `openapi.json`: the document of the one service its `basis.apis` entry point lists. A service that
    does not load, or a document FastAPI cannot make of it, is a `SyncError`, the file's one line, so every other check
    still runs."""
    from basis.api import openapi  # noqa: PLC0415 -- FastAPI loads when the document is made, not for every `basis --help`

    fix = "fix it, then run `uv run basis sync`, which writes openapi.json"
    try:
        service = load()
    except BasisError as error:
        msg = f"basis cannot make {DOCUMENT}, since {error}"
        raise SyncError(msg, "then run `uv run basis sync`") from error
    except Exception as error:  # the repo's own code, whatever it raises as it loads, which `basis serve` would show in full
        log.debug("the service did not load", exc_info=True)
        msg = f"basis cannot make {DOCUMENT}, since the service does not load: {type(error).__name__}: {' '.join(str(error).split())}"
        raise SyncError(msg, fix) from error
    try:
        return openapi.text(service)
    except Exception as error:  # a model FastAPI or pydantic cannot describe, which is the repo's to change
        log.debug("the document was not made", exc_info=True)
        msg = f"basis cannot make {DOCUMENT} of service {service.name}: {type(error).__name__}: {' '.join(str(error).split())}"
        raise SyncError(msg, fix) from error
