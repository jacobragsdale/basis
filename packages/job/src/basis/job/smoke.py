"""`basis smoke --env ENV`, basis-job's side: a dry run of each job the repo declares for it, on that environment's
inputs, for its most recent days, running its transform and its checks and publishing nothing. A repo declares them,
explicitly, in `[tool.basis.job.smoke]`, each by its name:

    [tool.basis.job.smoke]
    positions = { params = "jobs/positions/params/default.json", days = 1 }

`days` is how many days the interval holds, ending today in UTC. A run that fails, or that its checks refuse, is one
`ERROR` line, with its fix; a repo that declares none has nothing to dry-run, which passes. The run's snapshot goes to a
temporary folder, removed after, so a dry run leaves nothing in the environment's run store."""

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from basis.core import environments
from basis.core.interval import Interval
from basis.core.settings import Settings
from basis.core.storage import Root
from basis.core.sync import Problem
from basis.core.verbs import table
from basis.job import job as jobs
from basis.job import runs

KEY = "smoke"
_FIELDS = frozenset({"params", "days"})


def smoke(repo: Path, env: str) -> list[Problem]:
    """Each declared job's dry run on environment `env` that did not check out, with its fix."""
    declared = table(repo, "job").get(KEY)
    if not declared:
        return []
    if not isinstance(declared, dict):
        return [("[tool.basis.job.smoke] is not a table", 'give each job to dry-run as `NAME = { params = "jobs/NAME/params/default.json", days = 1 }`')]
    environment = environments.load(env, Settings)
    found: list[Problem] = []
    for name, spec in declared.items():
        read = _spec(str(name), spec)
        found += _dry_run(repo, environment, str(name), read) if isinstance(read, Declared) else [read]
    return found


@dataclass(frozen=True)
class Declared:
    """A job's dry run, as `[tool.basis.job.smoke]` declares it."""

    days: int
    params: str


def _spec(name: str, spec: object) -> Declared | Problem:
    """A job's days and params, or why `spec` gives none."""
    fix = f'give it as `{name} = {{ params = "jobs/{name}/params/default.json", days = 1 }}`'
    given: dict[str, object] = {str(key): value for key, value in spec.items()} if isinstance(spec, dict) else {}
    days, params = given.get("days"), given.get("params")
    if set(given) != _FIELDS or isinstance(days, bool) or not isinstance(days, int) or days < 1 or not isinstance(params, str):
        return (f"[tool.basis.job.smoke] {name} is not a job's params, a path, and its days, a whole number of days, 1 or more", fix)
    return Declared(days, params)


def _dry_run(repo: Path, environment: environments.Environment[Settings], name: str, spec: Declared) -> list[Problem]:
    days, params = spec.days, spec.params
    job = jobs.load(name)
    today = datetime.now(UTC).date()
    request = runs.Request(repo / params, Interval.parse(f"{today - timedelta(days=days)}..{today}"), labels={"smoke": environment.name})
    with tempfile.TemporaryDirectory(prefix="basis-smoke-") as folder:
        result = runs.run(job, environment.settings, request, snapshot_root=Root.at(folder), environment=environment.name)
    found = runs.failure_of(result.record, publish=False)
    return [found] if found is not None else []
