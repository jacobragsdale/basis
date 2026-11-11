"""How two runs' changes to a table differ, as `basis replay` and `basis regress` report it. With a key, rows are matched
by it, and a matched row that differs in any column counts as changed, by column; without one, rows are compared as
a multiset, so a row repeated once more is one added. Two nulls are equal, in a key and in a value alike, and two
floats are equal when they agree within a relative tolerance of 1e-12 (or an absolute one of 1e-12, near zero), so a
sum taken in another order is no change, where a cent on a billion is; without a key, floats are rounded to 12
significant digits to be grouped. A column added, removed or retyped between the runs is a change of its own, and the
rest compare."""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from basis.core.frames import FLOAT
from basis.core.tables import Changes
from basis.job.outputs import Target


@dataclass(frozen=True)
class OperationDiff:
    output: str
    operation: str
    added: int
    removed: int
    changed: int
    columns: dict[str, int] = field(default_factory=dict)
    """Changed rows, by the column that changed."""
    schema: dict[str, str] = field(default_factory=dict)
    """Each column the two runs' frames do not hold alike, and how: `added`, `removed`, or `retyped from A to B`."""
    rows: list[dict[str, object]] = field(default_factory=list)
    """The first `SAMPLE` changed rows, matched by key: each row's `key`, by column, and `changes`, each column that
    changed with its value `before` and `after`, as JSON holds them; `basis replay --rows N` prints the first N."""

    @property
    def same(self) -> bool:
        return not (self.added or self.removed or self.changed or self.schema)


def diff(name: str, target: Target, before: Changes | None, after: Changes | None) -> list[OperationDiff]:
    """Every operation whose rows differ between two runs' changes to one table, and the delete if it differs."""
    found: list[OperationDiff] = []
    old = before.frames() if before is not None else {}
    new = after.frames() if after is not None else {}
    for operation in sorted(set(old) | set(new)):
        key = target.key if operation != "delete_matching" else None
        result = diff_frames(name, operation, old.get(operation), new.get(operation), key)
        if not result.same:
            found.append(result)
    if _delete_of(before) != _delete_of(after):
        found.append(OperationDiff(name, "delete", added=0, removed=0, changed=1))
    return found


def _delete_of(changes: Changes | None) -> tuple[object, ...] | None:
    if changes is None or changes.delete is None:
        return None
    within = changes.delete.within
    return (within.column, within.start, within.end) if within is not None else (None,)


def diff_frames(output: str, operation: str, before: pd.DataFrame | None, after: pd.DataFrame | None, key: tuple[str, ...] | None) -> OperationDiff:
    """How two runs' frames of one operation differ. A column one has and the other has not, or holds as another type,
    is a change of its own; the rows compare by the columns both hold alike, by `key` where both hold all of it."""
    if before is None or after is None:
        rows = after if after is not None else before
        count = len(rows) if rows is not None else 0
        return OperationDiff(output, operation, added=count if after is not None else 0, removed=count if before is not None else 0, changed=0)
    schema = _schema(before, after)
    shared = [str(name) for name in before.columns if name in after.columns and name not in schema]
    if not shared:
        return OperationDiff(output, operation, added=len(after), removed=len(before), changed=0, schema=schema)
    if key is None or not set(key) <= set(shared):
        return _multiset(output, operation, _rounded(before[shared]), _rounded(after[shared]), schema)
    on = list(key)
    values = [name for name in shared if name not in key]
    merged = before[shared].merge(after[shared], on=on, how="outer", suffixes=("", _AFTER), indicator=_SIDE)
    side = merged[_SIDE]
    both = merged[side == "both"]
    differs = {name: ~_same(both[name], both[f"{name}{_AFTER}"]) for name in values}
    columns = {name: int(changed.sum()) for name, changed in differs.items()}
    any_changed = pd.concat(differs.values(), axis=1).any(axis=1) if differs else pd.Series(False, index=both.index)  # noqa: FBT003 -- a series of False, not a flag
    return OperationDiff(
        output, operation, added=int((side == "right_only").sum()), removed=int((side == "left_only").sum()), changed=int(any_changed.sum()),
        columns={name: count for name, count in columns.items() if count}, schema=schema, rows=_sample(both[any_changed], on, differs),
    )  # fmt: skip


SAMPLE = 20
"""How many changed rows a difference keeps, for `basis replay --rows N`."""


def _sample(changed: pd.DataFrame, key: list[str], differs: dict[str, pd.Series]) -> list[dict[str, object]]:
    """The first `SAMPLE` of `changed`, rows matched by `key` that differ, each its key and its changed columns' values
    before and after."""
    head = changed.head(SAMPLE)
    flags = pd.DataFrame(differs, index=head.index)
    found: list[dict[str, object]] = []
    for (_, row), (_, flag) in zip(head.iterrows(), flags.iterrows(), strict=True):
        changes = {name: {"before": _json(row[name]), "after": _json(row[f"{name}{_AFTER}"])} for name in differs if bool(flag[name])}
        found.append({"key": {name: _json(row[name]) for name in key}, "changes": changes})
    return found


def _json(value: object) -> object:
    """A frame's value as JSON holds it: a null as None, a float, text or a boolean as itself, and the rest, a date or
    a time, as its ISO text."""
    if value is None or value is pd.NA or value is pd.NaT or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


_SIDE = "#side"  # never a row's column, whose name is lower-case letters, digits and underscores
_AFTER = "#after"


def _schema(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, str]:
    """Each column added, removed or retyped from `before` to `after`, in the order the frames hold them."""
    found = {str(name): "removed" for name in before.columns if name not in after.columns}
    for name in after.columns:
        if name not in before.columns:
            found[str(name)] = "added"
        elif (old := before[name].dtype) != (new := after[name].dtype):
            found[str(name)] = f"retyped from {_type(old)} to {_type(new)}"
    return found


def _type(dtype: object) -> str:
    return str(dtype.pyarrow_dtype) if isinstance(dtype, pd.ArrowDtype) else str(dtype)


RELATIVE = 1e-12
"""How far apart, relative to their size, two floats may be and be the same: a sum of many terms taken in another order
differs by far less, and a cent on a billion by more."""
ABSOLUTE = 1e-12
"""How far apart two floats near zero may be and be the same, where a relative tolerance means nothing."""
SIGNIFICANT = 12
"""The digits floats are rounded to, to group rows that have no key: well inside what a double holds exactly, so two
orders of a sum land on the same side of a rounding boundary far more often than not."""


def _rounded(frame: pd.DataFrame) -> pd.DataFrame:
    """`frame` with each float rounded to `SIGNIFICANT` digits, as two runs' values compare."""
    rounded = frame.copy()
    for name in frame.columns:
        if frame[name].dtype == FLOAT:
            numbers = frame[name].to_numpy(copy=True)
            finite = np.isfinite(numbers)
            numbers[finite] = [float(f"{value:.{SIGNIFICANT}g}") for value in numbers[finite]]
            rounded[name] = numbers
    return rounded


def _same(before: pd.Series, after: pd.Series) -> pd.Series:
    """Whether each pair of values is equal, two nulls being equal, and two floats within the tolerance."""
    if before.dtype == FLOAT and after.dtype == FLOAT:
        old, new = before.to_numpy(dtype="float64"), after.to_numpy(dtype="float64")
        return pd.Series(np.isclose(old, new, rtol=RELATIVE, atol=ABSOLUTE, equal_nan=True), index=before.index)
    return ((before == after).fillna(value=False) | (before.isna() & after.isna())).astype(bool)


def _multiset(output: str, operation: str, before: pd.DataFrame, after: pd.DataFrame, schema: dict[str, str]) -> OperationDiff:
    columns = [str(name) for name in before.columns]
    old = before.groupby(columns, dropna=False, sort=False).size().rename("#before").reset_index()
    new = after.groupby(columns, dropna=False, sort=False).size().rename("#after").reset_index()
    joined = old.merge(new, on=columns, how="outer")
    counts = joined[["#before", "#after"]].fillna(0).astype("int64")
    added = int((counts["#after"] - counts["#before"]).clip(lower=0).sum())
    removed = int((counts["#before"] - counts["#after"]).clip(lower=0).sum())
    return OperationDiff(output, operation, added=added, removed=removed, changed=0, schema=schema)
