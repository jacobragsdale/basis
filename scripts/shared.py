"""What this repo's scripts share, so that they cannot drift apart. Each script imports it from its own folder, which
Python puts first on `sys.path`. Like them it has no dependencies, so the commit hook runs each one with no sync, and
none can import basis-core, whose `basis.core.upgrade.normalized` `normalized` matches."""

import os
import re
from collections.abc import Mapping

UNSET = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_FROZEN", "UV_LOCKED", "UV_EXCLUDE_NEWER", "PYTHONPATH")
"""Variables that would point a uv command a script runs at the environment `uv run` made for the script, at a lock or
an environment other than the workspace's, or at another moment of the index."""


def environment(variables: Mapping[str, str] = os.environ) -> dict[str, str]:
    """`variables` without `UNSET` and git's own (`GIT_*`): a hook sets `GIT_DIR`, which would point git at this
    clone from another folder."""
    return {name: value for name, value in variables.items() if name not in UNSET and not name.startswith("GIT_")}


def normalized(name: str) -> str:
    """A distribution's name as PEP 503 normalizes it, and as uv.lock spells it: lower case, with each run of `-`, `_`
    and `.` one `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()
