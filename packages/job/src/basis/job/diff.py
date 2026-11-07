"""How two runs' changes to a table differ, as `basis replay` and `basis regress` report it. With a key, rows are matched
by it, and a matched row that differs in any column counts as changed, by column; without one, rows are compared as
a multiset, so a row repeated once more is one added. Two nulls are equal, in a key and in a value alike. A column
added, removed or retyped between the runs is a change of its own, and the rest compare."""

from dataclasses import dataclass, field

import pandas as pd

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
        return _multiset(output, operation, before[shared], after[shared], schema)
    on = list(key)
    values = [name for name in shared if name not in key]
    merged = before[shared].merge(after[shared], on=on, how="outer", suffixes=("", _AFTER), indicator=_SIDE)
    side = merged[_SIDE]
    both = merged[side == "both"]
    differs = {name: ~_same(both[name], both[f"{name}{_AFTER}"]) for name in values}
    columns = {name: int(changed.sum()) for name, changed in differs.items()}
    changed = int(pd.concat(differs.values(), axis=1).any(axis=1).sum()) if differs else 0
    return OperationDiff(
        output, operation, added=int((side == "right_only").sum()), removed=int((side == "left_only").sum()), changed=changed,
        columns={name: count for name, count in columns.items() if count}, schema=schema,
    )  # fmt: skip


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


def _same(before: pd.Series, after: pd.Series) -> pd.Series:
    """Whether each pair of values is equal, two nulls being equal."""
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
