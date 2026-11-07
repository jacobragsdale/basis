"""A run as the run store keeps it (see `basis.core.storage.Store`): a manifest at `runs/PACKAGE/JOB/RUN_ID.json`,
which holds the run's record, the params as the run read them, and the hash of each dataset it saved:

    inputs/NAME                   each input as it was loaded
    outputs/NAME/OPERATION        each table's frames: upsert, insert_new, insert and delete_matching

Each dataset is an object the store holds once, however many runs save it: a replay in the same store stores only
the outputs that changed. A run is saved before anything is written to a database, its objects first and its manifest
last, so a run that fails leaves what it read for a replay. After publishing, the manifest is rewritten with each
database's outcome; its params and datasets never change. Inputs are stored as loaded, never derived frames: a replay
runs the transform again on them, with the code of the day."""

import hashlib
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

import polars as pl
from pydantic import Field

from basis.core.interval import Interval
from basis.core.model import Model
from basis.core.storage import DIGEST, Root, Store
from basis.core.tables import Changes, Delete, Range

type Status = Literal["failed", "checked", "published", "partial"]
"""`failed`: an error, or a reason not to write; `checked`: every check passed, and the run was not asked to publish;
`published`: every database committed; `partial`: some committed and one failed, which `basis resume` finishes."""


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
    """Whether the dataset `outputs/NAME/delete_matching` holds the values it matches."""


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
    """The version of the basis part that made the run, such as basis-job's."""
    git_sha: str | None = None
    """The job repo's commit, as the image's `BASIS_GIT_SHA` gives it."""
    started_at: datetime
    finished_at: datetime | None = None
    interval: str | None = None
    """`START..END`, or None for a run without one."""
    params_path: str
    """The params file the run read, or `run RUN_ID` for a replay with the params of the run it replays."""
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


class Manifest(RunRecord):
    """A run's record as the store keeps it, with what a replay or a resume reads besides."""

    params: str
    """The params, as the run read them."""
    datasets: dict[str, Annotated[str, Field(pattern=f"^{DIGEST.pattern}$")]]
    """Each dataset the run saved, `inputs/NAME` and `outputs/NAME/OPERATION`, by its object's hash."""


@dataclass(frozen=True)
class Snapshot:
    """A saved run, its manifest read once."""

    store: Store
    path: Root
    """Where its manifest is."""
    manifest: Manifest

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    def record(self) -> RunRecord:
        return RunRecord.model_validate(self.manifest.model_dump(include=set(RunRecord.model_fields)))

    def params_json(self) -> str:
        return self.manifest.params

    def inputs(self) -> dict[str, pl.DataFrame]:
        return {key.removeprefix("inputs/"): self._dataset(key) for key in self.manifest.datasets if key.startswith("inputs/")}

    def changes(self) -> dict[str, Changes]:
        """Each table's changes, as the run computed them."""
        return {name: self._changes(name, output) for name, output in self.manifest.outputs.items() if output.rows or output.delete is not None}

    def _changes(self, name: str, output: OutputRecord) -> Changes:
        def frame(operation: str) -> pl.DataFrame | None:
            return self._dataset(f"outputs/{name}/{operation}") if operation in output.rows else None

        delete = None
        if (record := output.delete) is not None:
            within = Range(record.column, record.start, record.end) if record.column is not None and record.start is not None and record.end is not None else None
            delete = Delete(within, frame("delete_matching") if record.matching else None)
        return Changes(delete, frame("upsert"), frame("insert_new"), frame("insert"))

    def _dataset(self, key: str) -> pl.DataFrame:
        if (found := self.manifest.datasets.get(key)) is None:
            msg = f"run {self.run_id}'s manifest {self.path.location} names no dataset {key}, which its record says it saved"
            raise LookupError(msg)
        return self.store.object(found).read_frame()

    def save_record(self, record: RunRecord) -> Snapshot:
        """Save the run's record again, as after publishing, with the params and datasets it was saved with, which
        never change; and return the run as saved."""
        if record.run_id != self.run_id:
            msg = f"the record of run {record.run_id} cannot replace run {self.run_id}'s"
            raise ValueError(msg)
        manifest = _manifest(record, self.manifest.params, self.manifest.datasets)
        self.path.write_bytes(manifest.model_dump_json(indent=2).encode())
        return Snapshot(self.store, self.path, manifest)


def _manifest(record: RunRecord, params_json: str, datasets: Mapping[str, str]) -> Manifest:
    return Manifest.model_validate(record.model_dump() | {"params": params_json, "datasets": dict(datasets)})


def _read(store: Store, path: Root) -> Snapshot:
    try:
        manifest = Manifest.model_validate_json(path.read_bytes())
    except ValueError as error:
        error.add_note(f"while reading the manifest {path.location}")
        raise
    return Snapshot(store, path, manifest)


def write(root: Root, record: RunRecord, params_json: str, inputs: Mapping[str, pl.DataFrame], changes: Mapping[str, Changes], *, package: str) -> Snapshot:  # noqa: PLR0913 -- the run, and the part whose run it is, by keyword
    """Save a new run in the store at `root`, under the part `package`: each dataset the store does not hold yet, then
    the run's manifest. A run id the store holds already is refused: only `Snapshot.save_record` saves a run again."""
    store = Store(root)
    path = store.manifest(package, record.job, record.run_id)
    if path.exists():
        msg = f"run {record.run_id} is in the store already, at {path.location}: a new run takes a new id"
        raise FileExistsError(msg)
    frames = {f"inputs/{name}": frame for name, frame in inputs.items()}
    frames |= {f"outputs/{name}/{operation}": frame for name, table in changes.items() for operation, frame in table.frames().items()}
    manifest = _manifest(record, params_json, {key: store.put(frame.to_arrow()) for key, frame in frames.items()})
    path.write_bytes(manifest.model_dump_json(indent=2).encode())
    return Snapshot(store, path, manifest)


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


def find(root: Root, run_id: str, name: str | None = None) -> Snapshot:
    """A run by its id, in the store at `root`, of any part and name unless `name` is given."""
    store = Store(root)
    return _read(store, store.find(run_id, name))


def runs_of(root: Root, name: str, *, package: str) -> Iterator[Snapshot]:
    """The runs of `package`'s `name`, newest first, each manifest read only when its run is reached."""
    store = Store(root)
    return (_read(store, path) for path in store.manifests(package, name))


def params_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
