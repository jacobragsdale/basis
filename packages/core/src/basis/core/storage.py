"""Files under a root that is a local path or a URL, read and written through `pyarrow.fs`, so the snapshot root moves
from local disk to a mounted volume, NFS, Azure Data Lake or S3 by changing its setting alone. A write goes to
a temporary name and is renamed into place, so a reader never sees half a file; the root's filesystem must support
renames, as local disk, SMB and NFS mounts, and Data Lake with a hierarchical namespace do."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import polars as pl
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
        with _replacing(self) as temporary, self.fs.open_output_stream(temporary.path) as stream:
            stream.write(data)

    def read_frame(self) -> pl.DataFrame:
        """A Parquet file, with the types it was written with: decimals keep their precision, datetimes their zone."""
        frame = pl.from_arrow(pq.read_table(self.path, filesystem=self.fs))
        if not isinstance(frame, pl.DataFrame):
            msg = f"{self.location} holds a column, not a table"
            raise TypeError(msg)
        return frame

    def write_frame(self, frame: pl.DataFrame) -> None:
        with _replacing(self) as temporary:
            pq.write_table(frame.to_arrow(), temporary.path, filesystem=self.fs)

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
