"""Shared folders, such as the NFS shares where portfolio managers keep their workbooks: each a named path, which each
machine's settings give, `BASIS_FOLDERS__NAME__PATH`, as `\\\\server\\share\\pm` on Windows and `/mnt/pm` in WSL and in a
pod. A file in one is named by its path inside it, which may not leave it.

Workbooks are read and written with pandas, through `python-calamine` and `xlsxwriter`, which a repo takes as
`basis-core[excel]`. A sheet is read as text, each cell as a CSV's would be, so the row that declares it types every
value and nothing is inferred: a whole number as its digits, so `001234` saved as a number reads `1234` and a price
`100.5` reads `100.5`; a date as `YYYY-MM-DD`, a time with its seconds, `true` or `false`; an empty cell null. A
workbook is written to a temporary file beside it, which is then renamed into place, so a reader of the folder, a
portfolio manager's Excel among them, sees the old file or the new one, never half of one: a rename within one folder
is atomic on NFS, SMB and every local filesystem. A frame's datetimes are written in the zone each column names, as
Excel has no zones."""

import hashlib
import io
import math
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

import pandas as pd
import pyarrow as pa

from basis.core.errors import BasisError
from basis.core.settings import Settings, SettingsError, variable

EXCEL = (".xlsx", ".xlsm", ".xlsb", ".xls")
"""The workbooks a sheet is read from."""
WRITTEN = ".xlsx"
"""The one kind of workbook written."""
TEMPORARY = ".basis-tmp"
"""The suffix of a file being written, which no workbook has, so a reader that lists workbooks never lists it."""
_EXTRA = "add basis-core[excel] to the repo's dependencies, as `uv add 'basis-core[excel]'`, which brings python-calamine and xlsxwriter"


class FolderError(BasisError, ValueError):
    """A path that leaves its folder, a folder that is not there, or a workbook that cannot be read or written."""


@dataclass(frozen=True)
class Written:
    """A file written into a folder: where, its size and its SHA-256."""

    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class Folder:
    """A shared folder, by its name, at `root` on this machine."""

    name: str
    root: Path

    def at(self, relative: str) -> Path:
        """The file `relative` names in the folder: a relative path of `/`-separated parts, none of them `..`."""
        parts = PurePosixPath(relative.replace("\\", "/")).parts
        if not parts or PureWindowsPath(relative).is_absolute() or PurePosixPath(relative).is_absolute() or ".." in parts or PureWindowsPath(relative).drive:
            msg = f"{relative!r} is no path inside the folder {self.name}: give one relative to it, as positions/2026-11-05.xlsx, which never leaves it"
            raise FolderError(msg)
        return self.root.joinpath(*parts)

    def read_sheet(self, relative: str, sheet: str) -> pd.DataFrame:
        """The sheet `sheet` of the workbook at `relative`, its first row the columns' names, each cell as text."""
        path = self.at(relative)
        if path.suffix.lower() not in EXCEL:
            msg = f"{relative} in {self.name} is no workbook: name one, {', '.join(EXCEL)}"
            raise FolderError(msg)
        _engines("python_calamine")
        try:
            read = pd.read_excel(path, sheet_name=sheet, header=None, engine="calamine", dtype=object, na_filter=False)
        except FileNotFoundError as error:
            msg = f"{self.name} has no {relative}, at {path}: has it landed, or is the path wrong?"
            raise FolderError(msg) from error
        except ValueError as error:
            msg = f"{relative} in {self.name} has no sheet {sheet!r}, or cannot be read ({error}): name a sheet it has"
            raise FolderError(msg) from error
        rows = read.map(cell_text)
        frame = rows.iloc[1:].reset_index(drop=True)
        frame.columns = pd.Index([str(name) if name is not None else "" for name in rows.iloc[0]] if len(rows) else [], dtype=object)
        return frame.astype("string[pyarrow]")

    def write_workbook(self, relative: str, sheets: Mapping[str, pd.DataFrame], *, zones: Mapping[str, str] | None = None) -> Written:
        """Write `sheets`, each a frame by its sheet's name, as the workbook at `relative`, atomically, as the module's
        docstring says; `zones` names the zone each datetime column is written in, by column, UTC where it names none."""
        path = self.at(relative)
        if path.suffix.lower() != WRITTEN:
            msg = f"{relative}: basis writes workbooks as {WRITTEN}: name it so"
            raise FolderError(msg)
        if not sheets:
            msg = f"{relative}: a workbook has at least one sheet: give one"
            raise FolderError(msg)
        _engines("xlsxwriter")
        content = io.BytesIO()
        with pd.ExcelWriter(content, engine="xlsxwriter") as writer:
            for sheet, frame in sheets.items():
                plain(frame, zones or {}).to_excel(writer, sheet_name=sheet, index=False)
        return self.write_bytes(relative, content.getvalue())

    def write_bytes(self, relative: str, content: bytes) -> Written:
        """Write `content` as the file at `relative`, through a temporary file beside it, then renamed into place."""
        path = self.at(relative)
        if not self.root.is_dir():
            msg = f"the folder {self.name}, at {self.root}, is not there or not reachable from here: mount it, or correct {variable(('folders', self.name, 'path'))}"
            raise FolderError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}{TEMPORARY}")
        try:
            with temporary.open("xb") as file:
                _written(file, content)
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return Written(path, len(content), hashlib.sha256(content).hexdigest())


def _written(file: BinaryIO, content: bytes) -> None:
    """`content`, in `file`, on the disk before the rename that publishes it."""
    file.write(content)
    file.flush()
    os.fsync(file.fileno())


def cell_text(value: object) -> str | None:
    """A cell as the text a CSV would hold, as the module's docstring says, or None for an empty one."""
    text: str | None
    match value:
        case None | "":
            text = None
        case str():
            text = value
        case bool():
            text = "true" if value else "false"
        case datetime():
            text = value.date().isoformat() if value.time() == time() and value.tzinfo is None else value.isoformat()
        case date() | time():
            text = value.isoformat()
        case float() if math.isnan(value):
            text = None
        case float() if value.is_integer() and abs(value) < 2**53:
            text = str(int(value))
        case float() | int():
            text = repr(value)
        case _:
            text = str(value)
    return text


def plain(frame: pd.DataFrame, zones: Mapping[str, str]) -> pd.DataFrame:
    """`frame` as a workbook's cells hold it: each value a Python one, null where it is missing, and each datetime its
    wall clock in its column's zone, which Excel cannot hold."""
    out: dict[str, list[object]] = {}
    for name in frame.columns:
        column = frame[name]
        if isinstance(column.dtype, pd.ArrowDtype) and pa.types.is_timestamp(column.dtype.pyarrow_dtype):
            zone = zones.get(str(name), "UTC")
            local = column.dt.tz_convert(zone).dt.tz_localize(None) if column.dtype.pyarrow_dtype.tz is not None else column
            out[str(name)] = [None if pd.isna(value) else value.to_pydatetime() for value in local]
        else:
            out[str(name)] = [None if _missing(value) else value for value in column.tolist()]
    return pd.DataFrame(out, columns=[str(name) for name in frame.columns], dtype=object)


def _missing(value: object) -> bool:
    return value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value))


def _engines(module: str) -> None:
    try:
        __import__(module)
    except ImportError as error:
        msg = f"reading and writing workbooks needs {module}, which is not installed: {_EXTRA}"
        raise FolderError(msg) from error


def folder(name: str, settings: Settings) -> Folder:
    """The folder the settings name `name`."""
    if (found := settings.folders.get(name)) is None:
        msg = f"the folder {name} is not configured, and the run reads or writes it: set {variable(('folders', name, 'path'))}, as .env.example shows"
        raise SettingsError(msg)
    return Folder(name, Path(found.path))


type Opener = Callable[[str], Folder]
"""Each folder a run names, as the settings configure it, or a test's."""


def opener(settings: Settings) -> Opener:
    return lambda name: folder(name, settings)
