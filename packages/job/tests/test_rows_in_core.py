"""A job's rows are basis-core's: `etl.Row` is `basis.core.rows`'s, spelled as a job's, and the other names
`basis.job.rows` has always exported are core's, so a job's row may declare a display format as any row does."""

from typing import Annotated

import basis.job as etl
import basis.job.rows
from basis.core import rows
from basis.core.display import Display


def test_a_job_declares_its_rows_with_the_same_names_and_may_give_a_display_format() -> None:
    assert issubclass(etl.Row, rows.Row)
    assert (etl.Row.spelling, etl.Row.columns) == ("etl", ())
    assert (etl.MaxLength, etl.Zone, etl.Json) == (rows.MaxLength, rows.Zone, rows.Json)
    assert (basis.job.rows.conform, basis.job.rows.SchemaError) == (rows.conform, rows.SchemaError)

    class Priced(etl.Row):
        security_id: Annotated[str, etl.MaxLength(8)]
        price: Annotated[float, Display(kind="price", decimals=3)]

    assert Priced.column("price").display == Display(kind="price", decimals=3)
