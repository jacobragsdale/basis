"""The snapshot of a run, under the snapshot root:

    JOB/RUN_ID/
      run.json                   the record: the request, each input's source, each output's changes, every reason
                                 not to write, and each database's publish
      params.json                the params, as the run read them
      inputs/NAME.parquet        each input as it was loaded
      outputs/NAME/OPERATION.parquet   each table's frames: upsert, insert_new, insert and delete_matching

It is written whole, under a hidden name renamed into place, before anything is written to a database, so a run that
fails leaves what it read for a replay. After publishing, `run.json` is rewritten with each database's outcome. Inputs
are stored as loaded, never derived frames: a replay runs the transform again on them, with the code of the day."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import polars as pl
from pydantic import Field

from etl_framework.interval import Interval
from etl_framework.outputs import Changes, Delete, Range
from etl_framework.params import Model
from etl_framework.storage import Root

type Status = Literal["failed", "checked", "published", "partial"]
"""`failed`: an error, or a reason not to write; `checked`: every check passed, and the run was not asked to publish;
`published`: every database committed; `partial`: some committed and one failed, which `etl resume` finishes."""


class InputRecord(Model):
    source: str
    """Where it was read: `DATABASE: SQL_FILE`, `file: PATH`, or `data: PATH` for a run on files."""
    rows: int
    read_ms: float


class DeleteRecord(Model):
    column: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    matching: bool = False
    """Whether `outputs/NAME/delete_matching.parquet` holds the values it matches."""


class OutputRecord(Model):
    database: str
    table: str
    write: str
    delete: DeleteRecord | None = None
    rows: dict[str, int] = Field(default_factory=dict)
    """Rows by operation: upsert, insert_new, insert, delete_matching."""


class PublishRecord(Model):
    status: Literal["committed", "failed"]
    at: datetime
    counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    """What the database counted, by output and operation."""
    error: str | None = None


class RunRecord(Model):
    run_id: str
    job: str
    module: str
    version: str
    """The etl-framework version the run ran with."""
    git_sha: str | None = None
    """The job repo's commit, as the image's `ETL_GIT_SHA` gives it."""
    started_at: datetime
    finished_at: datetime | None = None
    interval: str | None = None
    """`START..END`, or None for a run without one."""
    params_path: str
    params_sha256: str
    labels: dict[str, str] = Field(default_factory=dict)
    data: str | None = None
    """The folder a run on files read its inputs from."""
    replay_of: str | None = None
    status: Status
    inputs: dict[str, InputRecord] = Field(default_factory=dict)
    outputs: dict[str, OutputRecord] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    """Every reason the run may not write, from the destinations, the outputs and the job's own check."""
    error: str | None = None
    publishes: dict[str, PublishRecord] = Field(default_factory=dict)

    def parsed_interval(self) -> Interval | None:
        return Interval.parse(self.interval) if self.interval is not None else None


def new_run_id() -> str:
    """A UUIDv7: run ids sort by when their runs started."""
    return str(uuid.uuid7())


@dataclass(frozen=True)
class Snapshot:
    root: Root
    """The run's folder."""

    def record(self) -> RunRecord:
        return RunRecord.model_validate_json((self.root / "run.json").read_bytes())

    def params_json(self) -> str:
        return (self.root / "params.json").read_bytes().decode()

    def inputs(self) -> dict[str, pl.DataFrame]:
        return {child.name.removesuffix(".parquet"): child.read_frame() for child in (self.root / "inputs").children() if child.name.endswith(".parquet")}

    def changes(self) -> dict[str, Changes]:
        """Each table's changes, as the run computed them."""
        return {name: self._changes(name, output) for name, output in self.record().outputs.items() if output.rows or output.delete is not None}

    def _changes(self, name: str, output: OutputRecord) -> Changes:
        folder = self.root / "outputs" / name

        def frame(operation: str) -> pl.DataFrame | None:
            return (folder / f"{operation}.parquet").read_frame() if operation in output.rows else None

        delete = None
        if (record := output.delete) is not None:
            within = Range(record.column, record.start, record.end) if record.column is not None and record.start is not None and record.end is not None else None
            delete = Delete(within, frame("delete_matching") if record.matching else None)
        return Changes(delete, frame("upsert"), frame("insert_new"), frame("insert"))

    def save_record(self, record: RunRecord) -> None:
        (self.root / "run.json").write_bytes(record.model_dump_json(indent=2).encode())


def write(root: Root, record: RunRecord, params_json: str, inputs: dict[str, pl.DataFrame], changes: dict[str, Changes]) -> Snapshot:
    """Write a run's snapshot whole, and return it."""
    final = root / record.job / record.run_id
    staging = root / record.job / f".{record.run_id}.tmp"
    staging.make_dir()
    try:
        (staging / "params.json").write_bytes(params_json.encode())
        for name, frame in inputs.items():
            (staging / "inputs" / f"{name}.parquet").write_frame(frame)
        for name, table in changes.items():
            for operation, frame in table.frames().items():
                (staging / "outputs" / name / f"{operation}.parquet").write_frame(frame)
        (staging / "run.json").write_bytes(record.model_dump_json(indent=2).encode())
    except BaseException:
        if staging.exists():
            staging.delete_dir()
        raise
    staging.move_to(final)
    return Snapshot(final)


def output_record(database: str, table: str, write_mode: str, changes: Changes | None) -> OutputRecord:
    if changes is None:
        return OutputRecord(database=database, table=table, write=write_mode)
    delete = None
    if changes.delete is not None:
        within = changes.delete.within
        delete = DeleteRecord(
            column=within.column if within is not None else None,
            start=within.start if within is not None else None,
            end=within.end if within is not None else None,
            matching=changes.delete.matching is not None,
        )
    return OutputRecord(database=database, table=table, write=write_mode, delete=delete, rows={name: frame.height for name, frame in changes.frames().items()})


def find(root: Root, run_id: str, job: str | None = None) -> Snapshot:
    """A run's snapshot by its id, under `root/JOB/`, for any job unless `job` is given."""
    folders = [root / job] if job is not None else root.children()
    for folder in folders:
        if (candidate := folder / run_id).is_dir() and (candidate / "run.json").exists():
            return Snapshot(candidate)
    msg = f"no run {run_id} under {root.location}: is it the right snapshot root?"
    raise LookupError(msg)


def runs_of(root: Root, job: str) -> list[Snapshot]:
    """A job's runs, newest first."""
    folder = root / job
    found = [Snapshot(child) for child in folder.children() if not child.name.startswith(".") and (child / "run.json").exists()]
    return sorted(found, key=lambda snapshot: snapshot.root.name, reverse=True)


def params_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
