"""What a Dash app reads, declared: each dataset by name, with its row, its params and their types, and how fresh it
must be, nothing guessed; and each query's SQL file checked against its params, in its database's dialect, when the
app is built and before any environment is read."""

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from dash import html

import basis.dash as bd
from basis.core import fakes
from basis.core.rows import RowError, SchemaError, conform


class Position(bd.Row):
    account: str
    cusip: str
    par: Annotated[float, bd.format.money("USD", 0)]
    as_of: date


class Sector(bd.Row):
    sector: str
    weight: Annotated[float, bd.format.pct(1)]


def by_sector(positions: pd.DataFrame, book: str) -> pd.DataFrame:
    return positions.assign(sector=book)


class Needs(bd.Settings):
    token: str
    """A setting with no default, which reading the environment fails without."""


PAGE = bd.page("/", "Overview", layout=html.Div())


def _sql(tmp_path: Path, text: str) -> str:
    path = tmp_path / "positions.sql"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_query_declares_its_database_row_binds_and_freshness() -> None:
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    positions = bd.query("positions", warehouse, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=900, stale_s=86_400)
    assert (positions.params, positions.ttl_s, positions.stale_s, positions.database) == ({"as_of": date}, 900.0, 86_400.0, warehouse)
    assert positions.sql == Path(__file__).parent / "sql" / "positions.sql"  # beside the module that declared it
    assert positions.module == __name__
    assert Position.column("par").display == bd.format.money("USD", 0)


@pytest.mark.parametrize(
    ("freshness", "message"),
    [
        ({}, "does not say how fresh it must be: give ttl_s"),
        ({"ttl_s": 900}, "does not say how fresh it must be"),
        ({"ttl_s": 0, "stale_s": 60}, "ttl_s is 0: give it seconds above 0"),
        ({"ttl_s": 60, "stale_s": -1}, "stale_s is -1: give it seconds of 0 or more"),
    ],
)
def test_a_dataset_that_does_not_say_how_fresh_it_must_be_is_refused_where_it_is_declared(freshness: dict[str, float], message: str) -> None:
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    with pytest.raises(bd.DeclarationError, match=message):
        bd.query("positions", warehouse, "sql/positions.sql", Position, **freshness)  # ty: ignore[invalid-argument-type] -- each case gives a wrong value on purpose
    with pytest.raises(bd.DeclarationError, match=message):
        bd.file("positions", bd.folder("desk"), "positions.csv", Position, **freshness)  # ty: ignore[invalid-argument-type] -- each case gives a wrong value on purpose


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"binds": {"as_of": list}}, "param as_of is <class 'list'>: give one of str, float, int, bool, date, one value each, never a list"),
        ({"binds": {"As-Of": date}}, "a param's name is lower-case letters"),
        ({"name": "Positions"}, "a name is lower-case letters"),
        ({"sql": "positions.txt"}, "give its SQL file"),
        ({"row": dict}, "give a class of its columns, declared as class Position"),
    ],
)
def test_a_query_states_everything_it_reads(change: dict[str, object], message: str) -> None:
    given: dict[str, object] = {"name": "positions", "sql": "sql/positions.sql", "row": Position, "binds": {"as_of": date}} | change
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    with pytest.raises(bd.DeclarationError, match=message):
        bd.query(given["name"], warehouse, given["sql"], given["row"], binds=given["binds"], ttl_s=60, stale_s=0)  # ty: ignore[invalid-argument-type] -- each case gives a wrong value on purpose


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ({"dialect": "postgres", "deadline_s": 5, "max_concurrent": 1}, "give dialect='mssql' or dialect='oracle'"),
        ({"dialect": "mssql", "deadline_s": 0, "max_concurrent": 1}, "give it deadline_s"),
        ({"dialect": "mssql", "deadline_s": 5, "max_concurrent": 0}, "give it max_concurrent, a whole number of 1 or more"),
    ],
)
def test_a_database_states_its_dialect_deadline_and_concurrency(declared: dict[str, object], message: str) -> None:
    with pytest.raises(bd.DeclarationError, match=message):
        bd.database("warehouse", **declared)  # ty: ignore[invalid-argument-type] -- each case gives a wrong value on purpose


def test_a_file_reads_a_csv_or_parquet_inside_its_folder() -> None:
    desk = bd.folder("desk")
    assert bd.file("marks", desk, "marks/2026-11.csv", Position, ttl_s=60, stale_s=0).folder is desk
    with pytest.raises(bd.DeclarationError, match="no path inside the folder desk"):
        bd.file("marks", desk, "../marks.csv", Position, ttl_s=60, stale_s=0)
    with pytest.raises(bd.DeclarationError, match=r"whose name ends \.csv, \.parquet, \.xlsx"):
        bd.file("marks", desk, "marks.json", Position, ttl_s=60, stale_s=0)


def test_a_views_params_are_its_inputs_and_its_functions_own() -> None:
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    positions = bd.query("positions", warehouse, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=900, stale_s=0)
    sectors = bd.view("sectors", by_sector, Sector, positions=positions)
    assert sectors.params == {"as_of": date, "book": str}
    assert sectors.inputs == {"positions": positions}
    assert sectors.resources() == (warehouse,)


def _no_input(frame: pd.DataFrame) -> pd.DataFrame:
    return frame


def _unannotated(positions, book):  # noqa: ANN001, ANN202 -- a function that says no param's type
    return positions, book


def _as_of_text(positions: pd.DataFrame, as_of: str) -> pd.DataFrame:
    return positions.assign(as_of=as_of)


@pytest.mark.parametrize(
    ("function", "message"),
    [
        (_no_input, "takes no parameter positions: name a parameter after each input"),
        (_unannotated, "takes book, which is no input: annotate it with its type"),
        (_as_of_text, "param as_of is a date in one input and a str in another: give it one type"),
    ],
)
def test_a_view_names_a_parameter_for_each_input_and_types_each_param(function: object, message: str) -> None:
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    positions = bd.query("positions", warehouse, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=900, stale_s=0)
    with pytest.raises(bd.DeclarationError, match=message):
        bd.view("sectors", function, Sector, positions=positions)  # ty: ignore[invalid-argument-type] -- each case gives a wrong function on purpose


@pytest.mark.parametrize(
    ("dialect", "sql", "message"),
    [
        ("mssql", "SELECT * FROM positions WHERE as_of = %(as_of)s AND book = %(book)s", "binds book, which it does not declare: declare each in binds="),
        ("mssql", "SELECT * FROM positions", r"declares as_of, which its SQL never binds: bind each, as %\(as_of\)s"),
        ("mssql", "SELECT * FROM positions WHERE as_of = :as_of", r"binds as_of in Oracle's form, and database warehouse is SQL Server: write each as %\(NAME\)s"),
        ("oracle", "SELECT * FROM positions WHERE as_of = %(as_of)s", "binds as_of in SQL Server's form, and database warehouse is Oracle: write each as :NAME"),
    ],
)
def test_building_the_app_checks_each_sql_files_binds_in_its_dialect_before_reading_the_environment(tmp_path: Path, dialect: str, sql: str, message: str) -> None:
    warehouse = bd.database("warehouse", dialect=dialect, deadline_s=30, max_concurrent=4)  # ty: ignore[invalid-argument-type] -- a dialect of the case's
    bd.query("positions", warehouse, _sql(tmp_path, sql), Position, binds={"as_of": date}, ttl_s=900, stale_s=0)
    app = bd.App(name="positions", title="Positions", settings=Needs, resources=[warehouse], pages=[PAGE])
    with pytest.raises(bd.DeclarationError, match=message):
        bd.build(app)  # Needs's token is unset, so reading the environment would fail first


def test_a_query_whose_file_binds_exactly_its_params_builds(tmp_path: Path) -> None:
    warehouse = bd.database("warehouse", dialect="oracle", deadline_s=30, max_concurrent=4)
    sql = "SELECT account, cusip, par, as_of FROM positions -- :not_a_bind\nWHERE as_of = :as_of AND note <> ':nor_this'"
    bd.query("positions", warehouse, _sql(tmp_path, sql), Position, binds={"as_of": date}, ttl_s=900, stale_s=0)
    app = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[warehouse], pages=[PAGE])
    assert bd.build(app, settings=bd.Settings.given(), fakes={"warehouse": fakes.Database("warehouse", dialect="oracle")}).app is app


def test_an_apps_resources_are_declared_and_its_datasets_named_once(tmp_path: Path) -> None:
    warehouse = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    missing = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[warehouse], pages=[PAGE])
    bd.query("positions", warehouse, str(tmp_path / "missing.sql"), Position, ttl_s=900, stale_s=0)
    with pytest.raises(bd.DeclarationError, match=r"missing\.sql, which cannot be read: write it"):
        bd.build(missing, settings=bd.Settings.given())
    other = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)
    sql = _sql(tmp_path, "SELECT 1")
    bd.query("marks", other, sql, Position, ttl_s=900, stale_s=0)
    bd.query("marks", other, sql, Position, ttl_s=900, stale_s=0)
    twice = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[other, other, "pricing"], pages=[PAGE])
    with pytest.raises(bd.DeclarationError) as refused:
        bd.build(twice, settings=bd.Settings.given())
    assert "lists 'pricing' in its resources: list what it reads" in str(refused.value)
    assert "more than one database named warehouse" in str(refused.value)
    assert "more than one dataset named marks" in str(refused.value)


def test_a_rows_refusal_names_the_fix_in_bds_words() -> None:
    with pytest.raises(RowError, match=r"Annotated\[datetime, bd\.Zone\('UTC'\)\]"):
        type("Unzoned", (bd.Row,), {"__annotations__": {"at": datetime}})

    class Terms(bd.Row):
        terms: Annotated[str, bd.Json()]

    with pytest.raises(SchemaError, match=r"write it with bd\.json_text"):
        conform(pd.DataFrame({"terms": ["7"]}), Terms, what="terms", naive="reject")
    assert bd.json_text({"coupon": 0.045}) == '{"coupon":0.045}'
