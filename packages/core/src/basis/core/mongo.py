"""Mongo, which the firm uses and is moving to SQL Server's `json` columns: a part reads documents from a collection,
and writes them as upserts by a declared key, so a rerun is safe, with no transactions and no ledger in Mongo. Each
database is named, as `BASIS_MONGO__NAME__URI`, `__DATABASE`, `__TIMEOUT_S` and `__MAX_DOCUMENTS` configure it, and
reached through pymongo, which a repo takes as `basis-core[mongo]`.

A read's documents become a row's columns by their field names, matched without regard to case, as `accountId` gives
the column `accountid`; a field the row has no column for is left out, and one a document lacks is null. Each value is
read as its text, as a CSV's is, for the row to type: an `ObjectId` as its hex, a date as its ISO text in UTC, a
`Decimal128` as its digits, a whole number as its digits, and an object or an array as JSON text
(`basis.core.jsontext`), which a column declared `etl.Json()` keeps. An upsert's rows become documents of the row's
columns: each JSON column's value its object or array, a datetime a BSON date, a date its `YYYY-MM-DD` text, since
BSON has no date, and null where a value is missing. An upsert sets each document's fields, by its key's, inserting it
where none matches, so a second upsert of the same documents matches each and modifies none.

Every read and upsert has a span of its own, and the database's `timeout_s` as its deadline, and fails as one of
`basis.core.errors`' kinds, real or fake (`basis.core.fakes.Mongo`)."""

import json
import math
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Protocol

import pandas as pd
from opentelemetry.trace import SpanKind, Tracer

from basis.core import deadlines, jsontext
from basis.core.errors import BasisError, Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.settings import MongoSettings, Settings, SettingsError, variable
from basis.core.tables import Column

type Document = dict[str, object]
_EXTRA = "add basis-core[mongo] to the repo's dependencies, as `uv add 'basis-core[mongo]'`, which brings pymongo"
_DENIED = frozenset({13, 18})
"""Mongo's codes for an unauthorized command and a failed login."""


class TooManyDocumentsError(BasisError, ValueError):
    """A read that returned more documents than its database's `max_documents`."""


@dataclass(frozen=True)
class Upserted:
    """What an upsert did, as Mongo counted it."""

    matched: int = 0
    """Documents whose key matched one there."""
    modified: int = 0
    """Of those, the ones it changed."""
    upserted: int = 0
    """Documents it inserted, since none matched their key."""


class Store(Protocol):
    """A Mongo database, real or fake."""

    def find(self, collection: str, query: Mapping[str, object], *, limit: int) -> list[Document]:
        """The documents of `collection` that `query` matches, at most `limit`."""
        ...

    def upsert(self, collection: str, key: Sequence[str], documents: Sequence[Document]) -> Upserted:
        """Set each document's fields in `collection`, matched by its `key` fields, inserting it where none matches."""
        ...

    def unique_keys(self, collection: str) -> list[tuple[str, ...]]:
        """The fields of each unique index `collection` has, each in its order; none for a collection not made yet."""
        ...


def refusals(found: Store, collection: str, key: Sequence[str], fields: Sequence[str]) -> list[str]:
    """Why upserting documents of `fields`, matched by `key`, into `collection` would write what a read would not find
    again: a field the collection's documents name in another case, which an upsert would add beside it, and no
    unique index on the key, without which two runs at once could each insert a document for one key."""
    sample = found.find(collection, {}, limit=1)
    named = {str(field).lower(): str(field) for document in sample for field in document}
    reasons = [
        f"{collection} names its field {there}, and the upsert's {field} would be a second field beside it: name it so, as fields={{{field!r}: {there!r}}}"
        for field in fields
        if (there := named.get(field.lower())) is not None and there != field
    ]
    if set(key) not in [set(index) for index in found.unique_keys(collection)]:
        spec = ", ".join(f"{field}: 1" for field in key)
        create = f"db.{collection}.createIndex({{{spec}}}, {{unique: true}})"
        reasons.append(f"{collection} has no unique index on {', '.join(key)}, so two runs at once could each insert a document for one key: create one, as {create}")
    return reasons


class MongoStore:
    """A Mongo database as pymongo reaches it, from its settings alone."""

    def __init__(self, name: str, settings: MongoSettings, tracer: Tracer) -> None:
        try:
            import pymongo  # noqa: PLC0415 -- an optional extra, which only a repo that reads Mongo installs
        except ImportError as error:
            msg = f"reading or writing Mongo needs pymongo, which is not installed: {_EXTRA}"
            raise BasisError(msg) from error
        self.name = name
        self.settings = settings
        self._tracer = tracer
        timeout_ms = int(settings.timeout_s * 1000)
        uri = settings.uri.get_secret_value()
        if settings.ca_bundle is not None:
            self._client = pymongo.MongoClient(uri, tz_aware=True, tzinfo=UTC, timeoutMS=timeout_ms, serverSelectionTimeoutMS=timeout_ms, appname="basis", tlsCAFile=str(settings.ca_bundle))
        else:
            self._client = pymongo.MongoClient(uri, tz_aware=True, tzinfo=UTC, timeoutMS=timeout_ms, serverSelectionTimeoutMS=timeout_ms, appname="basis")
        self._database = self._client[settings.database]

    def find(self, collection: str, query: Mapping[str, object], *, limit: int) -> list[Document]:
        with self._tracer.start_as_current_span(f"{self.name} find", kind=SpanKind.CLIENT, attributes={"db.system": "mongodb", "db.collection.name": collection}) as span:
            try:
                with self._within():
                    found: list[Document] = [{str(field): value for field, value in document.items()} for document in self._database[collection].find(dict(query), limit=limit)]
            except ServiceError:
                raise
            except Exception as error:
                raise _failure(self.name, error) from error
            span.set_attribute("db.response.returned_rows", len(found))
        return found

    def upsert(self, collection: str, key: Sequence[str], documents: Sequence[Document]) -> Upserted:
        from pymongo import UpdateOne  # noqa: PLC0415 -- the optional extra, as above

        if not documents:
            return Upserted()
        requests = [UpdateOne({field: document[field] for field in key}, {"$set": document}, upsert=True) for document in documents]
        with self._tracer.start_as_current_span(f"{self.name} upsert", kind=SpanKind.CLIENT, attributes={"db.system": "mongodb", "db.collection.name": collection}) as span:
            try:
                with self._within():
                    result = self._database[collection].bulk_write(requests, ordered=False)
            except ServiceError:
                raise
            except Exception as error:
                raise _failure(self.name, error) from error
            span.set_attribute("db.response.returned_rows", len(documents))
        return Upserted(result.matched_count, result.modified_count, result.upserted_count)

    def unique_keys(self, collection: str) -> list[tuple[str, ...]]:
        with self._tracer.start_as_current_span(f"{self.name} indexes", kind=SpanKind.CLIENT, attributes={"db.system": "mongodb", "db.collection.name": collection}):
            try:
                with self._within():
                    indexes = self._database[collection].index_information()
            except ServiceError:
                raise
            except Exception as error:
                raise _failure(self.name, error) from error
        return [tuple(str(field) for field, _ in index["key"]) for index in indexes.values() if index.get("unique") or index.get("key") == [("_id", 1)]]

    @contextmanager
    def _within(self) -> Generator[None]:
        """A call within the shorter of the database's timeout and what is left of the deadline around it, as
        pymongo's client-side timeout bounds every operation in its block."""
        import pymongo  # noqa: PLC0415 -- the optional extra, as above

        limit = deadlines.limit(self.name, self.settings.timeout_s)
        if limit.seconds <= 0:
            raise limit.exceeded()
        with pymongo.timeout(limit.seconds):
            yield

    def ping(self) -> None:
        """Whether the database answers, reading nothing: what `basis doctor` checks."""
        with self._tracer.start_as_current_span(f"{self.name} ping", kind=SpanKind.CLIENT, attributes={"db.system": "mongodb"}):
            try:
                with self._within():
                    self._database.command("ping")
            except ServiceError:
                raise
            except Exception as error:
                raise _failure(self.name, error) from error

    def close(self) -> None:
        self._client.close()


def _failure(name: str, error: Exception) -> Exception:
    """The kind of failure a pymongo error is, or the error itself where it is no pymongo error."""
    from pymongo import errors  # noqa: PLC0415 -- the optional extra, as above

    reason = " ".join(str(error).split())[:500]
    kind: type[ServiceError] | None = None
    if isinstance(error, errors.ServerSelectionTimeoutError):  # no server answered: it cannot be reached, though pymongo calls it a timeout
        kind, detail = Unavailable, f"{name} could not be reached"
    elif isinstance(error, errors.ExecutionTimeout | errors.NetworkTimeout | errors.WTimeoutError) or (isinstance(error, errors.PyMongoError) and error.timeout):
        kind, detail = Timeout, f"{name} did not answer within its timeout"
    elif isinstance(error, errors.DuplicateKeyError):
        kind, detail = Conflict, f"{name} refused a document for a unique key"
    elif isinstance(error, errors.OperationFailure) and error.code in _DENIED:
        kind, detail = Denied, f"{name} refused the login or the command"
    elif isinstance(error, errors.ConnectionFailure):
        kind, detail = Unavailable, f"{name} could not be reached"
    elif isinstance(error, errors.BulkWriteError | errors.WriteError):
        kind, detail = Rejected, f"{name} refused a document"
    else:
        detail = f"{name} failed"
        kind = Failed if isinstance(error, errors.PyMongoError) else None
    return kind(detail, resource=name, reason=reason) if kind is not None else error


def store(name: str, settings: Settings, tracer: Tracer) -> MongoStore:
    """The Mongo database the settings name `name`."""
    if (found := settings.mongo.get(name)) is None:
        names = ", ".join(variable(("mongo", name, part)) for part in ("uri", "database", "timeout_s", "max_documents"))
        msg = f"the Mongo database {name} is not configured, and the run reads or writes it: set {names}, as .env.example shows"
        raise SettingsError(msg)
    return MongoStore(name, found, tracer)


def read(found: Store, collection: str, query: Mapping[str, object], *, max_documents: int, name: str) -> list[Document]:
    """The documents `query` matches, refused past `max_documents`."""
    documents = found.find(collection, query, limit=max_documents + 1)  # one past the limit, to tell a read that passes it
    if len(documents) > max_documents:
        msg = f"{name}.{collection} returned more than {max_documents} documents, the database's max_documents: is a filter missing, or does the limit need raising?"
        raise TooManyDocumentsError(msg)
    return documents


def frame_of(documents: Sequence[Document], columns: Sequence[Column]) -> pd.DataFrame:
    """The documents as the row's columns, each value its text, as the module's docstring says."""
    texts: dict[str, list[str | None]] = {column.name: [] for column in columns}
    for document in documents:
        by_name = {str(field).lower(): value for field, value in document.items()}
        for column in columns:
            texts[column.name].append(value_text(by_name.get(column.name)))
    return pd.DataFrame({name: pd.Series(values, dtype="string[pyarrow]") for name, values in texts.items()}, columns=[column.name for column in columns])


def value_text(value: object) -> str | None:  # noqa: PLR0911 -- one case per kind of BSON value
    """A document's value as the text a CSV would hold, or None for a missing one."""
    match value:
        case None:
            return None
        case str():
            return value
        case bool():
            return "true" if value else "false"
        case datetime():
            return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).isoformat()
        case float() if math.isnan(value):
            return None
        case float() if value.is_integer() and abs(value) < 2**53:
            return str(int(value))
        case float() | int():
            return repr(value)
        case Mapping() | list() | tuple():
            return jsontext.dumps(_plain(value))
        case _:
            return _bson_text(value)


def _bson_text(value: object) -> str:
    """An `ObjectId`, a `Decimal128` or any other BSON value, as its own text."""
    to_decimal = getattr(value, "to_decimal", None)
    return str(to_decimal()) if callable(to_decimal) else str(value)


def _plain(value: object) -> object:
    """A nested value with each BSON value JSON can hold as text: an `ObjectId` and a `Decimal128` their text."""
    match value:
        case Mapping():
            return {str(field): _plain(item) for field, item in value.items()}
        case list() | tuple():
            return [_plain(item) for item in value]
        case None | str() | bool() | int() | float() | datetime() | date():
            return value
        case _:
            return _bson_text(value)


def documents_of(frame: pd.DataFrame, columns: Sequence[Column], fields: Mapping[str, str] | None = None) -> list[Document]:
    """A frame of the row's columns as documents, as the module's docstring says, each column its field of `fields`'s
    name, or its own."""
    names = fields or {}
    found: list[Document] = [{} for _ in range(len(frame))]
    for column in columns:
        series = frame[column.name]
        for document, value in zip(found, series.tolist(), strict=True):
            document[names.get(column.name, column.name)] = _document_value(value, column)
    return found


def _document_value(value: object, column: Column) -> object:
    found: object = value
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        found = None
    elif column.json and isinstance(value, str):
        found = json.loads(value)
    elif isinstance(value, pd.Timestamp):
        found = value.to_pydatetime()
    elif isinstance(value, date | time) and not isinstance(value, datetime):
        found = value.isoformat()
    return found
