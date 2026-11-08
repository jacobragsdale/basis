"""basis-job: the run loop ETL jobs share. A job declares its params, inputs and outputs, and transforms inputs to
outputs in a pure function; the framework loads, checks, snapshots and publishes the same way for every job.

    import basis.job as etl

`etl.run` and `etl.replay` load the run loop on first use, so job code, which imports this package for its
declarations, imports nothing that reads or writes."""

from typing import TYPE_CHECKING

from basis.core.calendars import Calendar, calendar
from basis.core.interval import Interval
from basis.core.tables import Changes, Delete, Range
from basis.job.inputs import Clock, Fetch, File, Ids, Inputs, Query
from basis.job.outputs import Append, InsertNew, Manual, Outputs, Replace, ReplaceWhere, Table, Upsert
from basis.job.params import Params
from basis.job.rows import MaxLength, Row, Zone
from basis.job.version import VERSION

if TYPE_CHECKING:
    from basis.job.api import replay, run

__all__ = [
    "VERSION",
    "Append",
    "Calendar",
    "Changes",
    "Clock",
    "Delete",
    "Fetch",
    "File",
    "Ids",
    "Inputs",
    "InsertNew",
    "Interval",
    "Manual",
    "MaxLength",
    "Outputs",
    "Params",
    "Query",
    "Range",
    "Replace",
    "ReplaceWhere",
    "Row",
    "Table",
    "Upsert",
    "Zone",
    "calendar",
    "replay",
    "run",
]


def __getattr__(name: str) -> object:
    if name in {"run", "replay"}:
        from basis.job import api  # noqa: PLC0415 -- the run loop loads on first use, which job code never makes

        return getattr(api, name)
    msg = f"module 'basis.job' has no attribute {name!r}"
    raise AttributeError(msg)
