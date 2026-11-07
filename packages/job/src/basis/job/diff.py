"""How two runs' changes to a table differ, as `basis replay` and `basis regress` report it. With a key, rows are matched
by it, and a matched row that differs in any column counts as changed, by column; without one, rows are compared as
a multiset, so a row repeated once more is one added."""

from dataclasses import dataclass, field

import polars as pl

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

    @property
    def same(self) -> bool:
        return not (self.added or self.removed or self.changed)


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


def diff_frames(output: str, operation: str, before: pl.DataFrame | None, after: pl.DataFrame | None, key: tuple[str, ...] | None) -> OperationDiff:
    if before is None or after is None:
        rows = after if after is not None else before
        count = rows.height if rows is not None else 0
        return OperationDiff(output, operation, added=count if after is not None else 0, removed=count if before is not None else 0, changed=0)
    if key is None:
        return _multiset(output, operation, before, after)
    on = list(key)
    added = after.join(before, on=on, how="anti", nulls_equal=True).height
    removed = before.join(after, on=on, how="anti", nulls_equal=True).height
    values = [name for name in before.columns if name not in key]
    both = before.join(after, on=on, how="inner", suffix="#after", nulls_equal=True)
    differs = {name: pl.col(name).ne_missing(pl.col(f"{name}#after")) for name in values}
    columns = {name: int(both.select(expression.sum()).item()) for name, expression in differs.items()}
    changed = both.filter(pl.any_horizontal(differs.values())).height if differs else 0
    return OperationDiff(output, operation, added=added, removed=removed, changed=changed, columns={name: count for name, count in columns.items() if count})


def _multiset(output: str, operation: str, before: pl.DataFrame, after: pl.DataFrame) -> OperationDiff:
    columns = before.columns
    old = before.group_by(columns).agg(pl.len().cast(pl.Int64).alias("#before"))  # pl.len() is unsigned: a difference would wrap
    new = after.group_by(columns).agg(pl.len().cast(pl.Int64).alias("#after"))
    joined = old.join(new, on=columns, how="full", coalesce=True, nulls_equal=True).fill_null(0)
    added = int(joined.select((pl.col("#after") - pl.col("#before")).clip(lower_bound=0).sum()).item())
    removed = int(joined.select((pl.col("#before") - pl.col("#after")).clip(lower_bound=0).sum()).item())
    return OperationDiff(output, operation, added=added, removed=removed, changed=0)
