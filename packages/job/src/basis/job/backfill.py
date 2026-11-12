"""`basis backfill JOB --from DATE --to DATE`: a job's runs for a span of days, as a scheduler would have made them,
one run per business day of the job's `CALENDAR`, each for the day's interval, `DAY..DAY+1` in UTC, as
`--interval previous-business-day` gives one, in order, oldest first.

It stops at the first run that does not do what it was asked, and says which, so a backfill is never left with a gap
behind its last day. Run again with the same days, it carries on from there, reading the run store: a day whose
newest run did what it was asked, `published` with `--publish` or `checked` without, is skipped; a day whose newest
run stopped while publishing, `partial` or `publishing`, is resumed from its snapshot, as `basis resume` would,
rather than run again, so nothing is written twice; any other day is run. Each run carries the label
`backfill=FROM..TO`, beside the backfill's own `--label`s."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from basis.core import snapshots
from basis.core.calendars import Calendar
from basis.core.databases import Database
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.settings import Settings
from basis.core.snapshots import Snapshot
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job

_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Step:
    """What the backfill did for one day."""

    day: date
    did: Literal["skipped", "ran", "resumed"]
    run_id: str
    status: str
    labels: Mapping[str, str]


def days(calendar: Calendar, first: date, last: date) -> list[date]:
    """The business days of `calendar` from `first` to `last`, both included, in order."""
    found: list[date] = []
    day = first
    while day <= last:
        if calendar.is_business_day(day):
            found.append(day)
        day += _DAY
    return found


def interval(day: date) -> Interval:
    """The interval of the run for `day`: its midnight to the next, in UTC."""
    return Interval(datetime.combine(day, time(), UTC), datetime.combine(day + _DAY, time(), UTC))


def backfill(
    job: Job,
    settings: Settings,
    *,
    params: Path,
    first: date,
    last: date,
    publish: bool,
    snapshot_root: Root,
    environment: str | None = None,
    labels: Mapping[str, str] | None = None,
    data: Path | None = None,
    databases: Callable[[str], Database] | None = None,
) -> tuple[list[Step], tuple[str, str] | None]:
    """Each day's step, as the module's docstring says, and why the backfill stopped, with the fix, or None where every
    day did what it was asked. `data` and `databases` are a test's."""
    if first > last:
        msg = f"--from {first} is after --to {last}: give the first day, then the last"
        raise BasisError(msg)
    if job.calendar is None:
        msg = f"{job.name} declares no calendar, which a backfill's days are its business days of: declare one in the job's module, as CALENDAR = etl.calendar(\"sifma-us\")"
        raise BasisError(msg)
    wanted = days(job.calendar, first, last)
    if not wanted:
        msg = f"no business day of {job.name}'s calendar falls from {first} to {last}: give days that hold one"
        raise BasisError(msg)
    given = {**(labels or {}), "backfill": f"{first}..{last}"}
    newest = _newest(snapshot_root, job.name, {str(interval(day)) for day in wanted})
    steps: list[Step] = []
    for day in wanted:
        found = newest.get(str(interval(day)))
        record = found.record() if found is not None else None
        if record is not None and runs.failure_of(record, publish=publish) is None:
            steps.append(Step(day, "skipped", record.run_id, record.status, record.labels))
            continue
        if publish and found is not None and record is not None and record.status in {"partial", "publishing"}:
            record = runs.resume(job, found, settings, environment=environment, databases=databases)
            steps.append(Step(day, "resumed", record.run_id, record.status, record.labels))
        else:
            request = runs.Request(params, interval(day), data, given, publish=publish)
            record = runs.run(job, settings, request, snapshot_root=snapshot_root, environment=environment, databases=databases).record
            steps.append(Step(day, "ran", record.run_id, record.status, record.labels))
        if (failure := runs.failure_of(record, publish=publish)) is not None:
            reason, fix = failure
            return steps, (f"the backfill of {job.name} stopped at {day}: {reason} ({fix})", f"fix what it says, then run the same backfill again, which carries on from {day}")
    return steps, None


def _newest(root: Root, job: str, wanted: set[str]) -> dict[str, Snapshot]:
    """The newest run of `job` in the store for each interval in `wanted` that has one, replays left out."""
    found: dict[str, Snapshot] = {}
    for snapshot in snapshots.runs_of(root, job, package=runs.PACKAGE):
        record = snapshot.record()
        given = record.interval
        if record.replay_of is None and given is not None and given in wanted and given not in found:
            found[given] = snapshot
        if len(found) == len(wanted):
            break
    return found
