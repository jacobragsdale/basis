"""Step 5.5's done criterion: a job that reads Mongo publishes to a SQL Server `json` column, upserts documents back
into Mongo, and a second run's upsert of the same documents changes nothing there."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pytest
from integration.scratch import Scratch, configured, missing, mssql
from opentelemetry import trace

import basis.job as etl
from basis.core import fakes, mongo
from basis.core.settings import GivenSettings, MongoSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.job import Job

pytestmark = pytest.mark.integration


class Bond(etl.Row):
    cusip: Annotated[str, etl.MaxLength(9)]
    coupon: float
    terms: Annotated[str, etl.Json()] | None


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    found = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    [(version, json_type)] = found.rows("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)), TYPE_ID('json')")
    if json_type is None:
        found.drop(found.ledger())
        missing(f"SQL Server {version} has no json type, which SQL Server 2025 (17.x) added")
    yield found
    found.drop(found.ledger(), f"bonds_{found.suffix}")


@pytest.fixture
def mongo_uri() -> str:
    if (uri := configured().mongo) is None:
        missing("set BASIS_TEST_MONGO to a scratch Mongo server (see tests/integration/README.md)")
    return uri.get_secret_value()


def test_a_job_reads_mongo_publishes_json_to_sql_server_and_upserts_back_once(scratch: Scratch, mongo_uri: str, tmp_path: Path) -> None:
    from pymongo import MongoClient  # noqa: PLC0415 -- basis-core[mongo]'s, which this tier alone needs

    database, table = f"basis_it_{scratch.suffix}", f"bonds_{scratch.suffix}"
    client: MongoClient[dict[str, object]] = MongoClient(mongo_uri, tz_aware=True)
    client[database]["bonds"].insert_many(
        [
            {"cusip": "912810TM6", "coupon": 0.045, "updated": datetime(2026, 11, 5, 9, tzinfo=UTC), "terms": {"frequency": 2, "notes": ["é😀"]}},
            {"cusip": "912810TN4", "coupon": 0.0425, "updated": datetime(2026, 11, 5, 10, tzinfo=UTC), "terms": None},
        ]
    )
    client[database]["mirror"].create_index("cusip", unique=True)  # which an upsert's key needs

    class Inputs(etl.Inputs):
        bonds = etl.Mongo(Bond, mongo="reference", collection="bonds", filter={"updated": {"$gte": "{start}", "$lt": "{end}"}}, empty="fail")

    class Outputs(etl.Outputs):
        warehouse = etl.Table(Bond, database="warehouse", table=table, write=etl.Upsert(), key=("cusip",))
        mirror = etl.MongoUpsert(Bond, mongo="reference", collection="mirror", key=("cusip",))

    def transform(inputs: Inputs, params: etl.Params) -> Outputs:
        del params
        return Outputs(warehouse=inputs.bonds, mirror=inputs.bonds)

    job = Job("bonds", "tests.bonds", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)
    scratch.execute(ddl.create_table(Outputs.warehouse.target, "mssql"))
    settings = GivenSettings.model_validate({"databases": {"warehouse": scratch.settings}, "mongo": {"reference": {"uri": mongo_uri, "database": database, "timeout_s": 30, "max_documents": 1000}}})
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    request = runs.Request(tmp_path / "params.json", etl.Interval.parse("2026-11-05..2026-11-06"), publish=True)
    try:
        first = runs.run(job, settings, request, snapshot_root=Root.at(tmp_path / "snapshots"))
        assert first.record.status == "published", first.record
        assert (first.record.upserted["mirror"].upserted, first.record.upserted["mirror"].modified) == (2, 0)
        rows = scratch.rows(f"SELECT cusip, JSON_VALUE(terms, '$.frequency'), JSON_VALUE(terms, '$.notes[0]') FROM {table} ORDER BY cusip")
        assert rows == [("912810TM6", "2", "é😀"), ("912810TN4", None, None)]
        mirrored = sorted(client[database]["mirror"].find({}, {"_id": 0}), key=lambda document: str(document["cusip"]))
        assert mirrored == [{"cusip": "912810TM6", "coupon": 0.045, "terms": {"frequency": 2, "notes": ["é😀"]}}, {"cusip": "912810TN4", "coupon": 0.0425, "terms": None}]

        second = runs.run(job, settings, request, snapshot_root=Root.at(tmp_path / "snapshots"))
        assert second.record.status == "published", second.record
        counts = second.record.upserted["mirror"]
        assert (counts.matched, counts.modified, counts.upserted) == (2, 0, 0)
        assert len(scratch.rows(f"SELECT cusip FROM {table}")) == 2
    finally:
        client.drop_database(database)
        client.close()


QUERIES = [
    ({"terms.frequency": 2}, ["b1"]),
    ({"tags": "callable"}, ["b1"]),
    ({"tags": {"$in": ["green", "callable"]}}, ["b1", "b2"]),
    ({"tags": {"$ne": "callable"}}, ["b2", "b3"]),
    ({"$or": [{"cusip": "OLD"}, {"terms.frequency": 2}]}, ["b1", "b3"]),
    ({"$and": [{"coupon": {"$gte": 0.04}}, {"tags": "green"}]}, ["b2"]),
    ({"terms.frequency": {"$gt": 1}}, ["b1", "b2"]),
]
BONDS: list[dict[str, object]] = [
    {"_id": "b1", "cusip": "912810TM6", "coupon": 0.045, "terms": {"frequency": 2}, "tags": ["callable", "long"]},
    {"_id": "b2", "cusip": "912810TN4", "coupon": 0.0425, "terms": {"frequency": 4}, "tags": ["green"]},
    {"_id": "b3", "cusip": "OLD", "coupon": 0.01},
]


def test_real_mongo_and_its_fake_match_the_same_documents_and_name_the_same_unique_keys(mongo_uri: str) -> None:
    from pymongo import MongoClient  # noqa: PLC0415 -- as above

    database = f"basis_it_{uuid.uuid4().hex[:8]}"
    client: MongoClient[dict[str, object]] = MongoClient(mongo_uri)
    client[database]["bonds"].insert_many([dict(bond) for bond in BONDS])
    client[database]["bonds"].create_index("cusip", unique=True)
    fake = fakes.Mongo()
    fake.collections["bonds"] = [dict(bond) for bond in BONDS]
    fake.unique["bonds"] = [("cusip",)]
    real = mongo.MongoStore("reference", MongoSettings.model_validate({"uri": mongo_uri, "database": database, "timeout_s": 30, "max_documents": 100}), trace.NoOpTracer())
    try:
        for query, found in QUERIES:
            assert sorted(str(document["_id"]) for document in real.find("bonds", query, limit=10)) == found, query
            assert sorted(str(document["_id"]) for document in fake.find("bonds", query, limit=10)) == found, query
        assert sorted(real.unique_keys("bonds")) == sorted(fake.unique_keys("bonds")) == [("_id",), ("cusip",)]
        assert real.unique_keys("not_made_yet") == []
    finally:
        real.close()
        client.drop_database(database)
        client.close()
