"""`run` and `replay`, as a job's `main.py` calls them, so an IDE runs and debugs a job by running a file:

    from basis.job import run

    from jobs.positions import job

    if __name__ == "__main__":
        result = run(job, params="params/default.json", interval="2026-11-05..2026-11-06", env="dev")

Paths are the job folder's, wherever the IDE runs the file from. `env` names the environment whose settings the call
reads, as `basis run --env` does: the user's file for it, such as `~/.config/basis/dev.env`, alone, or in a pod the
process's environment (`basis.core.environments`). There is no default environment: without `env`, the call reaches
nothing, so every dataset comes from `data`. A run from Python saves its snapshot in the repo's `snapshots/`, and never
publishes: only `basis run --publish` writes to a database."""

import logging
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from pydantic import AwareDatetime

from basis.core import environments, fakes, logs
from basis.core import runs as core_runs
from basis.core.environments import Env
from basis.core.interval import Interval
from basis.core.model import Model
from basis.core.settings import GivenSettings, Settings
from basis.core.storage import Root
from basis.job import handles, runs
from basis.job import job as jobs
from basis.job.diff import OperationDiff

log = logging.getLogger(__name__)


def run(  # noqa: PLR0913 -- main.py writes every value out, by keyword
    job: ModuleType,
    *,
    params: str | Path,
    interval: str | None = None,
    env: Env | None = None,
    data: str | Path | None = None,
    labels: Mapping[str, str] | None = None,
    snapshot_root: str | Path | None = None,
) -> runs.Result:
    """Run a job without publishing, in `env`, or with no settings at all. `data` names a folder of files, such as
    `tests/fixture`, that replace the datasets they name: a run on a fixture, whose clocks read its `fixture.json`'s
    time and whose ids count up from 1, whatever its interval, and, with no `interval`, whose interval is the
    fixture's."""
    found = jobs.read(job)
    _setup_logs(found)
    folder = data if data is None else found.directory / data
    faked: dict[str, fakes.Fake] = {}
    if folder is not None:
        fixture = read_fixture(folder) if interval is None or (folder / FIXTURE).exists() else None
        if interval is None and fixture is not None:
            interval = fixture.interval
        faked = fixture_fakes(found, fixture, folder)
    request = runs.Request(found.directory / params, Interval.parse(interval) if interval is not None else None, folder, dict(labels or {}))
    root = Root.at(found.directory / snapshot_root) if snapshot_root is not None else Root.at(jobs.repo_root(found.directory) / "snapshots")
    settings, environment = _settings(env)
    result = runs.run(found, settings, request, snapshot_root=root, faked=faked, environment=environment)
    _report(result)
    return result


def replay(job: ModuleType, run_id: str, *, params: str | Path | None = None, env: Env | None = None, snapshot_root: str | Path | None = None) -> tuple[runs.Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code you have now, and with `params` if given. The run
    is found in the repo's `snapshots/`, or `snapshot_root` if given, then under the `BASIS_SNAPSHOT_ROOT` of `env`'s
    settings, such as prod's; the replay is saved in the first of those."""
    found = jobs.read(job)
    _setup_logs(found)
    local = Root.at(found.directory / snapshot_root) if snapshot_root is not None else Root.at(jobs.repo_root(found.directory) / "snapshots")
    roots = [local]
    configured, environment = _settings(env)
    if configured.snapshot_root is not None:
        roots.append(Root.at(configured.snapshot_root))
    source = core_runs.find(roots, run_id, found.name)
    result, diffs = runs.replay(found, source, params=found.directory / params if params is not None else None, snapshot_root=local, tracing=configured.traces, environment=environment)
    _report(result)
    for entry in diffs:
        log.info("differs", extra=vars(entry))
    return result, diffs


class Fixture(Model):
    interval: str | None
    """`START..END`, or null for a job that runs without one."""
    now: AwareDatetime | None = None
    """The time each clock the job's `Inputs` declare reads, which a job that declares one must give."""


def fixture_interval(folder: Path) -> str | None:
    """A fixture's interval, from its `fixture.json`: `{"interval": "2026-11-05..2026-11-06"}`, or null for none."""
    return read_fixture(folder).interval


FIXTURE = "fixture.json"


def read_fixture(folder: Path) -> Fixture:
    """A fixture's `fixture.json`: its interval, and the time its run's clocks read, as
    `{"interval": "2026-11-05..2026-11-06", "now": "2026-11-06T06:00:00+00:00"}`."""
    path = folder / FIXTURE
    if not path.exists():
        msg = f'{folder} has no fixture.json, which gives the fixture\'s interval: write {{"interval": "2026-11-05..2026-11-06"}}, or null'
        raise FileNotFoundError(msg)
    return Fixture.model_validate_json(path.read_bytes())


def fixture_fakes(job: jobs.Job, fixture: Fixture | None, folder: Path) -> dict[str, fakes.Fake]:
    """What a run on the fixture in `folder` gives its clocks and ids: the time its `fixture.json` gives, and ids that
    count up from 1."""
    fix = f'add "now": "2026-11-06T06:00:00+00:00" to {folder / FIXTURE}'
    return handles.faked(job.inputs, fixture.now if fixture is not None else None, fix=fix)


def _settings(env: Env | None) -> tuple[Settings, Env | None]:
    """The settings of `env`, as `--env` reads them, or none at all, never the process's environment, without one."""
    if env is None:
        return GivenSettings(), None
    environment = environments.load(env, Settings)
    return environment.settings, environment.name


def _setup_logs(job: jobs.Job) -> None:
    if not logging.getLogger().handlers:
        logs.setup(default_format="text")
    logs.include(job.module.partition(".")[0])


def _report(result: runs.Result) -> None:
    record = result.record
    log.info("%s %s: %s", record.job, record.run_id, record.status)
    for reason in record.reasons:
        log.warning("  %s", reason)
    if record.error is not None:
        log.error("  %s", record.error)
