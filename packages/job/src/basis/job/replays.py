"""Replay and regress: a saved run's transform run again on what it read, with the code of the day, and how its
changes differ from the run's. Neither reads a database nor writes one; each replay is one trace, as a run is."""

from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from basis.core import logs, snapshots, traces
from basis.core import runs as core_runs
from basis.core.logs import utc_now
from basis.core.runs import Regression
from basis.core.settings import Traces
from basis.core.snapshots import InputRecord, MessageRecord, RunRecord, Snapshot
from basis.core.storage import Root
from basis.job import effects, handles
from basis.job.diff import OperationDiff, diff, diff_frames
from basis.job.job import Job
from basis.job.params import Params
from basis.job.runs import PACKAGE, Draft, Result, Transformed, finish, targets_of, transformed
from basis.job.version import VERSION


def replay(
    job: Job,
    source: Snapshot,
    *,
    params: Path | None = None,
    snapshot_root: Root | None = None,
    clock: Callable[[], datetime] = utc_now,
    tracing: Traces | None = None,
    provider: TracerProvider | None = None,
    environment: str | None = None,
) -> tuple[Result, list[OperationDiff]]:
    """Run a saved run's transform again on its inputs, with the code of the day and, if given, other params, and how
    its changes differ from the run's: one trace, exported where `tracing` says, or on a test's `provider`. Reads no
    database and writes none, and gives the transform the times and ids the run kept."""
    with (
        logs.fields(job=job.name, service=job.name, replay_of=source.run_id),  # the replay's own, whatever an earlier run in the process bound, until it ends
        traces.trace("replay", service=job.name, settings=tracing, version=None, environment=environment, given=provider, **{"basis.job": job.name, "basis.replay_of": source.run_id}) as (tracer, _),
    ):
        return _replay(job, source, params=params, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replay(job: Job, source: Snapshot, *, params: Path | None, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:
    with core_runs.replaying(source, name=job.name, module=job.module, params=params, parse=job.params.model_validate_json, reads=job.inputs.declared()) as opened:
        return _replayed(job, source, opened, snapshot_root=snapshot_root, clock=clock, tracer=tracer)


def _replayed(job: Job, source: Snapshot, opened: core_runs.Replay[Params], *, snapshot_root: Root | None, clock: Callable[[], datetime], tracer: Tracer) -> tuple[Result, list[OperationDiff]]:
    original = opened.original
    interval = original.parsed_interval()
    record = Draft(
        run_id=opened.run_id, job=job.name, module=job.module, version=VERSION, git_sha=None, started_at=clock(), interval=original.interval,
        params_path=opened.params_path, params_sha256=snapshots.params_digest(opened.params_text),
        labels={"replay_of": original.run_id}, data=None, replay_of=original.run_id, trace_id=logs.trace_id(),
        inputs={  # a watermark as the run recorded it: the replay reads what the run read
            name: InputRecord(source=f"snapshot: {original.run_id}", rows=table.num_rows, read_ms=0.0, watermark=entry.watermark if (entry := original.inputs.get(name)) is not None else None)
            for name, table in opened.saved.items()
        },
    )  # fmt: skip
    tables = {name: opened.saved[name] for name in job.inputs.declared()}
    kept = handles.replayed(job.inputs, original, tracer)
    made = transformed(job, opened.params, tables, interval, kept)
    planned, reasons = made.planned, made.reasons
    record = record.with_outputs(planned, record.inputs).with_made(kept).with_effects(made.effects_planned.records)
    result = finish(record.done("failed" if reasons else "checked", reasons=reasons, finished_at=clock()), snapshot_root, opened.params_text, tables, planned, reports=made.effects_planned.frames)
    return result, _differences(job, source, original, made, result)


def _differences(job: Job, source: Snapshot, original: RunRecord, made: Transformed, result: Result) -> list[OperationDiff]:
    """How a replay's outputs differ from its run's: each table's changes, the rows of each workbook, upsert and
    archive, and each message's text."""
    before = source.changes()
    targets = targets_of(job)
    diffs = [found for name in sorted(set(before) | set(result.planned)) if name in targets for found in diff(name, targets[name], before.get(name), result.changes(name))]
    saved = {name for kind in effects.KINDS if kind.rows for name in kind.of(original)[0]}  # each saved as its rows, as resume reads them
    frames = made.effects_planned.frames
    for name in sorted(saved | set(frames)):
        old = source.dataset(f"outputs/{name}/insert") if name in saved else None
        new = frames.get(name)
        if not (found := diff_frames(name, "rows", old, new, None)).same:
            diffs.append(found)
    messages = {name: record for name, record in made.effects_planned.records[effects.Messages.planned].items() if isinstance(record, MessageRecord)}
    for name in sorted(set(original.messages) | set(messages)):
        old_message, new_message = original.messages.get(name), messages.get(name)
        if old_message is None or new_message is None:
            diffs.append(OperationDiff(name, "message", added=int(old_message is None), removed=int(new_message is None), changed=0))
        elif (old_message.sha256, old_message.subject) != (new_message.sha256, new_message.subject):
            diffs.append(OperationDiff(name, "message", added=0, removed=0, changed=1))
    return diffs


def regress(job: Job, root: Root, *, last: int, labels: Mapping[str, str], statuses: Collection[str] = ("published",)) -> list[Regression[OperationDiff]]:
    """Replay a job's last `last` runs of `statuses`, published by default, whose labels include `labels`, with the
    code of the day, and how each one's changes differ. Writes nothing."""

    def replayed(snapshot: Snapshot) -> tuple[RunRecord, list[OperationDiff]]:
        result, diffs = replay(job, snapshot)
        return result.record, diffs

    return core_runs.regress(root, job.name, package=PACKAGE, last=last, labels=labels, replay=replayed, statuses=statuses)
