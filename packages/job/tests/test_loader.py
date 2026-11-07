from pathlib import Path

import pytest
from builders import DAY, Price, make_job, prices

import etl_framework as etl
from etl_framework.databases import Database, open_database
from etl_framework.loader import EmptyInputError, load
from etl_framework.settings import GivenSettings


def no_databases(name: str) -> Database:
    return open_database(GivenSettings(), name)


def test_a_csv_reads_as_text_its_row_converts_and_an_empty_field_is_null(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n2026-11-05,S1,100.5,,\n", encoding="utf-8")
    frames, records = load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)
    assert frames["prices"].row(0, named=True)["note"] is None
    assert frames["prices"].schema == prices({}).schema
    assert records["prices"].source == f"data: {tmp_path / 'prices.csv'}"


def test_an_empty_input_fails_unless_its_dataset_allows_it(tmp_path: Path) -> None:
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n", encoding="utf-8")
    with pytest.raises(EmptyInputError, match="prices returned no rows"):
        load(make_job(write=etl.Append()).inputs.declared(), DAY, open_database=no_databases, data=tmp_path)


def test_a_dataset_with_no_file_reads_its_source_and_says_what_to_configure(tmp_path: Path) -> None:
    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="markets", sql="prices.sql", empty="allow")

    with pytest.raises(LookupError, match="ETL_DATABASES__MARKETS__KIND"):
        load(Inputs.declared(), DAY, open_database=no_databases, data=tmp_path)
