"""Files under a root that is a local path or a URL, read and written through `pyarrow.fs`, so the snapshot root moves
from local disk to a mounted volume, NFS, Azure Data Lake or S3 by changing its setting alone. A write goes to
a temporary name and is renamed into place, so a reader never sees half a file; the root's filesystem must support
renames, as local disk, SMB and NFS mounts, and Data Lake with a hierarchical namespace do. `Store` lays a root out as
the run store."""

import hashlib
import re
import uuid
from collections.abc import Buffer, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from basis.core import frames
from basis.core.secrets import redacted

DIGEST = re.compile(r"[0-9a-f]{64}")
"""A dataset's name in the store: its content's SHA-256, in lower-case hex."""


@dataclass(frozen=True)
class Root:
    fs: pafs.FileSystem
    path: str  # the root inside `fs`, `/`-separated, without a trailing `/`
    location: str  # as configured, for messages, each secret it holds as ***

    @classmethod
    def at(cls, location: str | Path) -> Root:
        """A path, relative to the working directory or absolute, or a URL such as `abfs://container@account/runs`."""
        if "://" in str(location):
            fs, path = pafs.FileSystem.from_uri(str(location))
        else:  # pyarrow takes absolute paths only, with `/` on Windows too
            fs, path = pafs.LocalFileSystem(), Path(location).resolve().as_posix()
        return cls(fs, path.rstrip("/"), redacted(str(location)))  # what messages show: never its credentials

    def __truediv__(self, name: str) -> Root:
        return Root(self.fs, f"{self.path}/{name}", f"{self.location.rstrip('/')}/{name}")

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def parent(self) -> Root:
        parent, _, _ = self.path.rpartition("/")
        location, _, _ = self.location.rstrip("/").rpartition("/")
        return Root(self.fs, parent, location)

    def exists(self) -> bool:
        return bool(self.fs.get_file_info(self.path).type != pafs.FileType.NotFound)  # pyarrow is untyped: bool() types it

    def is_dir(self) -> bool:
        return bool(self.fs.get_file_info(self.path).type == pafs.FileType.Directory)

    def children(self) -> list[Root]:
        """What the root holds, files and directories, by name."""
        if not self.is_dir():
            return []
        return sorted((self / info.base_name for info in self.fs.get_file_info(pafs.FileSelector(self.path))), key=lambda root: root.name)

    def read_bytes(self) -> bytes:
        with self.fs.open_input_stream(self.path) as stream:
            return bytes(stream.read())

    def write_bytes(self, data: bytes) -> None:
        with self.writing() as stream:
            stream.write(data)

    @contextmanager
    def writing(self) -> Generator[pa.NativeFile]:
        """A stream to write the file through, as it is made, rather than whole from memory: under a temporary name,
        renamed onto this one when the block succeeds, and deleted when it fails."""
        with _replacing(self) as temporary, self.fs.open_output_stream(temporary.path) as stream:
            yield stream

    def read_table(self) -> pa.Table:
        """A Parquet file, with the types it was written with: decimals keep their precision, datetimes their zone."""
        table = pq.read_table(self.path, filesystem=self.fs)
        if not isinstance(table, pa.Table):
            msg = f"{self.location} does not hold a table"
            raise TypeError(msg)
        return table

    def read_frame(self) -> pd.DataFrame:
        """A Parquet file as a frame of Arrow-backed columns, each of the type it was written with."""
        return frames.from_arrow(self.read_table())

    def make_dir(self) -> None:
        self.fs.create_dir(self.path, recursive=True)

    def move_to(self, other: Root) -> None:
        self.fs.move(self.path, other.path)

    def delete(self) -> None:
        self.fs.delete_file(self.path)


@contextmanager
def _replacing(target: Root) -> Generator[Root]:
    """A temporary name beside `target`, renamed onto it when the block succeeds and deleted when it fails."""
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    target.parent.make_dir()
    try:
        yield temporary
    except BaseException:
        if temporary.exists():
            temporary.delete()
        raise
    temporary.move_to(target)


@dataclass(frozen=True)
class Store:
    """The run store, one per environment:

        objects/HASH.parquet               each dataset, once, named by the SHA-256 of the file (see `digest`)
        runs/PACKAGE/NAME/RUN_ID.json      each run's manifest, which names its datasets by hash

    An object is written once: one the store holds is never replaced, so a dataset that twenty runs load is stored
    once. A manifest is written after the objects it names, and like every file here under a hidden name renamed into
    place, so a reader never sees half of one, nor a manifest whose objects are missing. Each part writes its own
    manifests, under its own `PACKAGE`; every manifest is a JSON object whose `run_id` is its id, whose `labels` (such
    as a regress set's) are strings by key, and whose `datasets` maps each dataset's name to its object's hash, so a
    collector can keep the objects that any manifest it keeps names. Names starting with `.` are temporary."""

    root: Root

    def put(self, table: pa.Table) -> str:
        """Store `table` and return its hash. The table is written once, under a temporary name, and hashed as it is
        written; the file is then renamed to its hash, or deleted if the store holds that hash already."""
        folder = self.root / "objects"
        folder.make_dir()
        temporary = folder / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary.fs.open_output_stream(temporary.path) as stream:
                sink = _Hashing(stream)
                _write(table, sink)
            name = sink.hash.hexdigest()
            if (target := self.object(name)).exists():
                temporary.delete()
            else:
                temporary.move_to(target)
        except BaseException:
            if temporary.exists():
                temporary.delete()
            raise
        return name

    def object(self, name: str) -> Root:
        if DIGEST.fullmatch(name) is None:
            msg = f"{name!r} is not a dataset's hash: 64 lower-case hex digits"
            raise ValueError(msg)
        return self.root / "objects" / f"{name}.parquet"

    def manifest(self, package: str, name: str, run_id: str) -> Root:
        return self.root / "runs" / _segment(package) / _segment(name) / f"{_segment(run_id)}.json"

    def manifests(self, package: str, name: str) -> list[Root]:
        """The manifests of `name`'s runs, newest first: run ids sort by when their runs started."""
        found = [child for child in (self.root / "runs" / _segment(package) / _segment(name)).children() if child.name.endswith(".json") and not child.name.startswith(".")]
        return sorted(found, key=lambda manifest: manifest.name, reverse=True)

    def find(self, run_id: str, name: str | None = None) -> Root:
        """A run's manifest by its id, whichever part made it, and of `name` alone if given. An id or a name that
        cannot name a file in the store, such as one with a slash, names no run."""
        if _is_segment(run_id) and (name is None or _is_segment(name)):
            for package in (self.root / "runs").children():
                folders = [package / name] if name is not None else package.children()
                for folder in folders:
                    if not folder.name.startswith(".") and (candidate := folder / f"{run_id}.json").exists():
                        return candidate
        msg = f"no run {run_id} under {self.root.location}: is it the right snapshot root?"
        raise LookupError(msg)


def digest(table: pa.Table) -> str:
    """The hash `Store.put` names `table` by: the SHA-256 of the Parquet file it writes, with `_OBJECT`'s settings.

    Equal data gets one hash however it is laid out in memory. Parquet writes a column's values without its nulls,
    beside a level for each row that says which rows are null, so the bytes under a null never reach the file, as
    they would in Arrow's IPC stream. Without dictionaries, pyarrow's writer writes the same bytes however the table
    is chunked or sliced; with them, as measured with pyarrow 26, the bytes follow the chunks, so they are off. The
    tests hold a table of many pages to this, in odd chunks. The file carries the Arrow schema, which makes each
    column's name, order and type part of the hash, a decimal's scale and a datetime's zone included, so two datasets
    share an object only when it reads back as each of them.

    The table is written once, and hashed as it is written: no copy of it is made, and nothing is held but the
    writer's own buffers. The hash depends on `_OBJECT` and on pyarrow's version, whose writer the file records: a
    change to either may store a dataset once more after it, which costs space and never reads wrong data."""
    sink = _Hashing(None)
    _write(table, sink)
    return sink.hash.hexdigest()


_OBJECT: dict[str, object] = {
    "version": "2.6",
    "data_page_version": "1.0",
    "compression": "zstd",
    "compression_level": 1,
    "use_dictionary": False,
    "use_byte_stream_split": False,
    "write_statistics": True,
    "write_page_index": False,
    "store_schema": True,
    "store_decimal_as_integer": False,
    "use_compliant_nested_type": True,
    "row_group_size": 1 << 20,
}
"""How every object is written, so what its name is the hash of: each setting that decides the bytes, pinned where a
new default could change it. zstd at level 1 makes smaller files than pyarrow's default snappy with dictionaries, and
as fast; statistics let a reader such as DuckDB skip row groups."""


def _write(table: pa.Table, sink: _Hashing) -> None:
    pq.write_table(table, sink, **_OBJECT)


class _Hashing:
    """A file that pyarrow writes to, which hashes what it is given and passes it on to `stream`, if any."""

    def __init__(self, stream: pa.NativeFile | None) -> None:
        self.stream = stream
        self.hash = hashlib.sha256()
        self.size = 0
        self.closed = False

    def write(self, data: Buffer) -> int:
        view = memoryview(data)
        self.hash.update(view)
        if self.stream is not None:
            self.stream.write(view)
        self.size += view.nbytes
        return view.nbytes

    def tell(self) -> int:
        return self.size

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def writable(self) -> bool:
        return True


def _is_segment(name: str) -> bool:
    return bool(name) and not name.startswith(".") and "/" not in name and "\\" not in name


def _segment(name: str) -> str:
    """`name` as one segment of a path in the store: a part's, a run's or a run id's name."""
    if not _is_segment(name):
        msg = f"{name!r} cannot name a folder or file in the run store: it is empty, starts with '.', or holds a slash"
        raise ValueError(msg)
    return name
