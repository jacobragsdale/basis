"""From a transform's outputs to the changes a run would write, and every reason it may not write them. Pure: it
reads frames and declarations, and nothing else.

Each table's frame becomes `Changes` by its write mode, and each frame in them is conformed to the table's row, so a
naive datetime, a value that would round or a null where the row forbids one stops the run. Then the checks that
hold for every table: a key repeats in nothing the run writes, every row an insert adds is one its delete covers, so
a rerun cannot add it twice, and a delete that matches on values has no nulls, which would match nothing."""

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from basis.core.interval import Interval
from basis.core.tables import Changes, Delete, Range
from basis.job.outputs import Append, InsertNew, Manual, Outputs, Replace, ReplaceWhere, Target, Upsert
from basis.job.rows import SchemaError, conform, conform_columns


@dataclass(frozen=True)
class Planned:
    """One table's changes, conformed to its row, and the reasons the run may not write them."""

    name: str
    target: Target
    changes: Changes | None
    """None when the frame could not become changes at all."""
    reasons: list[str]


def plan(outputs: Outputs, interval: Interval | None) -> dict[str, Planned]:
    """Every declared table's changes, by output name."""
    return {name: _plan(name, table.target, outputs.values[name], interval) for name, table in outputs.declared().items()}


def _plan(name: str, target: Target, value: pl.DataFrame | Changes, interval: Interval | None) -> Planned:
    try:
        changes = _conformed(name, target, build(target, value, interval))
    except (SchemaError, ValueError) as error:
        return Planned(name, target, None, [f"{name}: {error}"])
    return Planned(name, target, changes, [f"{name}: {reason}" for reason in check(target, changes)])


def build(target: Target, value: pl.DataFrame | Changes, interval: Interval | None) -> Changes:
    """The changes a write mode makes of a frame; `Manual()` takes the transform's own."""
    match target.write, value:
        case Manual(), Changes():
            return value
        case Append(), pl.DataFrame():
            return Changes(insert=value)
        case Replace(), pl.DataFrame():
            return Changes(delete=Delete(), insert=value)
        case ReplaceWhere(interval=column, matching=matching), pl.DataFrame():
            within = None
            if column is not None:
                if interval is None:
                    msg = f"ReplaceWhere(interval={column!r}) replaces the run's interval, and this run has none: give it one"
                    raise ValueError(msg)
                within = Range(column, interval.start, interval.end)
            return Changes(delete=Delete(within, value.select(matching).unique() if matching else None), insert=value)
        case Upsert(), pl.DataFrame():
            return Changes(upsert=value)
        case InsertNew(), pl.DataFrame():
            return Changes(insert_new=value)
        case _:
            msg = f"{type(target.write).__name__}() takes a {'Changes' if isinstance(target.write, Manual) else 'DataFrame'}, not a {type(value).__name__}"
            raise ValueError(msg)


def _conformed(name: str, target: Target, changes: Changes) -> Changes:
    """The changes with every frame conformed to the row: `SchemaError` lists every problem in all of them."""
    problems: list[str] = []

    def conformed(frame: pl.DataFrame | None, operation: str) -> pl.DataFrame | None:
        if frame is None:
            return None
        try:
            return conform(frame, target.row, what=f"{name}'s {operation}", naive="reject")
        except SchemaError as error:
            problems.extend(f"{operation}: {problem}" for problem in error.problems)
            return None

    delete = changes.delete
    if delete is not None and delete.matching is not None:
        try:
            columns = [target.row.column(column.lower()) for column in delete.matching.columns]
            delete = Delete(delete.within, conform_columns(delete.matching, columns, what=f"{name}'s delete", naive="reject"))
        except (SchemaError, KeyError) as error:
            problems.extend(f"delete: {problem}" for problem in (error.problems if isinstance(error, SchemaError) else [str(error)]))
    result = Changes(delete, conformed(changes.upsert, "upsert"), conformed(changes.insert_new, "insert_new"), conformed(changes.insert, "insert"))
    if problems:
        raise SchemaError(name, problems)
    return result


def check(target: Target, changes: Changes) -> list[str]:
    """Why the run may not write `changes` to `target`, beyond its row."""
    reasons: list[str] = []
    written = {"upsert": changes.upsert, "insert_new": changes.insert_new, "insert": changes.insert}
    present = {operation: frame for operation, frame in written.items() if frame is not None}
    if (changes.upsert is not None or changes.insert_new is not None) and target.key is None:
        reasons.append("an upsert or an insert of new rows needs the table's key, as key=('id',)")
    if target.key is not None and present:
        keys = pl.concat([frame.select(target.key) for frame in present.values()])
        if (repeated := keys.filter(keys.is_duplicated())).height:
            reasons.append(f"{repeated.unique().height} keys appear more than once in what the run writes, such as {_example(repeated)}")
    if changes.delete is not None:
        reasons.extend(_delete_problems(target, changes.delete))
        if changes.insert is not None and (uncovered := uncovered_rows(target, changes.insert, changes.delete)).height:
            reasons.append(f"{uncovered.height} inserted rows are outside what the delete removes, so a rerun would insert them twice, such as {_example(uncovered)}")
    return reasons


def _delete_problems(target: Target, delete: Delete) -> list[str]:
    reasons: list[str] = []
    if delete.within is not None:
        if delete.within.column not in target.row.names():
            reasons.append(f"the delete's range is on {delete.within.column!r}, which the row does not have")
        elif target.row.column(delete.within.column).kind not in {"date", "datetime"}:
            reasons.append(f"the delete's range is on {delete.within.column!r}, which is not a date or datetime")
        elif delete.within.start >= delete.within.end:
            reasons.append(f"the delete's range starts at {delete.within.start}, not before its end {delete.within.end}")
    if delete.matching is not None:
        if not delete.matching.width:
            reasons.append("the delete matches on no columns: Delete() with neither within nor matching deletes every row")
        elif nulls := sum(delete.matching.null_count().row(0)):
            reasons.append(f"the delete matches on {nulls} nulls, which match nothing")
    return reasons


def uncovered_rows(target: Target, insert: pl.DataFrame, delete: Delete) -> pl.DataFrame:
    """The rows of `insert` that `delete` would not remove, were they in the table."""
    indexed = insert.with_row_index(_ROW)
    covered = indexed
    if delete.within is not None and delete.within.column in insert.columns:
        covered = covered.filter(_within(target, delete.within))
    if delete.matching is not None and delete.matching.width:
        covered = covered.join(delete.matching.unique(), on=delete.matching.columns, how="semi")
    return indexed.join(covered.select(_ROW), on=_ROW, how="anti").drop(_ROW)


_ROW = "#row"  # never a row's column, whose name is lower-case letters, digits and underscores


def _within(target: Target, within: Range) -> pl.Expr:
    column = pl.col(within.column)
    if target.row.column(within.column).kind == "date":
        column = column.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    return (column >= within.start) & (column < within.end)


def _example(frame: pl.DataFrame) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in frame.row(0, named=True).items())


def summary(planned: Mapping[str, Planned]) -> dict[str, dict[str, object]]:
    """What each table's changes do, for the run's record and its log."""
    return {name: describe(entry.target, entry.changes) for name, entry in planned.items()}


def describe(target: Target, changes: Changes | None) -> dict[str, object]:
    found: dict[str, object] = {"database": target.database, "table": target.table, "write": type(target.write).__name__}
    if changes is None:
        return found
    if changes.delete is not None:
        found["delete"] = _describe_delete(changes.delete)
    for operation, frame in (("upsert", changes.upsert), ("insert_new", changes.insert_new), ("insert", changes.insert)):
        if frame is not None:
            found[operation] = frame.height
    return found


def _describe_delete(delete: Delete) -> str:
    parts: list[str] = []
    if delete.within is not None:
        parts.append(f"{delete.within.column} in {Interval(delete.within.start, delete.within.end)}")
    if delete.matching is not None:
        parts.append(f"{delete.matching.height} values of ({', '.join(delete.matching.columns)})")
    return " and ".join(parts) if parts else "every row"
