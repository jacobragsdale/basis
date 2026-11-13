"""From a transform's outputs to the changes a run would write, and every reason it may not write them. Pure: it
reads frames and declarations, and nothing else.

Each table's frame becomes `Changes` by its write mode, and each frame in them is conformed to the table's row, so a
naive datetime, a value that would round or a null where the row forbids one stops the run. Then the checks that
hold for every table: a key repeats in nothing the run writes, every row an insert adds is one its delete covers, so
a rerun cannot add it twice, and a delete that matches on values has no nulls, which would match nothing."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd

from basis.core import frames
from basis.core.interval import Interval, unsound_end
from basis.core.tables import Changes, Delete, Range
from basis.job.outputs import Append, InsertNew, Manual, Outputs, Replace, ReplaceWhere, Target, Upsert, table_value
from basis.job.rows import Row, SchemaError, conform, conform_columns, select


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
    return {name: _plan(name, table.target, table_value(outputs, name), interval) for name, table in outputs.declared().items()}


def _plan(name: str, target: Target, value: pd.DataFrame | Changes, interval: Interval | None) -> Planned:
    try:
        changes = _conformed(name, target, build(target, value, interval))
    except (SchemaError, ValueError) as error:
        return Planned(name, target, None, [f"{name}: {error}"])
    return Planned(name, target, changes, [f"{name}: {reason}" for reason in check(target, changes)])


def build(target: Target, value: pd.DataFrame | Changes, interval: Interval | None) -> Changes:
    """The changes a write mode makes of a frame; `Manual()` takes the transform's own."""
    match target.write, value:
        case Manual(), Changes():
            return value
        case Append(), pd.DataFrame():
            return Changes(insert=value)
        case Replace(), pd.DataFrame():
            return Changes(delete=Delete(), insert=value)
        case ReplaceWhere(interval=column, matching=matching), pd.DataFrame():
            within = None
            if column is not None:
                if interval is None:
                    msg = f"ReplaceWhere(interval={column!r}) replaces the run's interval, and this run has none: give it one"
                    raise ValueError(msg)
                within = Range(column, interval.start, interval.end)
            matched = select(value, matching, what="the frame ReplaceWhere(matching=...) deletes by").drop_duplicates(ignore_index=True) if matching else None
            return Changes(delete=Delete(within, matched), insert=value)
        case Upsert(), pd.DataFrame():
            return Changes(upsert=value)
        case InsertNew(), pd.DataFrame():
            return Changes(insert_new=value)
        case _:
            msg = f"{type(target.write).__name__}() takes a {'Changes' if isinstance(target.write, Manual) else 'DataFrame'}, not a {type(value).__name__}"
            raise ValueError(msg)


def _conformed(name: str, target: Target, changes: Changes) -> Changes:
    """The changes with every frame conformed to the row: `SchemaError` lists every problem in all of them."""
    problems: list[str] = []

    def conformed(frame: pd.DataFrame | None, operation: str) -> pd.DataFrame | None:
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
            columns = [target.row.column(str(column).lower()) for column in delete.matching.columns]
            delete = Delete(delete.within, conform_columns(delete.matching, columns, what=f"{name}'s delete", naive="reject", spelling=Row.spelling))
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
        keys = pd.concat([frame[list(target.key)] for frame in present.values()], ignore_index=True)
        if len(repeated := keys[keys.duplicated(keep=False)]):
            reasons.append(f"{len(repeated.drop_duplicates())} keys appear more than once in what the run writes, such as {_example(repeated)}")
    reasons.extend(_named_twice(target, [*present.values(), *([changes.delete.matching] if changes.delete is not None and changes.delete.matching is not None else [])]))
    if changes.delete is not None:
        reasons.extend(_delete_problems(target, changes.delete))
        if changes.insert is not None and len(uncovered := uncovered_rows(target, changes.insert, changes.delete)):
            reasons.append(f"{len(uncovered)} inserted rows are outside what the delete removes, so a rerun would insert them twice, such as {_example(uncovered)}")
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
        elif (zone := target.row.column(delete.within.column).zone) is not None:
            ends = [("starts", delete.within.start), ("ends", delete.within.end)]
            reasons.extend(
                f"the delete's range on {delete.within.column} {says} at {end}, {local:%H:%M} in {zone}, which shows that time twice as its clocks go back,"
                " so the naive range would delete other rows than the run's: start and end the run's interval outside that hour, or store the column in UTC"
                for says, end in ends
                if (local := unsound_end(end, zone)) is not None
            )
    if delete.matching is not None:
        if not len(delete.matching.columns):
            reasons.append("the delete matches on no columns: Delete() with neither within nor matching deletes every row")
        elif nulls := int(delete.matching.isna().sum().sum()):
            reasons.append(f"the delete matches on {nulls} nulls, which match nothing")
    return reasons


def _named_twice(target: Target, written: list[pd.DataFrame]) -> list[str]:
    """Why a zoned datetime column's values cannot be written as its database stores them, naive in its zone: a value at
    a time the zone shows twice names two instants there, and would read back as either."""
    reasons: list[str] = []
    for column in target.columns:
        if column.kind != "datetime" or column.zone is None:
            continue
        if not (parts := [frame[column.name] for frame in written if column.name in frame.columns]) or (values := pd.concat(parts, ignore_index=True)).empty:
            continue
        twice = values[frames.twice_named(values, column.zone)]
        if len(twice):
            reasons.append(
                f"{column.name}: {len(twice)} values are at a time {column.zone} shows twice as its clocks go back, such as {twice.iloc[0]},"
                " which a naive value cannot tell apart: store the column in UTC, as Annotated[datetime, etl.Zone('UTC')], or leave those rows out"
            )
    return reasons


def uncovered_rows(target: Target, insert: pd.DataFrame, delete: Delete) -> pd.DataFrame:
    """The rows of `insert` that `delete` would not remove, were they in the table."""
    covered = pd.Series(data=True, index=insert.index, dtype=bool)
    if delete.within is not None and delete.within.column in insert.columns:
        covered &= _within(target, insert[delete.within.column], delete.within)
    if delete.matching is not None and len(delete.matching.columns):
        covered &= _matched(insert, delete.matching)
    return insert[~covered].reset_index(drop=True)


_MATCHED = "#matched"  # never a row's column, whose name is lower-case letters, digits and underscores


def _within(target: Target, values: pd.Series, within: Range) -> pd.Series:
    """Whether each value is in the range; a null is not. A date is its midnight UTC, so it is in the range from the
    first date whose midnight is at or after the range's start, until the first at or after its end."""
    start, end = (_first_date(within.start), _first_date(within.end)) if target.row.column(within.column).kind == "date" else (within.start, within.end)
    return ((values >= start) & (values < end)).fillna(value=False).astype(bool)


def _first_date(moment: datetime) -> date:
    """The first date whose midnight UTC is at or after `moment`, an instant in UTC, as a range's ends are."""
    day = moment.date()
    return day if moment == datetime.combine(day, time(), UTC) else day + timedelta(days=1)


def _matched(insert: pd.DataFrame, matching: pd.DataFrame) -> pd.Series:
    """Whether each row of `insert` equals a row of `matching` in each of its columns; a null matches nothing."""
    names = [str(name) for name in matching.columns]
    keys = matching.dropna().drop_duplicates()
    merged = insert[names].merge(keys, on=names, how="left", indicator=_MATCHED)
    return pd.Series((merged[_MATCHED] == "both").to_numpy(), index=insert.index)


def _example(frame: pd.DataFrame) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in frame.iloc[0].to_dict().items())


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
            found[operation] = len(frame)
    return found


def _describe_delete(delete: Delete) -> str:
    parts: list[str] = []
    if delete.within is not None:
        parts.append(f"{delete.within.column} in {Interval(delete.within.start, delete.within.end)}")
    if delete.matching is not None:
        parts.append(f"{len(delete.matching)} values of ({', '.join(str(name) for name in delete.matching.columns)})")
    return " and ".join(parts) if parts else "every row"
