"""Every way a job's outputs can be declared wrong, refused when its class is made, with what to declare instead: as
`api/tests/test_routes.py` holds a route's declarations."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

import pandas as pd
import pytest

import basis.job as etl
from basis.core.messages import Mail
from basis.core.tables import Changes


class Trade(etl.Row):
    id: Annotated[str, etl.MaxLength(12)]
    day: date
    at: Annotated[datetime, etl.Zone("UTC")]
    desk: Annotated[str, etl.MaxLength(12)] | None
    terms: Annotated[str, etl.Json()]
    quantity: float


def table(**given: object) -> Callable[[], object]:
    return lambda: etl.Table(Trade, **({"database": "warehouse", "table": "trades", "write": etl.Append()} | given))  # ty: ignore[no-matching-overload] -- each refusal's own


def upsert(**given: object) -> Callable[[], object]:
    return lambda: etl.MongoUpsert(Trade, **({"mongo": "reference", "collection": "trades", "key": ("id",)} | given))  # ty: ignore[invalid-argument-type] -- as above


def lake(**given: object) -> Callable[[], object]:
    return lambda: etl.Lake(Trade, **({"lake": "history", "table": "trades", "partition": "date", "ledger": "warehouse"} | given))  # ty: ignore[invalid-argument-type] -- as above


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (table(table="trades; drop"), r"a table is TABLE or SCHEMA\.TABLE"),
        (table(database="Warehouse"), r"database 'Warehouse' is lower-case letters, digits and underscores, as in BASIS_DATABASES__WAREHOUSE__KIND"),
        (table(key=()), "a key names at least one column; leave key out for a table without one"),
        (table(key=("trade_id",)), r"its key names columns its row does not have: \['trade_id'\]"),
        (table(key=("terms",)), "its key's columns may not be JSON"),
        (table(key=("desk",)), r"its key's columns may not be null, but \['desk'\] are `\| None`"),
        (table(write=etl.Upsert()), r"Upsert\(\) needs the table's key, as key=\('id',\)"),
        (table(write=etl.InsertNew()), r"InsertNew\(\) needs the table's key"),
        (table(write=etl.ReplaceWhere(interval="quantity")), r"ReplaceWhere\(interval='quantity'\) needs a date or datetime column"),
        (table(write=etl.ReplaceWhere(matching=("book",))), r"ReplaceWhere\(matching=...\) names columns its row does not have: \['book'\]"),
        (table(write=etl.ReplaceWhere(matching=("terms",))), r"ReplaceWhere\(matching=...\) columns may not be JSON"),
        (table(write=etl.ReplaceWhere(matching=("desk",))), r"ReplaceWhere\(matching=...\) columns may not be null"),
        (etl.ReplaceWhere, "ReplaceWhere names the rows it replaces"),
        (upsert(mongo="Reference"), r"a Mongo database's name is lower-case letters, digits and underscores, as in BASIS_MONGO__REFERENCE__URI"),
        (upsert(collection="$trades"), r"name the collection, without \$"),
        (upsert(key=()), "an upsert's key names columns of its row, at least one"),
        (upsert(key=("trade_id",)), r"not \['trade_id'\]"),
        (upsert(key=("desk",)), r"an upsert's key columns may be neither null nor JSON, but \['desk'\] are"),
        (upsert(fields={"book": "Book"}), "fields names each column's field where the collection names it otherwise, by the row's column, and the row has no book"),
        (lake(lake="History"), "a lake's name is lower-case letters, digits and underscores, as in BASIS_LAKES__HISTORY__ACCOUNT"),
        (lake(table="Trades"), "a lake table's folder is lower-case letters, digits and underscores, starting with a letter"),
        (lake(partition="month"), "a lake table is partitioned by 'date' or by 'interval'"),
        (lake(ledger="Warehouse"), r"name the database whose ledger records the archive, as in BASIS_DATABASES__WAREHOUSE__\*"),
        (lambda: etl.Slack("desk", ledger="Warehouse", deadline_s=5), "a message names the database whose ledger records it"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_output_declared_wrong_is_refused_with_what_to_declare(make: Callable[[], object], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        make()


def test_an_output_is_named_once_by_its_attribute_in_lower_case() -> None:
    with pytest.raises(TypeError, match=r"Outputs\.Trades: an output's name is lower-case letters, digits and underscores"):

        class Outputs(etl.Outputs):
            Trades = etl.Table(Trade, database="warehouse", table="trades", write=etl.Append())

    shared = etl.Table(Trade, database="warehouse", table="trades", write=etl.Append())
    with pytest.raises(TypeError, match=r"Twice\.again is already declared as 'trades': declare each output once per name"):

        class Twice(etl.Outputs):
            trades = shared
            again = shared

    with pytest.raises(TypeError, match="a report is named by its attribute on a job's Outputs class; this one is not on one"):
        _ = etl.ExcelReport(Trade, folder="pm", path="x.xlsx", sheet="S").name


class Outputs(etl.Outputs):
    trades = etl.Table(Trade, database="warehouse", table="trades", write=etl.Append())
    history = etl.Table(Trade, database="warehouse", table="history", write=etl.Manual())
    report = etl.ExcelReport(Trade, folder="pm", path="x.xlsx", sheet="Trades")
    alert = etl.Slack("desk", ledger="warehouse", deadline_s=5)
    mail = etl.Email("desk", ledger="warehouse", deadline_s=5)


FRAME = pd.DataFrame()
MISSING = object()
"""An output the transform leaves out."""
GIVEN: dict[str, object] = {"trades": FRAME, "history": Changes(), "report": FRAME, "alert": None, "mail": None}


@pytest.mark.parametrize(
    ("given", "says"),
    [
        ({"report": MISSING}, r"Outputs is missing outputs \['report'\]: give every output it declares, None for a message it does not send"),
        ({"extra": FRAME}, r"Outputs declares no outputs named \['extra'\]; it declares"),
        ({"trades": Changes()}, r"trades is written with Append\(\), so its value is a DataFrame, not a Changes"),
        ({"history": FRAME}, r"history is written with Manual\(\), so its value is a Changes, not a DataFrame"),
        ({"report": "rows"}, "report is written from a frame, so its value is a DataFrame, not a str"),
        ({"alert": 3}, "alert posts to Slack, so its value is its text or None, not a int"),
        ({"mail": "text"}, r"mail is an email, so its value is an etl\.Mail\(subject=\.\.\., text=\.\.\.\) or None, not a str"),
    ],
    ids=["missing", "unknown", "frame-for-changes", "changes-for-frame", "report", "slack", "email"],
)
def test_a_transform_that_gives_an_output_the_wrong_value_is_refused_with_what_it_takes(given: dict[str, object], says: str) -> None:
    values = {name: value for name, value in (GIVEN | given).items() if value is not MISSING}
    with pytest.raises(TypeError, match=says):
        Outputs(**values)  # ty: ignore[invalid-argument-type] -- each refusal's own


def test_outputs_given_rightly_read_back_as_declared() -> None:
    made = Outputs(**(GIVEN | {"alert": "*traded*", "mail": Mail(subject="Trades", text="done")}))  # ty: ignore[invalid-argument-type] -- the values the transform gives
    assert (made.trades is FRAME, made.report is FRAME, made.alert, made.mail) == (True, True, "*traded*", Mail(subject="Trades", text="done"))
    assert sorted(Outputs.declared()) == ["history", "trades"]
    assert sorted(Outputs.declared_messages()) == ["alert", "mail"]
