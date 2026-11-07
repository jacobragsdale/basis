"""Row schemas to typed DataFrames.

Columns are validated one at a time, with a pydantic `TypeAdapter(list[T])` per field. That is about 5x faster than a
model per row (100k bonds: ~85ms vs ~460ms), at one cost: field rules must live in the annotation
(`Annotated[float, Field(gt=0)]`, `AfterValidator`). A row model with `@field_validator` or `@model_validator` is
refused rather than silently half-validated.
"""

import functools
from collections.abc import Callable, Sequence
from datetime import date
from types import NoneType
from typing import Annotated, Literal, TypeAliasType, get_args, get_origin

import pandas as pd
from pydantic import TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from optim.schema import Row
from optim.sources import Columns

type Dtype = str | pd.CategoricalDtype


def validate_columns(row: type[Row], columns: Columns, *, name: str, key: Sequence[str] = ()) -> pd.DataFrame:
    """Validate raw columns against `row` and build a typed frame. Errors name `name`, the column, and the bad row by its
    `key` columns, since a query's row numbers mean nothing to whoever must fix the data."""
    if not columns:  # an empty JSON response carries no column names
        return empty_frame(row)
    schema = _schema(row)
    if columns.keys() != schema.keys():
        msg = f"{name}: expected columns {sorted(schema)}, got {sorted(columns)}"
        raise ValueError(msg)

    def which(i: int) -> str:
        return ", ".join(f"{k} {columns[k][i]!r}" for k in key) or f"row {i}"

    return pd.DataFrame({column: _column(f"{name}.{column}", columns[column], *schema[column], which) for column in schema})


def empty_frame(row: type[Row]) -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dtype) for name, (_, dtype) in _schema(row).items()})


def conform(row: type[Row], frames: Sequence[pd.DataFrame], *, name: str) -> pd.DataFrame:
    """Frames this code built, as one frame validated against `row` and typed by it, as a dataset's frame is, for what
    others read, such as a snapshot's results. A field no frame has, such as `gap` when no solve was an integer one, is
    null; a column `row` has no field for is an error, so the schema cannot fall behind the code."""
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if extra := sorted(set(frame.columns) - set(row.model_fields)):
        msg = f"{name}: {row.__name__} has no field for {extra}"
        raise ValueError(msg)
    return validate_columns(row, to_columns(frame.reindex(columns=list(row.model_fields))), name=name)


def to_columns(frame: pd.DataFrame) -> Columns:
    """A frame's values as raw columns, nulls as None, as a source returns them: to validate a frame read from a file."""
    return {str(column): frame[column].astype(object).where(frame[column].notna(), None).tolist() for column in frame.columns}


def to_models[R: Row](row: type[R], frame: pd.DataFrame) -> list[R]:
    """A validated frame's rows as models, for small frames you want typed attributes on (one per portfolio)."""
    records = frame.astype(object).where(frame.notna(), None).to_dict("records")
    return [row.model_validate(record) for record in records]


def _column(label: str, values: list[object], adapter: TypeAdapter[list[object]], dtype: Dtype, which: Callable[[int], str]) -> pd.Series:
    try:
        return pd.Series(adapter.validate_python(values), dtype=dtype)
    except ValidationError as error:
        first = error.errors()[0]
        msg = f"{label} at {which(int(first['loc'][0]))}: {first['msg']} (got {first['input']!r}); {error.error_count()} bad values"
        raise ValueError(msg) from None  # the first error and the count; chaining all of them can flood a log


@functools.cache
def _schema(row: type[Row]) -> dict[str, tuple[TypeAdapter[list[object]], Dtype]]:
    validators = row.__pydantic_decorators__
    if validators.field_validators or validators.model_validators:
        msg = f"{row.__name__}: rows are validated column-wise; put validators in Annotated[...] metadata"
        raise TypeError(msg)
    return {name: (_column_adapter(field), _dtype(field.annotation)) for name, field in row.model_fields.items()}


def _column_adapter(field: FieldInfo) -> TypeAdapter[list[object]]:
    """Validates a whole column: the field's type and constraints, applied to every element."""
    return TypeAdapter(list[Annotated[field.annotation, field]], config=Row.model_config)  # ty: ignore[invalid-type-form] -- built from the row schema at runtime


def _dtype(annotation: object) -> Dtype:
    """str -> str, float -> float64, int -> int64 (Int64 if optional), bool, date -> datetime64[ms], Literal -> ordered
    category in declared order (so `rating <= "A-"` means "A- or better"). Dates are in ms, not s, because Parquet, which the cache and snapshots write, has no seconds
    unit: a frame read back would differ from the one loaded."""
    optional = NoneType in get_args(annotation)
    base = next(arg for arg in get_args(annotation) if arg is not NoneType) if optional else annotation
    if get_origin(base) is Annotated:  # `Price | None`: constraints sit inside the union
        base = get_args(base)[0]
    if isinstance(base, TypeAliasType):  # `type Action = Literal[...]`
        base = base.__value__
    if get_origin(base) is Literal:
        return pd.CategoricalDtype(list(get_args(base)), ordered=True)
    dtypes: dict[object, Dtype] = {str: "str", float: "float64", int: "Int64" if optional else "int64", bool: "boolean" if optional else "bool", date: "datetime64[ms]"}
    if base not in dtypes:
        msg = f"no pandas dtype for {annotation!r}; add one in optim.data.frames._dtype"
        raise TypeError(msg)
    return dtypes[base]
