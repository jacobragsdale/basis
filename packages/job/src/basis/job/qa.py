"""basis-job's runs in the QA tier, which `basis check --full` makes once `basis.core.qa` has found the QA settings
safe: each job the repo lists, run against QA with `--publish`, saving its snapshot in the QA store and labelled
`purpose=qa`. The repo declares each job's run in `[tool.basis.qa.job]`, with what `basis run` takes:

    [tool.basis.qa.job]
    positions = { params = "jobs/positions/params/default.json", interval = "2026-11-05..2026-11-06" }
    trades = { params = "jobs/trades/params/default.json", interval = "2026-11-05..2026-11-06", again = true }

`params` is relative to the repo; `interval` is left out for a job that runs without one; `again` publishes an
`Append()` table for an interval the ledger already has, which a second QA run of the same interval needs. Nothing is
assumed: a job the repo lists and does not declare is refused, and so is one that reads a database the QA settings do
not configure, or writes a table `[tool.basis.qa] writes` does not list. While any job is refused, no job runs."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.model import Model
from basis.core.snapshots import RunRecord
from basis.core.sync import Problem
from basis.core.verbs import Qa, QaPlan, QaRun
from basis.job import job as jobs
from basis.job import runs
from basis.job.inputs import Query

log = logging.getLogger(__name__)

LABEL = {"purpose": "qa"}
_TABLE = "[tool.basis.qa.job]"


class Declared(Model):
    """One job's QA run, as `[tool.basis.qa.job]` declares it."""

    params: str
    interval: str | None = None
    again: bool = False


@dataclass(frozen=True)
class _Planned:
    job: jobs.Job
    request: runs.Request


def plan(qa: Qa, *, listed: Callable[[], Mapping[str, str]] = jobs.listed, load: Callable[[str], jobs.Job] = jobs.load) -> QaPlan:
    """Each job's QA run, and every reason one would be unsafe or could not run: of each job `listed` gives, by name,
    as `load` loads it."""
    problems: list[Problem] = []
    declared: dict[str, Declared] = {}
    for name, value in sorted(qa.declared.items()):
        try:
            declared[name] = Declared.model_validate(value)
        except ValidationError as error:
            wrong = "; ".join(f"{'.'.join(map(str, problem['loc'])) or 'it'}: {problem['msg']}" for problem in error.errors(include_url=False, include_input=False))
            problems.append((f"{_TABLE} {name} is not valid: {wrong}", f'give it as {name} = {{ params = "jobs/{name}/params/default.json", interval = "START..END" }}'))
    names = sorted(listed())
    others = f"remove it, or give one of {', '.join(names) or 'the jobs pyproject.toml lists'}"
    problems += [(f"{_TABLE} declares a QA run of {name}, which the repo does not list", others) for name in sorted(set(declared) - set(names))]
    planned: list[_Planned] = []
    for name in names:
        if name not in declared:
            if name not in qa.declared:
                fix = f'add {name} = {{ params = "jobs/{name}/params/default.json", interval = "START..END" }} to {_TABLE}'
                problems.append((f"the QA tier runs each job, and {_TABLE} declares no run of {name}", fix))
            continue
        found, refused = _planned(qa, name, declared[name], load)
        problems += refused
        if found is not None:
            planned.append(found)

    def run() -> Sequence[QaRun]:
        return [_run(qa, entry) for entry in planned]

    return QaPlan(problems, run)


def _planned(qa: Qa, name: str, declared: Declared, load: Callable[[str], jobs.Job]) -> tuple[_Planned | None, list[Problem]]:
    """The job's run, if it can be made, and every reason it would be unsafe or could not run."""
    try:
        job = load(name)
    except BasisError as error:
        return None, [(f"the QA run of {name} cannot load the job: {error}", "fix the job, as `uv run basis check` says")]
    problems: list[Problem] = []
    params = qa.repo / declared.params
    try:
        runs.read_params(job, params)
    except (OSError, ValidationError) as error:
        problems.append((f"{_TABLE} {name}'s params, {declared.params}, cannot be read: {' '.join(str(error).split())}", "give a params file, relative to the repo, that the job's Params accept"))
    try:
        interval = Interval.parse(declared.interval) if declared.interval is not None else None
    except ValueError as error:
        problems.append((f"{_TABLE} {name}'s interval is not valid: {error}", "give START..END, as 2026-11-05..2026-11-06"))
        interval = None
    configured = qa.settings.databases
    unread = sorted({dataset.database for dataset in job.inputs.declared().values() if isinstance(dataset, Query) and dataset.database not in configured})
    problems += [(f"{name} reads database {database!r}, which the QA settings do not configure", f"set BASIS_DATABASES__{database.upper()}__* in them, to QA's") for database in unread]
    for output, table in job.outputs.declared().items():
        target = table.target
        if target.database not in configured:
            problems.append((f"{name} writes {output} to database {target.database!r}, which the QA settings do not configure", f"set BASIS_DATABASES__{target.database.upper()}__* to QA's"))
        elif configured[target.database].write is None:
            problems.append((f"{name} writes {output} to database {target.database!r}, which the QA settings give no write block", f"set BASIS_DATABASES__{target.database.upper()}__WRITE__* to QA's"))
        if not qa.may_write(target.database, target.table):
            reason = f"{name} writes {output} to {target.table} in {target.database}, which [tool.basis.qa] writes does not list, so the QA tier will not run it"
            problems.append((reason, f"add {target.table} to writes.{target.database} if it is QA's table"))
    request = runs.Request(params, interval, labels=dict(LABEL), publish=True, again=declared.again, git_sha=qa.commit)
    return (_Planned(job, request) if not problems else None), problems


def _run(qa: Qa, planned: _Planned) -> QaRun:
    job = planned.job
    try:
        result = runs.run(job, qa.settings, planned.request, snapshot_root=qa.store, databases=qa.databases)
    except Exception as error:
        log.exception("the QA run of %s failed", job.name)
        return QaRun(job.name, None, "did not run", (f"the QA run of {job.name} failed: {type(error).__name__}: {' '.join(str(error).split())}", "fix it, and check again"))
    record = result.record
    return QaRun(job.name, record.run_id, summary(record), runs.failure_of(record, publish=True))


def summary(record: RunRecord) -> str:
    """What a run did, in a few words: `published: daily_positions (3 inserted, 3 deleted), account_activity (2 upserted)`."""
    if record.status != "published":
        return record.status
    tables = {name: output.table for name, output in record.outputs.items()}
    counted: Mapping[str, dict[str, int]] = {name: counts for publish in record.publishes.values() for name, counts in publish.counts.items()}
    pieces = []
    for name, counts in sorted(counted.items()):
        done = ", ".join(f"{count} {operation.replace('_', ' ')}" for operation, count in counts.items() if count)
        pieces.append(f"{tables.get(name, name)} ({done or 'no rows'})")
    return f"published: {', '.join(pieces) or 'no changes'}"
