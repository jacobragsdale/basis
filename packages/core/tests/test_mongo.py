"""Mongo as a part reads and upserts it: documents as a row's text, frames as documents, an upsert that a second time
changes nothing, and pymongo's failures as the kinds of `ServiceError`; and JSON text as a JSON column takes it."""

import math
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from bson import Decimal128, ObjectId
from pymongo import errors

from basis.core import fakes, jsontext, mongo
from basis.core.errors import Conflict, Denied, Failed, Rejected, Timeout, Unavailable
from basis.core.settings import GivenSettings, SettingsError
from basis.core.tables import Column

COLUMNS = (
    Column("_id", "str", nullable=False),
    Column("accountid", "str", nullable=False),
    Column("quantity", "float", nullable=True),
    Column("booked_at", "datetime", nullable=True, zone="UTC"),
    Column("terms", "str", nullable=True, json=True),
)
OID = ObjectId("65f0c0ffee0000000000abcd")


def test_documents_read_as_their_row_s_columns_each_value_its_text() -> None:
    documents: list[mongo.Document] = [
        {"_id": OID, "accountId": "001234", "quantity": 100.0, "booked_at": datetime(2026, 11, 5, 14, 30, tzinfo=UTC), "terms": {"coupon": 0.045, "dates": [OID]}, "extra": 1},
        {"_id": "x2", "accountId": "9", "quantity": Decimal128("2.675")},
    ]
    assert mongo.frame_of(documents, COLUMNS).to_dict("records") == [
        {"_id": "65f0c0ffee0000000000abcd", "accountid": "001234", "quantity": "100", "booked_at": "2026-11-05T14:30:00+00:00", "terms": '{"coupon":0.045,"dates":["65f0c0ffee0000000000abcd"]}'},
        {"_id": "x2", "accountid": "9", "quantity": "2.675", "booked_at": None, "terms": None},
    ]


def test_a_frame_becomes_documents_its_json_objects_and_its_dates_text() -> None:
    frame = pd.DataFrame(
        {
            "key": ["a"],
            "value": [math.nan],
            "day": pd.Series([date(2026, 11, 5)], dtype="date32[pyarrow]"),
            "at": pd.Series([datetime(2026, 11, 5, 14, 30, tzinfo=UTC)], dtype="timestamp[us, UTC][pyarrow]"),
            "terms": ['{"coupon":0.045}'],
        }
    )
    columns = (
        Column("key", "str", nullable=False),
        Column("value", "float", nullable=True),
        Column("day", "date", nullable=False),
        Column("at", "datetime", nullable=False, zone="UTC"),
        Column("terms", "str", nullable=False, json=True),
    )
    assert mongo.documents_of(frame, columns) == [{"key": "a", "value": None, "day": "2026-11-05", "at": datetime(2026, 11, 5, 14, 30, tzinfo=UTC), "terms": {"coupon": 0.045}}]


def test_a_second_upsert_of_the_same_documents_matches_each_and_modifies_none() -> None:
    store = fakes.Mongo()
    documents: list[dict[str, object]] = [{"key": "a", "value": 1.0}, {"key": "b", "value": 2.0}]
    assert store.upsert("positions", ("key",), documents) == mongo.Upserted(matched=0, modified=0, upserted=2)
    assert store.upsert("positions", ("key",), documents) == mongo.Upserted(matched=2, modified=0, upserted=0)
    assert store.upsert("positions", ("key",), [{"key": "a", "value": 3.0}]) == mongo.Upserted(matched=1, modified=1, upserted=0)
    assert store.collections["positions"] == [{"key": "a", "value": 3.0}, {"key": "b", "value": 2.0}]


def test_a_fake_s_query_matches_as_mongo_s_comparisons_do_and_a_read_past_its_limit_is_refused() -> None:
    store = fakes.Mongo()
    store.collections["trades"] = [{"at": datetime(2026, 11, 5, hour, tzinfo=UTC), "desk": desk} for hour, desk in ((1, "a"), (5, "b"), (9, "a"))]
    query = {"at": {"$gte": datetime(2026, 11, 5, 2, tzinfo=UTC), "$lt": datetime(2026, 11, 6, tzinfo=UTC)}, "desk": "a"}
    assert [document["at"].hour for document in mongo.read(store, "trades", query, max_documents=5, name="trading")] == [9]  # ty: ignore[unresolved-attribute] -- each document's at is a datetime here
    with pytest.raises(mongo.TooManyDocumentsError, match=r"trading\.trades returned more than 2 documents"):
        mongo.read(store, "trades", {}, max_documents=2, name="trading")


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (errors.ServerSelectionTimeoutError("no servers"), Unavailable),
        (errors.AutoReconnect("reset"), Unavailable),
        (errors.NetworkTimeout("slow"), Timeout),
        (errors.ExecutionTimeout("maxTimeMS"), Timeout),
        (errors.DuplicateKeyError("E11000"), Conflict),
        (errors.OperationFailure("not authorized", code=13), Denied),
        (errors.BulkWriteError({"writeErrors": [{"code": 121}]}), Rejected),
        (errors.InvalidOperation("closed"), Failed),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value.__name__,
)
def test_pymongo_s_failures_are_the_kinds_of_service_error(error: Exception, kind: type[Exception]) -> None:
    failure = mongo._failure("trading", error)  # noqa: SLF001 -- each pymongo error as a store would meet it
    assert type(failure) is kind


def test_a_mongo_database_the_settings_do_not_configure_is_refused_naming_its_settings() -> None:
    with pytest.raises(SettingsError, match="set BASIS_MONGO__TRADING__URI, BASIS_MONGO__TRADING__DATABASE"):
        mongo.store("trading", GivenSettings(), tracer=None)  # ty: ignore[invalid-argument-type] -- refused before it is used


@pytest.mark.parametrize(("value", "text"), [(1e-07, "0.0000001"), (1.5e20, "150000000000000000000"), (2.675, "2.675"), (100.0, "100"), (-0.5, "-0.5"), (-0.0, "0"), (123456.789, "123456.789")])
def test_a_json_number_is_written_in_plain_notation(value: float, text: str) -> None:
    assert jsontext.plain(value) == text


def test_json_text_is_an_object_or_an_array_of_what_a_json_column_holds() -> None:
    assert jsontext.dumps({"a": 1, "b": [1.5, None, True], "c": "é😀", "d": date(2026, 11, 5)}) == '{"a":1,"b":[1.5,null,true],"c":"é😀","d":"2026-11-05"}'
    for refused, says in [(1, "holds an object or an array"), ({"x": math.nan}, "no JSON number"), ({"x": 1e40}, "more than the 38 digits"), ({"x": datetime(2026, 11, 5)}, "has no zone")]:
        with pytest.raises(jsontext.JsonTextError, match=says):
            jsontext.dumps(refused)
    assert [jsontext.problem(text) for text in ('{"a":1}', "[1]")] == [None, None]
    assert [jsontext.problem(text) for text in ("1", "null", "", '{"a": NaN}')] == [
        "JSON int, where a JSON column holds an object or an array",
        "JSON NoneType, where a JSON column holds an object or an array",
        "not JSON (Expecting value: line 1 column 1 (char 0))",
        "not JSON (NaN is no JSON number)",
    ]


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ({"terms.frequency": 2}, ["b1"]),
        ({"tags": "callable"}, ["b1"]),
        ({"tags": {"$in": ["green", "callable"]}}, ["b1", "b2"]),
        ({"tags": {"$ne": "callable"}}, ["b2", "b3"]),
        ({"$or": [{"cusip": "OLD"}, {"terms.frequency": 2}]}, ["b1", "b3"]),
        ({"$and": [{"coupon": {"$gte": 0.04}}, {"tags": "green"}]}, ["b2"]),
        ({"terms.frequency": {"$gt": 1}}, ["b1", "b2"]),
    ],
    ids=["dotted-path", "array-member", "array-in", "array-ne", "or", "and", "dotted-compare"],
)
def test_the_fake_matches_what_a_jobs_filters_ask_of_mongo(query: dict[str, object], found: list[str]) -> None:
    store = fakes.Mongo()
    store.collections["bonds"] = [
        {"_id": "b1", "cusip": "912810TM6", "coupon": 0.045, "terms": {"frequency": 2}, "tags": ["callable", "long"]},
        {"_id": "b2", "cusip": "912810TN4", "coupon": 0.0425, "terms": {"frequency": 4}, "tags": ["green"]},
        {"_id": "b3", "cusip": "OLD", "coupon": 0.01},
    ]
    assert [document["_id"] for document in store.find("bonds", query, limit=10)] == found
