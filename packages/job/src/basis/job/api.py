"""`run` and `replay`, as a job's `main.py` calls them, so an IDE runs and debugs a job by running a file:

    from basis.job import run

    from jobs.positions import job

    if __name__ == "__main__":
        result = run(job, params="params/default.json", interval="2026-11-05..2026-11-06", env=".env")

Paths are the job folder's, wherever the IDE runs the file from. `env` names a settings file, found in the job's
folder or a folder above it up to the repo's root. Its variables are set for this call alone and removed after it, so
a call that reads `.env.qa` and one that reads `.env.prod` never mix, and the process's own environment wins over
the file, as `uv run --env-file` does. A run from Python saves its snapshot in the repo's `snapshots/`, and never
publishes: only `basis run --publish` writes to a database."""

import logging
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from dotenv import dotenv_values

from basis.core import logs
from basis.core import runs as core_runs
from basis.core.interval import Interval
from basis.core.model import Model
from basis.core.settings import Settings
from basis.core.storage import Root
from basis.job import job as jobs
from basis.job import runs
from basis.job.diff import OperationDiff

log = logging.getLogger(__name__)


def run(  # noqa: PLR0913 -- main.py writes every value out, by keyword
    job: ModuleType,
    *,
    params: str | Path,
    interval: str | None = None,
    env: str | Path | None = ".env",
    data: str | Path | None = None,
    labels: Mapping[str, str] | None = None,
    snapshot_root: str | Path | None = None,
) -> runs.Result:
    """Run a job without publishing. `data` names a folder of files, such as `tests/fixture`, that replace the datasets
    they name; with it and no `interval`, the fixture's own interval applies."""
    found = jobs.read(job)
    _setup_logs(found)
    folder = data if data is None else found.directory / data
    if interval is None and folder is not None:
        interval = fixture_interval(folder)
    request = runs.Request(found.directory / params, Interval.parse(interval) if interval is not None else None, folder, dict(labels or {}))
    root = Root.at(found.directory / snapshot_root) if snapshot_root is not None else Root.at(jobs.repo_root(found.directory) / "snapshots")
    with _environment(_settings_file(found.directory, env)):
        result = runs.run(found, Settings(), request, snapshot_root=root)
    _report(result)
    return result


def replay(job: ModuleType, run_id: str, *, params: str | Path | None = None, env: str | Path | None = None, snapshot_root: str | Path | None = None) -> tuple[runs.Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code you have now, and with `params` if given. The run
    is found in the repo's `snapshots/`, or `snapshot_root` if given, then under the `BASIS_SNAPSHOT_ROOT` the settings
    file names, such as production's in `.env.prod`; the replay is saved in the first of those."""
    found = jobs.read(job)
    _setup_logs(found)
    local = Root.at(found.directory / snapshot_root) if snapshot_root is not None else Root.at(jobs.repo_root(found.directory) / "snapshots")
    roots = [local]
    with _environment(_settings_file(found.directory, env)):
        configured = Settings().snapshot_root
    if configured is not None:
        roots.append(Root.at(configured))
    source = core_runs.find(roots, run_id, found.name)
    result, diffs = runs.replay(found, source, params=found.directory / params if params is not None else None, snapshot_root=local)
    _report(result)
    for entry in diffs:
        log.info("differs", extra=vars(entry))
    return result, diffs


class Fixture(Model):
    interval: str | None
    """`START..END`, or null for a job that runs without one."""


def fixture_interval(folder: Path) -> str | None:
    """A fixture's interval, from its `fixture.json`: `{"interval": "2026-11-05..2026-11-06"}`, or null for none."""
    path = folder / "fixture.json"
    if not path.exists():
        msg = f'{folder} has no fixture.json, which gives the fixture\'s interval: write {{"interval": "2026-11-05..2026-11-06"}}, or null'
        raise FileNotFoundError(msg)
    return Fixture.model_validate_json(path.read_bytes()).interval


def _settings_file(directory: Path, env: str | Path | None) -> Path | None:
    """The settings file `env` names, in the job's folder or one above it up to the repo's root."""
    if env is None:
        return None
    for folder in (directory, *directory.parents):
        if (candidate := folder / env).exists():
            return candidate
        if (folder / "pyproject.toml").exists():
            break
    log.info("no settings file %s: reading the process's environment alone; copy .env.example to %s to read databases", env, env)
    return None


@contextmanager
def _environment(path: Path | None) -> Generator[None]:
    """The settings file's variables, set for the block alone; a variable the process already has keeps its value."""
    added = {key: value for key, value in (dotenv_values(path) if path is not None else {}).items() if value is not None and key not in os.environ}
    os.environ.update(added)
    try:
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)


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
