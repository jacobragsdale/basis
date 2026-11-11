"""A job that reads Mongo and upserts documents back, through `fakes.Mongo`, and a JSON column, which carries a
document's nesting into SQL Server's `json` type: its values checked, its DDL, and its rules as a key."""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from builders import DAY, params_file

import basis.job as etl
from basis.core import fakes
from basis.core.destination import DestColumn, TableSpec, check_table
from basis.core.errors import Unavailable
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.job import Job
from basis.job.rows import SchemaError, conform


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


class Bond(etl.Row):
    _id: Annotated[str, etl.MaxLength(24)]
    cusip: Annotated[str, etl.MaxLength(9)]
    coupon: float
    terms: Annotated[str, etl.Json()] | None


class Summary(etl.Row):
    cusip: Annotated[str, etl.MaxLength(9)]
    coupon_bp: float
    terms: Annotated[str, etl.Json()] | None


class Params(etl.Params):
    pass


class Inputs(etl.Inputs):
    bonds = etl.Mongo(Bond, mongo="reference", collection="bonds", filter={"updated": {"$gte": "{start}", "$lt": "{end}"}}, empty="fail")


class Outputs(etl.Outputs):
    summaries = etl.MongoUpsert(Summary, mongo="reference", collection="summaries", key=("cusip",))


def transform(inputs: Inputs, params: Params) -> Outputs:
    del params  # none change what it writes
    bonds = inputs.bonds
    return Outputs(summaries=pd.DataFrame({"cusip": bonds["cusip"], "coupon_bp": bonds["coupon"] * 10_000, "terms": bonds["terms"]}))


JOB = Job("summaries", "tests.summaries", Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.fixture
def reference() -> fakes.Mongo:
    store = fakes.Mongo()
    store.collections["bonds"] = [
        {"_id": "b1", "cusip": "912810TM6", "coupon": 0.045, "updated": datetime(2026, 11, 5, 9, tzinfo=UTC), "terms": {"frequency": 2, "callable": False}},
        {"_id": "b2", "cusip": "912810TN4", "coupon": 0.0425, "updated": datetime(2026, 11, 5, 10, tzinfo=UTC), "terms": None},
        {"_id": "b3", "cusip": "OLD", "coupon": 0.01, "updated": datetime(2026, 11, 4, tzinfo=UTC)},  # outside the interval
    ]
    store.unique["summaries"] = [("cusip",)]
    return store


def run(tmp_path: Path, reference: fakes.Mongo, *, publish: bool = True) -> runs.Result:
    request = runs.Request(params_file(tmp_path), DAY, publish=publish)
    return runs.run(JOB, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock, faked={"reference": reference})


def test_a_job_reads_mongo_in_its_interval_and_upserts_documents_with_their_json_back(tmp_path: Path, reference: fakes.Mongo) -> None:
    result = run(tmp_path, reference)
    assert result.record.status == "published", result.record
    assert result.record.inputs["bonds"].rows == 2
    assert (result.record.upserted["summaries"].upserted, result.record.upserted["summaries"].modified) == (2, 0)
    assert reference.collections["summaries"] == [
        {"cusip": "912810TM6", "coupon_bp": pytest.approx(450.0, rel=1e-15), "terms": {"frequency": 2, "callable": False}},
        {"cusip": "912810TN4", "coupon_bp": pytest.approx(425.0, rel=1e-15), "terms": None},
    ]


def test_a_second_run_upserts_the_same_documents_and_changes_nothing(tmp_path: Path, reference: fakes.Mongo) -> None:
    run(tmp_path, reference)
    again = run(tmp_path, reference)
    assert (again.record.upserted["summaries"].matched, again.record.upserted["summaries"].modified, again.record.upserted["summaries"].upserted) == (2, 0, 0)


def test_a_run_without_publish_upserts_nothing(tmp_path: Path, reference: fakes.Mongo) -> None:
    result = run(tmp_path, reference, publish=False)
    assert (result.record.status, result.record.documents["summaries"].rows, result.record.upserted) == ("checked", 2, {})
    assert "summaries" not in reference.collections


def test_an_upsert_that_fails_leaves_the_run_partial_and_its_resume_makes_it(tmp_path: Path, reference: fakes.Mongo) -> None:
    reference.fails(Unavailable, times=1, on="find")
    assert run(tmp_path, reference).record.status == "failed"  # a read that fails publishes nothing
    reference.fails(Unavailable, times=1, on="upsert")
    result = run(tmp_path, reference)
    assert (result.record.status, result.record.upserted["summaries"].status) == ("partial", "failed")
    reason, fix = runs.failure_of(result.record, publish=True) or ("", "")
    assert reason.startswith(f"run {result.record.run_id} of {result.record.job} published, and could not upsert summaries: ")
    assert fix == f"fix it, then `{runs.resume_command(result.record)}`, which upserts what it has not"
    assert result.snapshot is not None
    resumed = runs.resume(JOB, result.snapshot, GivenSettings(), clock=clock, faked={"reference": reference})
    assert (resumed.status, resumed.upserted["summaries"].status, len(reference.collections["summaries"])) == ("published", "upserted", 2)


def test_a_json_column_holds_only_objects_and_arrays() -> None:
    frame = pd.DataFrame({"cusip": ["A", "B"], "coupon_bp": [1.0, 2.0], "terms": ['{"a":1}', "3"]})
    with pytest.raises(SchemaError, match=r"terms: row 1's value is JSON int, where a JSON column holds an object or an array: write it with etl.json_text"):
        conform(frame, Summary, what="summaries", naive="reject")
    assert etl.json_text({"coupon": 0.045, "dates": ["2026-11-05"]}) == '{"coupon":0.045,"dates":["2026-11-05"]}'


def test_a_json_column_is_sql_server_s_json_type_and_never_a_key() -> None:
    target = etl.Table(Summary, database="warehouse", table="summaries", write=etl.Upsert(), key=("cusip",)).target
    assert "    terms JSON,  -- JSON text: SQL Server 2025 or Azure SQL\n" in ddl.create_table(target, "mssql")
    assert "    terms CLOB,  -- JSON text: SQL Server 2025 or Azure SQL\n" in ddl.create_table(target, "oracle")
    with pytest.raises(ValueError, match="its key's columns may not be JSON"):
        etl.Table(Summary, database="warehouse", table="summaries", write=etl.Upsert(), key=("terms",))
    with pytest.raises(ValueError, match=r"ReplaceWhere\(matching=...\) columns may not be JSON"):
        etl.Table(Summary, database="warehouse", table="summaries", write=etl.ReplaceWhere(matching=("terms",)))
    spec = TableSpec(
        "mssql",
        "summaries",
        {
            "cusip": DestColumn("cusip", "NVARCHAR", 9, None, None, nullable=False, generated=False, defaulted=False),
            "coupon_bp": DestColumn("coupon_bp", "FLOAT", None, 53, None, nullable=False, generated=False, defaulted=False),
            "terms": DestColumn("terms", "JSON", None, None, None, nullable=True, generated=False, defaulted=False),
        },
    )
    assert check_table(target, spec) == []
    plain = etl.Table(Bond, database="warehouse", table="bonds", write=etl.Append()).target
    as_text = TableSpec(
        "mssql", "bonds", {name: dataclasses.replace(spec.columns[like], name=name) for name, like in (("_id", "cusip"), ("cusip", "cusip"), ("coupon", "coupon_bp"), ("terms", "terms"))}
    )
    assert check_table(plain, as_text) == []
    untyped = TableSpec("mssql", "summaries", {**spec.columns, "cusip": DestColumn("cusip", "JSON", None, None, None, nullable=False, generated=False, defaulted=False)})
    assert check_table(target, untyped) == ["cusip: a JSON column holds JSON text, objects and arrays: declare the row's column Annotated[str, etl.Json()]"]


def test_an_upsert_into_a_collection_with_no_unique_index_on_its_key_is_refused_before_it_reads(tmp_path: Path, reference: fakes.Mongo) -> None:
    reference.unique.clear()
    result = run(tmp_path, reference)
    assert (result.record.status, result.record.inputs) == ("failed", {})
    assert result.record.reasons == [
        "summaries: summaries has no unique index on cusip, so two runs at once could each insert a document for one key: create one, as db.summaries.createIndex({cusip: 1}, {unique: true})"
    ]


def test_a_field_the_collection_names_in_another_case_is_refused_and_fields_names_it(tmp_path: Path, reference: fakes.Mongo) -> None:
    reference.collections["summaries"] = [{"Cusip": "912810TM6", "coupon_bp": 1.0, "terms": None}]
    refused = run(tmp_path, reference)
    assert refused.record.reasons == ["summaries: summaries names its field Cusip, and the upsert's cusip would be a second field beside it: name it so, as fields={'cusip': 'Cusip'}"]

    class Named(etl.Outputs):
        summaries = etl.MongoUpsert(Summary, mongo="reference", collection="summaries", key=("cusip",), fields={"cusip": "Cusip"})

    reference.unique["summaries"] = [("Cusip",)]
    named = dataclasses.replace(JOB, outputs=Named, transform=lambda inputs, params: Named(summaries=transform(inputs, params).summaries))
    request = runs.Request(params_file(tmp_path), DAY, publish=True)
    result = runs.run(named, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock, faked={"reference": reference})
    assert result.record.status == "published", result.record
    assert sorted(str(document["Cusip"]) for document in reference.collections["summaries"]) == ["912810TM6", "912810TN4"]
    assert all("cusip" not in document for document in reference.collections["summaries"])
