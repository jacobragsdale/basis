"""etl-framework: the run loop ETL jobs share. A job declares its params, inputs and outputs, and transforms inputs to
outputs in a pure function; the framework loads, checks, snapshots and publishes the same way for every job.

    import etl_framework as etl

`etl.run` and `etl.replay` load the run loop on first use, so job code, which imports this package for its
declarations, imports nothing that reads or writes."""

from typing import TYPE_CHECKING

from etl_framework.inputs import File, Inputs, Query
from etl_framework.interval import Interval
from etl_framework.outputs import Append, Changes, Delete, InsertNew, Manual, Outputs, Range, Replace, ReplaceWhere, Table, Upsert
from etl_framework.params import Params
from etl_framework.rows import MaxLength, Precision, Row, Zone
from etl_framework.version import VERSION

if TYPE_CHECKING:
    from etl_framework.api import replay, run

__all__ = [
    "VERSION",
    "Append",
    "Changes",
    "Delete",
    "File",
    "Inputs",
    "InsertNew",
    "Interval",
    "Manual",
    "MaxLength",
    "Outputs",
    "Params",
    "Precision",
    "Query",
    "Range",
    "Replace",
    "ReplaceWhere",
    "Row",
    "Table",
    "Upsert",
    "Zone",
    "replay",
    "run",
]


def __getattr__(name: str) -> object:
    if name in {"run", "replay"}:
        from etl_framework import api  # noqa: PLC0415 -- the run loop loads on first use, which job code never makes

        return getattr(api, name)
    msg = f"module 'etl_framework' has no attribute {name!r}"
    raise AttributeError(msg)
