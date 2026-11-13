"""Row schemas, which a job's tables and a Dash app's datasets share: basis-core's `basis.core.rows`, whose docstring is
the reference. This module keeps the names jobs have always imported from it, and `Row`, core's, whose refusals name
the fix as a job's code spells it, as `etl.Zone('UTC')`."""

from basis.core import rows
from basis.core.rows import Json, MaxLength, RowError, SchemaError, Zone, conform, conform_columns, select

__all__ = ["Json", "MaxLength", "Row", "RowError", "SchemaError", "Zone", "conform", "conform_columns", "select"]


class Row(rows.Row, spelling="etl"):
    """Base for a table's columns, `basis.core.rows.Row` spelled as a job's: subclass it and annotate one attribute per
    column."""
