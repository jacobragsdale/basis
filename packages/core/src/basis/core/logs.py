"""Logging. Each record is one JSON object per line on stderr, for agents and log platforms; `BASIS_LOG_FORMAT=text`
prints lines for a person instead. INFO tells a run's story: what loaded, what the transform made, every check that
failed, what was written to each database, and one summary record at the end, with timings in `*_ms` fields.

To log, use stdlib logging and put data in `extra`. Each key becomes a JSON field, and frames, series, numpy values,
dates and pydantic models serialize as they are:

    log = logging.getLogger(__name__)
    log.debug("rule applied", extra={"rule": rule, "dropped": 12})

Keys that clash with `LogRecord` attributes (`name`, `module`, `filename`, ...) raise. Keep records small: container
platforms split long lines, so bulk data belongs in the run's snapshot, which the logs point to. To time a block, use
`timed`.

Work that shares its process with other work, such as an API's request, adds its fields with `fields(request_id=...)`
instead of `bind`: they hold in its own context alone, so two requests served at once each log their own.

Every JSON record logged inside a trace, a run's or a request's, carries the trace's id and its current span's, from
`trace_fields`, so that Datadog links the record to the span it was logged in.

A run binds its id and its job first, with `bind(run_id=..., job=...)`, so every record carries them and filtering on
`run_id` finds a whole run. A job logs with `logging.getLogger(__name__)`; `include` gives its package's loggers the
configured level, as basis's have.

Exceptions need nothing: the command prints each as one line, with the notes added on the way up, and logs a defect's
record once, with its root cause and the line in basis where it surfaced. Where a frame knows what the message does
not, such as which dataset was loading, it adds that with `error.add_note(...)`.
"""

import importlib.util
import logging
import time
import traceback
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal, override

from opentelemetry import trace
from opentelemetry.trace import SpanContext
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.core import telemetry

_FRAMEWORK = "basis"  # the namespace every basis package is a portion of, whose logger is each one's parent
_GROUP = "basis.verbs"  # the entry points of the parts whose frames an error's `where` names, as basis-core's (`basis.core.verbs.GROUP`)
_PREFIX = "BASIS_LOG_"
_STANDARD = {*vars(logging.makeLogRecord({})), "message", "asctime"}  # LogRecord's own attributes, not `extra` fields
_bound: dict[str, object] = {}  # fields on every record this process logs; see `bind`
_scoped: ContextVar[Mapping[str, object]] = ContextVar("basis.core.logs.fields")  # fields in one context; see `fields`
_included: list[str] = []  # packages besides basis whose loggers log at the configured level; see `include`


class LogSettings(BaseSettings):
    """Read on its own, before `Settings`, so that a bad environment is logged like any other error."""

    model_config = SettingsConfigDict(env_prefix=_PREFIX, extra="forbid", frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] | None = None
    """None: the caller's default, JSON for the command line and text for a run from Python."""


def variable(location: Sequence[int | str]) -> str:
    """The environment variable that sets the value at `location` in `LogSettings`, from a validation error."""
    return _PREFIX + "__".join(str(part) for part in location).upper()


def setup(*, default_format: Literal["json", "text"] = "json") -> None:
    """Configure this process. basis's loggers log at the configured level; libraries log only warnings and
    up. Bad `BASIS_LOG_*` settings raise after configuring the defaults, so that the error is logged like any other."""
    try:
        settings = LogSettings()
    except ValidationError:
        _configure("INFO", default_format)
        raise
    _configure(settings.level, settings.format if settings.format is not None else default_format)


def _configure(level: str, format_name: Literal["json", "text"]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if format_name == "json" else TextFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.captureWarnings(True)  # noqa: FBT003 -- the stdlib takes it positionally; library warnings become log records
    for package in (_FRAMEWORK, *_included):
        logging.getLogger(package).setLevel(level)


def include(package: str) -> None:
    """Log `package`'s records at the configured level, as basis's are, rather than warnings only, as a
    library's are: a job's package."""
    if package not in _included:
        _included.append(package)
    logging.getLogger(package).setLevel(logging.getLogger(_FRAMEWORK).level)


def bind(**fields: object) -> None:
    """Add `fields` to every record this process logs from now on."""
    # process-wide, as the CLI runs one job per process; concurrent work in one process adds its fields with `fields`
    _bound.update(fields)


def bound() -> dict[str, object]:
    """The fields a record logged here now carries, `bind`'s, those of every `fields` block it is in and the current
    span's, for a worker process to bind in its own: a spawned process starts with none."""
    return _bound | dict(_scoped.get({})) | _current()


def trace_fields(context: SpanContext) -> dict[str, str]:
    """The fields that link a record to the span it was logged in, as Datadog reads them in a JSON record: the trace's
    id as 32 lower-case hexadecimal digits, and the span's as 16 (docs/spikes/datadog.md)."""
    return {"trace_id": trace.format_trace_id(context.trace_id), "span_id": trace.format_span_id(context.span_id)}


def trace_id() -> str | None:
    """The current trace's id, as `trace_fields` gives it, or None outside every trace."""
    return _current().get("trace_id")


def _current() -> dict[str, str]:
    """The current span's `trace_fields`, or none outside every trace."""
    context = trace.get_current_span().get_span_context()
    return trace_fields(context) if context.is_valid else {}


def included() -> list[str]:
    """The packages `include` added, for a worker process to include in its own."""
    return list(_included)


@contextmanager
def fields(**added: object) -> Generator[None]:
    """Add `added` to every record logged in the block, in this context alone, so that two requests served at once each
    log their own. A task the block starts carries them, as does a worker thread that copies the context, as anyio's
    do; a plain `threading.Thread` does not."""
    token = _scoped.set({**_scoped.get({}), **added})
    try:
        yield
    finally:
        _scoped.reset(token)


@contextmanager
def timed(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: object) -> Generator[dict[str, object]]:
    """Log `event` when the block ends, with `fields`, any the block adds to the dict it gets, and `ms`. A block that
    raises logs nothing here: its exception tells that story."""
    start = time.perf_counter()
    yield fields
    logger.log(level, event, extra=fields | {"ms": ms_since(start)})


def brief(fields: Mapping[str, object]) -> dict[str, object]:
    """Fields for a record or a message: a long list as its size and first few, since platforms split long lines."""
    return {k: f"{len(v)} values: {', '.join(map(str, v[:_BRIEF]))}, ..." if isinstance(v, list | tuple) and len(v) > _BRIEF else v for k, v in fields.items()}


_BRIEF = 3  # values a long list keeps


def ms_since(start: float) -> float:
    """Milliseconds since a `time.perf_counter()` reading."""
    return round((time.perf_counter() - start) * 1000, 1)


def utc_now() -> datetime:
    """The clock that stamps a run's start, loads and finish, unless a test passes its own."""
    return datetime.now(UTC)


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            **_extras(record, traced=True),
        }
        if isinstance(record.exc_info, tuple) and (error := record.exc_info[1]) is not None:
            entry |= describe(error)
        return to_json(entry)


class TextFormatter(logging.Formatter):
    """For a person at a terminal: the message, its fields as JSON, then the traceback as Python prints it."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    @override
    def formatMessage(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        return super().formatMessage(record) + (f" {to_json(extras)}" if extras else "")


def describe(error: BaseException) -> dict[str, object]:
    """An exception as an agent needs it: what went wrong, the notes added on the way up, the exception at the bottom of
    the chain, the line in basis where it surfaced (even when a library raised it), and the full traceback."""
    chain = [error]
    while (cause := _cause(chain[-1])) is not None and cause not in chain:
        chain.append(cause)
    return {
        "error": _headline(error),
        "notes": [note for e in chain for note in getattr(e, "__notes__", [])],
        "root_cause": _headline(chain[-1]),
        "where": next((where for e in reversed(chain) if (where := _where(e)) is not None), None),
        "traceback": "".join(traceback.format_exception(error)),
    }


def to_json(value: object) -> str:
    """`value` as one line of JSON; a value pydantic's serializer cannot hold, as pandas' missing time `NaT` or bytes
    that are not UTF-8, as its text instead, so the record is never lost for a traceback of the logging's own."""
    try:
        return _JSON.dump_json(value, fallback=_fallback).decode()
    except ValueError, TypeError, UnicodeDecodeError, PydanticSerializationError:
        return _JSON.dump_json(_plain(value), fallback=_fallback).decode()


def _plain(value: object) -> object:
    """`value` with what JSON has a type for kept, and every other value its text: bytes as how many."""
    match value:
        case dict():
            return {str(key): _plain(item) for key, item in value.items()}
        case list() | tuple() | set() | frozenset():
            return [_plain(item) for item in value]
        case None | bool() | int() | float() | str():
            return value
        case bytes() | bytearray():
            return f"<{len(value)} bytes>"
        case _:
            return str(value)


_JSON = TypeAdapter(object, config=ConfigDict(ser_json_inf_nan="null"))  # NaN is not JSON: it logs as null


def _extras(record: logging.LogRecord, *, traced: bool = False) -> dict[str, object]:
    """A record's fields: `bind`'s, those of each `fields` block, where `traced` the current span's, and its own `extra`.
    The span is the one current where the record is formatted, which is where it was logged, since a handler formats
    it in the thread that logs it."""
    found = _bound | dict(_scoped.get({})) | (_current() if traced else {}) | {key: value for key, value in vars(record).items() if key not in _STANDARD}
    if "service" in found and (name := telemetry.service_name()) is not None:
        found["service"] = name  # the platform's name for the process, which Datadog's tags and metrics carry
    return found


def _fallback(value: object) -> object:
    """A value JSON has no type for. A frame is known by its shape, so that logging, which every command loads first,
    loads neither pandas nor numpy: a frame logs as its size and columns, a pandas series as its values by its index,
    and an index or a numpy value as its `tolist()`."""
    match value:
        case _ if isinstance(shape := getattr(value, "shape", None), tuple) and shape and (columns := getattr(value, "columns", None)) is not None:
            return {"rows": shape[0], "columns": [str(column) for column in columns]}  # a pandas frame
        case _ if callable(to_dict := getattr(value, "to_dict", None)) and getattr(value, "index", None) is not None:
            return to_dict()  # a pandas series, whose index says what each value is
        case _ if callable(tolist := getattr(value, "tolist", None)):
            return tolist()
        case _:
            return str(value)


def _cause(error: BaseException) -> BaseException | None:
    return error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def _headline(error: BaseException) -> str:
    kind = type(error)
    name = kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"
    return f"{name}: {error}"


def _where(error: BaseException) -> str | None:
    """The deepest frame of `error` in basis, in whichever of its packages, as `basis/job/module.py:line in function`,
    or `PACKAGE/module.py:line in function` for a part with a package of its own, outside the namespace."""
    folders = _folders()
    ours = [(frame, path, base) for frame in traceback.extract_tb(error.__traceback__) for folder, base in folders if (path := Path(frame.filename).resolve()).is_relative_to(folder)]
    if not ours:
        return None
    frame, path, base = ours[-1]
    return f"{path.relative_to(base)}:{frame.lineno} in {frame.name}"


@cache
def _folders() -> tuple[tuple[Path, Path], ...]:
    """The folder of each installed basis package, such as `.../basis/core`, with the folder its import name starts
    in: the packages themselves, rather than every `basis` folder on the path, which the namespace holds, the working
    directory's among them. A part is any package that lists an entry point in the `basis.verbs` group, whatever its
    name."""
    core = Path(__file__).resolve().parent
    found = [(core, core.parents[1])]
    for name in _parts():
        spec = importlib.util.find_spec(name)  # finds the package without importing it, so basis-core imports no part
        if spec is not None and spec.submodule_search_locations is not None:
            found.extend((folder, folder.parents[name.count(".")]) for folder in (Path(location).resolve() for location in spec.submodule_search_locations))
    return tuple(found)


def _parts() -> list[str]:
    """Each installed part's package, from the module its entry point names: its portion of the namespace, such as
    `basis.job` for `basis.job.cli:part`, or for a part outside the namespace its own package, such as `quant` for
    `quant.cli:part`."""
    found: list[str] = []
    for entry in entry_points(group=_GROUP):
        top, _, rest = entry.module.partition(".")
        package = f"{top}.{rest.partition('.')[0]}" if top == _FRAMEWORK and rest else top
        if package not in found and package != f"{_FRAMEWORK}.core":
            found.append(package)
    return found
