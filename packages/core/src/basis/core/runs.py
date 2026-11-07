"""What is done with a saved run, whichever part made it: finding it, opening it to replay, finishing a publish that
failed partway (`resume`), and replaying recent published runs (`regress`). The part runs its own code, and these are
the steps around it that every part takes the same way."""

import itertools
import logging
import time
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

import pandas as pd

from basis.core import logs, publish, snapshots
from basis.core.databases import Database, open_database
from basis.core.settings import Settings
from basis.core.snapshots import PublishRecord, RunRecord, Snapshot, Status
from basis.core.storage import Root
from basis.core.tables import Target

log = logging.getLogger(__name__)


def opener(settings: Settings) -> Callable[[str], Database]:
    """Each database a run names, as the settings configure it, opened once however often the run asks."""

    @cache
    def opened(name: str) -> Database:
        return open_database(settings, name)

    return opened


def status_of(publishes: Mapping[str, PublishRecord], databases: int) -> Status:
    """A run's status from its publishes, of the `databases` it writes: `published` when every one committed, and
    `partial` when some did."""
    committed = sum(record.status == "committed" for record in publishes.values())
    if committed == databases:
        return "published"
    return "partial" if committed else "failed"


def find(roots: Sequence[Root], run_id: str, name: str) -> Snapshot:
    """A run of `name` by its id, from the first of `roots` that holds it."""
    for root in roots:
        try:
            return snapshots.find(root, run_id, name)
        except LookupError:
            continue
    msg = f"no run {run_id} of {name} under {', '.join(root.location for root in roots)}"
    raise LookupError(msg)


@dataclass(frozen=True)
class Replay[P]:
    """A saved run, opened to run again: its record, the params to run with, what it saved, and the replay's own id."""

    original: RunRecord
    run_id: str
    params: P
    params_text: str
    params_path: str
    """Where the params came from: the file given, or `run RUN_ID` for the run's own."""
    saved: dict[str, pd.DataFrame]
    """Every input the run saved, by name."""


@contextmanager
def replaying[P](source: Snapshot, *, name: str, module: str, params: Path | None, parse: Callable[[str], P], reads: Collection[str]) -> Generator[Replay[P]]:  # noqa: PLR0913 -- the part says what it is and reads, by keyword
    """Open `source`, a run of the part `name` (or of its `module`), to run it again with the run's params or `params`,
    which `parse` validates. `reads` names the inputs the part reads now, each of which the run must have saved. Every
    record logged in the block carries the replay's id, `name` and the run it replays, and none logged after it does."""
    original = source.record()
    if original.job not in {name, module}:
        msg = f"run {original.run_id} is a run of {original.job}, not {name}"
        raise ValueError(msg)
    params_text = params.read_text(encoding="utf-8") if params is not None else source.params_json()
    parsed = parse(params_text)
    run_id = snapshots.new_run_id()
    with logs.fields(run_id=run_id, job=name, replay_of=original.run_id):
        saved = source.inputs()
        if missing := sorted(set(reads) - set(saved)):
            msg = f"run {original.run_id} did not read {missing}, which the job reads now: a replay runs on what the run read"
            raise ValueError(msg)
        params_path = str(params) if params is not None else f"run {original.run_id}"
        yield Replay(original, run_id, parsed, params_text, params_path, saved)


def resume(  # noqa: PLR0913 -- the part says what it is and writes, by keyword
    source: Snapshot, record: RunRecord, *, name: str, targets: Mapping[str, Target], databases: Callable[[str], Database], version: str, clock: Callable[[], datetime], again: bool = False
) -> RunRecord:
    """Finish publishing `record`, a run of `name` that committed to some of its databases and failed on another, from
    its snapshot, and save its record. `targets` are where the part writes each output now, which must be where the
    run wrote it. It publishes a plain insert for an interval the ledger has already when the run was told to, or when
    `again` says so now."""
    if record.reasons or not record.publishes:
        msg = f"run {record.run_id} is {record.status} and never published: only a run that failed while publishing resumes; run it again instead"
        raise ValueError(msg)
    for output_name, output in record.outputs.items():
        now = (targets[output_name].database, targets[output_name].table) if output_name in targets else None
        if now != (output.database, output.table):
            msg = f"{output_name} is written to {now} now, and the run wrote ({output.database}, {output.table}): resume with the code the run ran"
            raise ValueError(msg)
    told = record.again or again
    publication = publish.Publication(record.run_id, name, record.parsed_interval(), version, told, clock)
    publishes = record.publishes | publish.publish(publication, targets, source.changes(), databases)
    status = status_of(publishes, len({output.database for output in record.outputs.values()}))
    final = RunRecord.model_validate(record.model_dump() | {"publishes": publishes, "status": status, "again": told})  # so that the next resume is told the same
    source.save_record(final)
    return final


@dataclass(frozen=True)
class Regression[D]:
    """One replayed run: how its outputs differ, and every reason the replay would not write them."""

    run_id: str
    diffs: list[D]
    reasons: list[str]


def regress[D](  # noqa: PLR0913 -- the part says what it is and how it replays, by keyword
    root: Root, name: str, *, package: str, last: int, labels: Mapping[str, str], replay: Callable[[Snapshot], tuple[RunRecord, list[D]]]
) -> list[Regression[D]]:
    """Replay the last `last` published runs of `package`'s `name` whose labels include `labels`, with `replay`, the
    part's own, and how each one's outputs differ. Writes nothing, and reads manifests only until it has `last`."""
    if last < 1:
        msg = f"regress replays the last N published runs, and N must be at least 1, not {last}"
        raise ValueError(msg)
    published = (snapshot for snapshot in snapshots.runs_of(root, name, package=package) if (record := snapshot.record()).status == "published" and labels.items() <= record.labels.items())
    chosen = list(itertools.islice(published, last))
    start = time.perf_counter()
    found: list[Regression[D]] = []
    for snapshot in chosen:
        record, diffs = replay(snapshot)
        found.append(Regression(snapshot.run_id, diffs, record.reasons))
    log.info("regressed", extra={"runs": len(found), "changed": sum(bool(entry.diffs or entry.reasons) for entry in found), "ms": logs.ms_since(start)})
    return found
