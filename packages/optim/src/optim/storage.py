"""Files under a root that is a local path or a URL, read and written through `pyarrow.fs`, so the snapshot root and the
cache root move from local disk to NFS, Azure Files or Azure Data Lake by changing their setting alone. A write goes to
a temporary name and is renamed into place, so a reader never sees half a file; the root's filesystem must support
renames, as local disk, SMB and NFS mounts, and Data Lake with a hierarchical namespace do."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Root:
    fs: pafs.FileSystem
    path: str  # the root inside `fs`, `/`-separated, without a trailing `/`
    location: str  # as configured, for messages

    @classmethod
    def at(cls, location: str | Path) -> Root:
        """A path, relative to the working directory or absolute, or a URL such as `abfs://container@account/runs`."""
        if "://" in str(location):
            fs, path = pafs.FileSystem.from_uri(str(location))
        else:  # pyarrow takes absolute paths only, with `/` on Windows too
            fs, path = pafs.LocalFileSystem(), Path(location).resolve().as_posix()
        return cls(fs, path.rstrip("/"), str(location))

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
        found: bool = self.fs.get_file_info(self.path).type != pafs.FileType.NotFound  # pyarrow is untyped: declared here
        return found

    def is_dir(self) -> bool:
        directory: bool = self.fs.get_file_info(self.path).type == pafs.FileType.Directory
        return directory

    def children(self) -> list[Root]:
        """What the root holds, files and directories, by name."""
        if not self.is_dir():
            return []
        return sorted((self / info.base_name for info in self.fs.get_file_info(pafs.FileSelector(self.path))), key=lambda root: root.name)

    def read_bytes(self) -> bytes:
        with self.fs.open_input_stream(self.path) as stream:
            data: bytes = stream.read()
        return data

    def write_bytes(self, data: bytes) -> None:
        with _replacing(self) as temporary, self.fs.open_output_stream(temporary.path) as stream:
            stream.write(data)

    def read_frame(self) -> tuple[pd.DataFrame, dict[bytes, bytes]]:
        """A Parquet file as `DataFrame.to_parquet` would read it back, with its schema's metadata."""
        table = pq.read_table(self.path, filesystem=self.fs)
        frame: pd.DataFrame = table.to_pandas()
        metadata: dict[bytes, bytes] = dict(table.schema.metadata or {})
        return frame, metadata

    def write_frame(self, frame: pd.DataFrame, metadata: dict[bytes, bytes] | None = None) -> None:
        """`frame` as Parquet, as `DataFrame.to_parquet` writes it, so dtypes such as ordered categories survive."""
        table = pa.Table.from_pandas(frame, preserve_index=False)
        table = table.replace_schema_metadata((table.schema.metadata or {}) | (metadata or {}))
        with _replacing(self) as temporary:
            pq.write_table(table, temporary.path, filesystem=self.fs)

    def make_dir(self) -> None:
        self.fs.create_dir(self.path, recursive=True)

    def delete_dir(self) -> None:
        self.fs.delete_dir(self.path)

    def move_to(self, other: Root) -> None:
        self.fs.move(self.path, other.path)


@contextmanager
def _replacing(target: Root) -> Generator[Root]:
    """A temporary name beside `target`, renamed onto it when the block succeeds and deleted when it fails."""
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    target.parent.make_dir()
    try:
        yield temporary
    except BaseException:
        if temporary.exists():
            temporary.fs.delete_file(temporary.path)
        raise
    temporary.move_to(target)
