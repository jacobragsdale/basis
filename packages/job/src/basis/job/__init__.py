"""basis-job: the run loop ETL jobs share. A job declares its params, inputs and outputs, and transforms inputs to
outputs in a pure function; the framework loads, checks, snapshots and publishes the same way for every job.

    import basis.job as etl

`etl.run` and `etl.replay` load the run loop on first use, so job code, which imports this package for its
declarations, imports nothing that reads or writes."""

from typing import TYPE_CHECKING

from basis.core.calendars import Calendar, calendar
from basis.core.errors import missing
from basis.core.interval import Interval
from basis.core.jsontext import dumps as json_text
from basis.core.messages import Mail
from basis.core.tables import Changes, Delete, Range
from basis.job.inputs import Clock, Cursor, Excel, Fetch, File, Ids, Inputs, Mongo, NextLink, Offset, Query
from basis.job.outputs import Append, Email, ExcelReport, Expectation, InsertNew, Lake, LakeCopy, Manual, MongoUpsert, NullRate, Outputs, Replace, ReplaceWhere, RowCount, Slack, Table, Total, Upsert
from basis.job.params import Params
from basis.job.rows import Json, MaxLength, Row, Zone
from basis.job.version import VERSION

if TYPE_CHECKING:
    from basis.job.api import replay, run

__all__ = [
    "VERSION",
    "Append",
    "Calendar",
    "Changes",
    "Clock",
    "Cursor",
    "Delete",
    "Email",
    "Excel",
    "ExcelReport",
    "Expectation",
    "Fetch",
    "File",
    "Ids",
    "Inputs",
    "InsertNew",
    "Interval",
    "Json",
    "Lake",
    "LakeCopy",
    "Mail",
    "Manual",
    "MaxLength",
    "Mongo",
    "MongoUpsert",
    "NextLink",
    "NullRate",
    "Offset",
    "Outputs",
    "Params",
    "Query",
    "Range",
    "Replace",
    "ReplaceWhere",
    "Row",
    "RowCount",
    "Slack",
    "Table",
    "Total",
    "Upsert",
    "Zone",
    "calendar",
    "json_text",
    "replay",
    "run",
]


def __getattr__(name: str) -> object:
    if name in {"run", "replay"}:
        from basis.job import api  # noqa: PLC0415 -- the run loop loads on first use, which job code never makes

        return getattr(api, name)
    return missing("basis.job", name, RENAMED)


RENAMED: dict[str, str] = {}
"""Each name a release renamed, by its old name, with its new one: refused with it for one release, then removed, as
basis's compatibility promise says (`scripts/surface.py`)."""
