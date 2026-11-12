"""Expectations of a job's tables (`etl.RowCount`, `etl.NullRate`, `etl.Total`), as a run evaluates them: on the
frames the transform returned, before anything is written, each that fails a reason not to write, named by its
output, as the framework's checks give theirs. A row count is held to the job's own recent published runs, which the
run store already holds, read once per run, and only for a job that declares one."""

import math
import statistics
from collections.abc import Mapping, Sequence

import pandas as pd
import pyarrow as pa

from basis.core import snapshots
from basis.core.snapshots import RunRecord
from basis.core.storage import Root
from basis.job.outputs import NullRate, Outputs, RowCount, Total, table_value

type History = Mapping[str, Sequence[int]]
"""Each table's rows in the job's recent published runs, newest first, by output."""


def history(root: Root, job: str, outputs: type[Outputs], *, package: str) -> History:
    """The rows each table that expects a row count held in the job's published runs, up to the most any of them
    compares, newest first; replays left out."""
    counted = {name: max(lasts) for name, table in outputs.declared().items() if (lasts := [entry.last for entry in table.expect if isinstance(entry, RowCount)])}
    found: dict[str, list[int]] = {name: [] for name in counted}
    if not counted:
        return found
    wanted = max(counted.values())
    seen = 0
    for snapshot in snapshots.runs_of(root, job, package=package):
        record = snapshot.record()
        if record.status != "published" or record.replay_of is not None:
            continue
        for name in counted:
            if (rows := _rows(record, name)) is not None:
                found[name].append(rows)
        seen += 1
        if seen >= wanted:
            break
    return found


def _rows(record: RunRecord, output: str) -> int | None:
    """The rows a published run wrote of `output`: every operation's but the delete of the rows it replaced."""
    entry = record.outputs.get(output)
    return sum(count for operation, count in entry.rows.items() if operation != "delete_matching") if entry is not None else None


def reasons(outputs: Outputs, inputs: Mapping[str, pa.Table], past: History) -> list[str]:
    """Why the transform's tables are not what their expectations say, each named by its output."""
    found: list[str] = []
    for name, table in type(outputs).declared().items():
        if not table.expect:
            continue
        frame = table_value(outputs, name)
        if not isinstance(frame, pd.DataFrame):
            continue
        found += [f"{name}: {reason}" for expectation in table.expect if (reason := _reason(expectation, frame, inputs, past.get(name, ()))) is not None]
    return found


def _reason(expectation: RowCount | NullRate | Total, frame: pd.DataFrame, inputs: Mapping[str, pa.Table], past: Sequence[int]) -> str | None:
    match expectation:
        case RowCount():
            return _counted(expectation, len(frame), past)
        case NullRate(column=column, at_most=at_most):
            share = float(frame[column].isna().mean()) if len(frame) else 0.0
            return f"{column} is null in {share:.0%} of its {len(frame)} rows, and at most {at_most:.0%} may be" if share > at_most else None
        case Total():
            return _reconciled(expectation, frame, inputs)


def _counted(expectation: RowCount, rows: int, past: Sequence[int]) -> str | None:
    compared = list(past[: expectation.last])
    if not compared:
        return None
    median = statistics.median(compared)
    low, high = median * (1 - expectation.within), median * (1 + expectation.within)
    if low <= rows <= high:
        return None
    last_runs = "the last published run's rows are" if len(compared) == 1 else f"the last {len(compared)} published runs' median is"
    return f"{rows} rows, and {last_runs} {median:g}: expected {low:g} to {high:g}, within {expectation.within:.0%} of it"


def _reconciled(expectation: Total, frame: pd.DataFrame, inputs: Mapping[str, pa.Table]) -> str | None:
    column, matches, within = expectation.column, expectation.matches, expectation.within
    dataset, _, source = matches.partition(".")
    read = inputs.get(dataset)
    if read is None or source not in read.column_names:
        return f"expects a total to match {matches}, and the job reads no dataset {dataset} with a column {source}"
    made = float(frame[column].sum(skipna=True))
    given = float(read.column(source).to_pandas().sum(skipna=True))
    if math.isclose(made, given, rel_tol=1e-12, abs_tol=within):
        return None
    return f"{column} totals {made:g}, and {matches} totals {given:g}: they differ by {abs(made - given):g}, and may by {within:g} at most"
