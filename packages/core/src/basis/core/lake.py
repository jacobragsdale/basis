"""Azure Data Lake Storage Gen2, as a part archives a run's rows to it: Parquet through `pyarrow.fs.AzureFileSystem`,
which basis already depends on, one partition of a table per interval, `CONTAINER/TABLE/date=2026-11-07/`.

A lake is signed in to as its settings say (`LakeSettings.auth`): an account key or a SAS token, each a secret, or
Entra ID, through workload identity in a pod or `az login` on a desktop. Azure's default chain of credentials is never
used, since it asks for a managed identity before the CLI, so on a desktop that is an Azure VM it would sign in as the
VM. `docs/spikes/data-lake.md` has what each choice was checked against.

`archive` writes an interval's partition as a part that publishes does, in the transaction of its ledger's database,
as `basis.core.sending.deliver` sends a message:

1. A run for an interval another run has archived the output for archives nothing, unless told `again`.
2. The partition is written under a hidden name, `TABLE/.tmp-RUN_ID/PARTITION/part-N.parquet`, which a reader of the
   table skips, since a name that starts with `.` is no partition.
3. Each file is read back: its SHA-256, and its rows from its footer, which must add up to the rows written.
4. The ledger row is recorded: the interval, the path, the rows and each file's hash.
5. The partition is renamed into place, the one step a reader sees: with the hierarchical namespace a rename is one
   atomic server-side operation, and without it pyarrow refuses it, so a flat account fails loudly rather than
   copying. Told `again`, the old partition is first moved aside to `TABLE/.old-RUN_ID/`, so a reader may briefly
   find none, but never half of one.
6. The transaction commits.

A process that dies between the rename and the commit leaves a partition the ledger lacks. A retry finds it in place,
reads it back, and records it if it holds the rows the run would write, and refuses otherwise, naming the path."""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from opentelemetry.trace import SpanKind

from basis.core import ledgered, traces
from basis.core.databases import Counts, Database, LedgerEntry
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.settings import LakeSettings, Settings, SettingsError, variable
from basis.core.snapshots import ArchivedFile, ArchivedRecord

log = logging.getLogger(__name__)

type Partitioning = Literal["date", "interval"]
"""How a table's partitions are named: `date`, `date=2026-11-07`, for a run whose interval is one UTC day, midnight to
midnight; or `interval`, `start=20261101T000000Z/end=20261201T000000Z`, for any interval, a month's among them."""
FILE_ROWS = 1_000_000
"""The most rows one Parquet file of a partition holds."""
TABLE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
"""A table's folder in its container."""
_DAY = timedelta(days=1)


class LakeError(BasisError, ValueError):
    """A lake that cannot be archived to, or an archive that does not read back as it was written."""


@dataclass(frozen=True)
class Lake:
    """A lake's container, by its name, through `fs`: Azure's, or a test's local filesystem, where the container is a
    folder."""

    name: str
    container: str
    fs: pafs.FileSystem
    account: str | None = None
    """The storage account, which messages name; None for a test's."""

    def where(self, path: str) -> str:
        """`path` inside the container, as messages name it: never a URL, which could carry a token."""
        return f"{self.account}/{path}" if self.account is not None else path


def filesystem(found: LakeSettings) -> pafs.FileSystem:
    """The filesystem the settings sign in to, as the module's docstring says."""
    secret = found.secret.get_secret_value() if found.secret is not None else None
    endpoint: dict[str, str] = {}
    if found.emulator is not None:
        endpoint = {"blob_storage_authority": found.emulator, "dfs_storage_authority": found.emulator, "blob_storage_scheme": "http", "dfs_storage_scheme": "http"}
    match found.auth:
        case "key":
            return pafs.AzureFileSystem(found.account, account_key=secret, **endpoint)  # ty: ignore[possibly-missing-attribute] -- every wheel of the locked pyarrow has it (spike 0.8)
        case "sas":  # pyarrow appends a token without its `?` to the path, which Azure refuses (arrow#49972)
            return pafs.AzureFileSystem(found.account, sas_token="?" + (secret or "").removeprefix("?"), **endpoint)  # ty: ignore[possibly-missing-attribute] -- as above
        case "workload_identity" | "cli":  # reachable only through a URI (spike 0.8)
            fs, _ = pafs.FileSystem.from_uri(f"abfs://{found.container}@{found.account}.dfs.core.windows.net/?credential_kind={found.auth}")
            return fs


def lake(name: str, settings: Settings) -> Lake:
    """The lake the settings name `name`."""
    if (found := settings.lakes.get(name)) is None:
        names = ", ".join(variable(("lakes", name, part)) for part in ("account", "container", "auth"))
        msg = f"the lake {name} is not configured, and the run archives to it: set {names}, as .env.example shows"
        raise SettingsError(msg)
    return Lake(name, found.container, filesystem(found), found.account)


type Opener = Callable[[str], Lake]
"""Each lake a run names, as the settings configure it, or a test's."""


def opener(settings: Settings) -> Opener:
    return lambda name: lake(name, settings)


def partition(partitioning: Partitioning, interval: Interval) -> str:
    """The partition of a table that `interval`'s rows go to, as `Partitioning` says."""
    if partitioning == "interval":
        return f"start={interval.start:%Y%m%dT%H%M%SZ}/end={interval.end:%Y%m%dT%H%M%SZ}"
    day = interval.start.date()
    if interval.start != datetime.combine(day, time(), UTC) or interval.end - interval.start != _DAY:
        msg = f"the interval {interval} is not one UTC day, midnight to midnight, and the archive is partitioned by date: run it for one day, or declare partition='interval'"
        raise LakeError(msg)
    return f"date={day.isoformat()}"


Archiving = ledgered.Run
"""What a part's run says of the archive it writes: its interval is the partition's, so it has one."""


def archive(archiving: Archiving, output: str, rows: pa.Table, store: Lake, database: Database, *, table: str, path: str) -> ArchivedRecord:
    """Archive `rows`, the run's output `output`, as the partition `path` of the lake's `table`, recording it in
    `database`'s ledger (`basis.core.ledgered`), as the module's docstring says, and what became of it."""
    if (interval := archiving.interval) is None:
        msg = f"{output}: an archive is its run's interval's partition, and this run has none: give it one"
        raise LakeError(msg)
    base = f"{store.container}/{table}"
    final = f"{base}/{path}"
    temporary, old = f"{base}/.tmp-{archiving.run_id}", f"{base}/.old-{archiving.run_id}"
    _container(store)
    files: list[ArchivedFile] = []
    settle: list[Settle] = []
    began: list[bool] = []

    def entry() -> LedgerEntry:
        began.append(True)
        with _reaching(store, f"write {store.where(temporary)}"):
            written, settled = _settled(store, final, _write(store, f"{temporary}/{path}", rows), again=archiving.again)
        files.extend(written)
        settle.append(settled)
        detail = {"path": store.where(final), "rows": rows.num_rows, "files": [file.model_dump() for file in files]}
        return LedgerEntry(
            archiving.run_id, archiving.job, output, f"lake:{store.name}:{table}", interval, rerun_safe=False,
            counts=Counts(inserted=rows.num_rows), published_at=archiving.clock(), version=archiving.version, detail=json.dumps(detail),
        )  # fmt: skip

    def make() -> None:
        if settle[0] != "keep":
            with _reaching(store, f"rename the partition into {store.where(final)}"):
                _move(store, f"{temporary}/{path}", final, aside=f"{old}/{path}" if settle[0] == "replace" else None)

    def undo() -> None:
        if settle[0] == "replace":  # the ledger keeps the run that archived the old partition: put that one back
            with _reaching(store, f"put the old partition back at {store.where(final)}"):
                _move(store, final, f"{temporary}/{path}", aside=None)
                _move(store, f"{old}/{path}", final, aside=None)

    try:
        outcome = ledgered.ledgered(archiving, output, database, ledgered.Steps("archive", "archived", entry, make, undo))
        if outcome[0] == "made":
            _delete(store, old)  # the old partition, once the ledger records the one replacing it
    finally:
        if began:  # it wrote under the hidden names
            _delete(store, temporary)
            if store.fs.get_file_info(f"{old}/{path}").type == pafs.FileType.NotFound:
                _delete(store, old)
    match outcome:
        case "skipped", reason:
            return ArchivedRecord(status="skipped", at=archiving.clock(), path=store.where(final), reason=reason)
        case "already", _:
            return ArchivedRecord(status="archived", at=archiving.clock(), path=store.where(final), rows=rows.num_rows, reason="archived by this run already")
        case _:
            log.info("archived %s to %s", output, store.where(final), extra={"output": output, "rows": rows.num_rows, "files": len(files)})
            return ArchivedRecord(status="archived", at=archiving.clock(), path=store.where(final), rows=rows.num_rows, files=files)


def check(store: Lake, *, rename: bool) -> str:
    """What `basis doctor --env` says of a lake: its container is reachable, and, where `rename`, a scratch folder
    under it renames, as an archive's partition does, and is removed; or a `LakeError` naming what is not so."""
    _container(store)
    if not rename:
        return f"{store.where(store.container)} is reachable; the rename an archive makes is not tried, since this environment is read-only here"
    scratch = f"{store.container}/.basis-doctor-{uuid.uuid4().hex[:12]}"
    try:
        with _reaching(store, f"write and rename a scratch folder under {store.where(store.container)}"):
            store.fs.create_dir(f"{scratch}/a", recursive=True)
            _move(store, f"{scratch}/a", f"{scratch}/b", None)
    finally:
        _delete(store, scratch)
    return f"{store.where(store.container)} is reachable, and a folder in it renames"


def _container(store: Lake) -> None:
    """Refuse a container that is not there: basis never creates one, which a role scoped to it could not do."""
    with _reaching(store, f"reach its container {store.where(store.container)}"):
        kind = store.fs.get_file_info(store.container).type
    if kind != pafs.FileType.Directory:
        msg = f"the lake {store.name} has no container {store.where(store.container)}: create it, or correct {variable(('lakes', store.name, 'container'))}"
        raise LakeError(msg)


def _write(store: Lake, directory: str, rows: pa.Table) -> list[ArchivedFile]:
    """Write `rows` as the files of the partition `directory`, at most `FILE_ROWS` to a file, and read each back."""
    store.fs.delete_dir_contents(directory, missing_dir_ok=True)  # a leftover of an earlier attempt of this run
    store.fs.create_dir(directory, recursive=True)
    count = max(1, -(-rows.num_rows // FILE_ROWS))
    for index in range(count):
        pq.write_table(rows.slice(index * FILE_ROWS, FILE_ROWS), f"{directory}/part-{index}.parquet", filesystem=store.fs)
    files = _read_back(store, directory)
    if (found := sum(file.rows for file in files)) != rows.num_rows:
        msg = f"the archive at {store.where(directory)} reads back {found} rows, and {rows.num_rows} were written: run it again, and report it if it does the same"
        raise LakeError(msg)
    return files


def _read_back(store: Lake, directory: str) -> list[ArchivedFile]:
    """Each Parquet file of the partition `directory`, by name: its SHA-256 and the rows its footer gives."""
    infos = store.fs.get_file_info(pafs.FileSelector(directory, recursive=False))
    files: list[ArchivedFile] = []
    for info in sorted((info for info in infos if info.type == pafs.FileType.File and info.base_name.endswith(".parquet")), key=lambda info: info.base_name):
        with store.fs.open_input_stream(info.path) as stream:
            content = stream.read()
        files.append(ArchivedFile(name=info.base_name, sha256=hashlib.sha256(content).hexdigest(), rows=pq.read_metadata(pa.BufferReader(content)).num_rows))
    return files


type Settle = Literal["move", "replace", "keep"]
"""How a written partition goes into place: renamed in where there is none; renamed in once the old one is moved
aside, when told `again`; or left where it is, the one an earlier attempt renamed in."""


def _settled(store: Lake, final: str, files: list[ArchivedFile], *, again: bool) -> tuple[list[ArchivedFile], Settle]:
    """What the archive's ledger row records, and how the partition goes into place: as `Settle` says, the files in
    place already recorded where an earlier attempt renamed them in and died before its commit, and they are the
    files written, by name, hash and rows; and a refusal where they are not, unless told `again`."""
    if store.fs.get_file_info(final).type == pafs.FileType.NotFound:
        return files, "move"
    if again:
        return files, "replace"
    there = _read_back(store, final)
    if there == files:  # the same files, byte for byte, as Parquet writes the same rows the same way
        log.info("the partition %s is in place already, with the files written, and the ledger lacks it: recording it", store.where(final))
        return there, "keep"
    msg = f"the partition {store.where(final)} is there already, and the ledger has no run that archived it: move it aside if it is no run's, or pass --again to replace it"
    raise LakeError(msg)


def _move(store: Lake, source: str, final: str, aside: str | None) -> None:
    """Rename the partition at `source` into place at `final`, moving the old one to `aside` first where given."""
    try:
        if aside is not None:
            store.fs.create_dir(aside.rsplit("/", 1)[0], recursive=True)
            store.fs.move(final, aside)
        store.fs.create_dir(final.rsplit("/", 1)[0], recursive=True)
        try:
            store.fs.move(source, final)
        except BaseException:
            if aside is not None:  # the old partition goes back where it was, which the ledger still says holds the date
                store.fs.move(aside, final)
            raise
    except NotImplementedError as error:  # pyarrow refuses a rename on an account without the hierarchical namespace
        msg = f"the lake {store.name}'s account {store.account} has no hierarchical namespace, which the archive needs so that a rename is atomic: archive to an ADLS Gen2 account with it on"
        raise LakeError(msg) from error


def _delete(store: Lake, directory: str) -> None:
    try:
        store.fs.delete_dir(directory)
    except FileNotFoundError:
        pass
    except OSError as error:  # what is left is hidden from readers of the table, and the next attempt of the run clears it
        log.warning("could not remove %s: %s", store.where(directory), _first(error))


def _first(error: BaseException) -> str:
    """The error's text on one line, each URL's query left out, since a SAS token is one."""
    return _QUERY.sub(r"\1?...", " ".join(str(error).split()))[:300]


_QUERY = re.compile(r"(https?://[^\s?]*)\?\S*")


@contextmanager
def _reaching(store: Lake, what: str) -> Generator[None]:
    """Make an error of the lake's an error that names it, never carrying its URLs' queries, nor chaining pyarrow's
    error, whose text would: a log of the traceback would print a SAS token. Each is a span of its own, on the tracer
    of the run that makes it; pyarrow's filesystems take no timeout, so it has none."""
    attributes = {"basis.resource": store.name, "basis.lake.call": what.split(" ", 1)[0]}
    try:
        with traces.tracer().start_as_current_span(f"{store.name} {what.split(' ', 1)[0]}", kind=SpanKind.CLIENT, attributes=attributes):
            yield
    except LakeError:
        raise
    except (OSError, pa.ArrowException) as error:
        msg = f"the lake {store.name} failed to {what} ({type(error).__name__}: {_first(error)}): check its sign-in, and that this machine may reach it"
        raise LakeError(msg) from None
