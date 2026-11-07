"""Logging. Each record is one JSON object per line on stderr, for agents and log platforms; `ETL_LOG_FORMAT=text`
prints lines for a person instead. INFO tells a run's story: what loaded, what the transform made, every check that
failed, what was written to each database, and one summary record at the end, with timings in `*_ms` fields.

To log, use stdlib logging and put data in `extra`. Each key becomes a JSON field, and frames, series, numpy values,
dates and pydantic models serialize as they are:

    log = logging.getLogger(__name__)
    log.debug("rule applied", extra={"rule": rule, "dropped": 12})

Keys that clash with `LogRecord` attributes (`name`, `module`, `filename`, ...) raise. Keep records small: container
platforms split long lines, so bulk data belongs in the run's snapshot, which the logs point to. To time a block, use
`timed`.

A run binds its id and its job first, with `bind(run_id=..., job=...)`, so every record carries them and filtering on
`run_id` finds a whole run. A job logs with `logging.getLogger(__name__)`; `include` gives its package's loggers the
configured level, as `etl_framework`'s have.

Exceptions need nothing: the CLI logs them once, with the notes added on the way up, the root cause, and the line in
this package where it surfaced. Where a frame knows what the message does not, such as which dataset was loading, it
adds that with `error.add_note(...)`.
"""

import logging
import time
import traceback
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, override

import polars as pl
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE = Path(__file__).parent
_STANDARD = {*vars(logging.makeLogRecord({})), "message", "asctime"}  # LogRecord's own attributes, not `extra` fields
_bound: dict[str, object] = {}  # fields on every record this process logs; see `bind`
_included: list[str] = []  # packages besides `etl_framework` whose loggers log at the configured level; see `include`


class LogSettings(BaseSettings):
    """Read on its own, before `Settings`, so that a bad environment is logged like any other error."""

    model_config = SettingsConfigDict(env_prefix="ETL_LOG_", extra="forbid", frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] | None = None
    """None: the caller's default, JSON for the command line and text for a run from Python."""


def setup(*, default_format: Literal["json", "text"] = "json") -> None:
    """Configure this process. `etl_framework` loggers log at the configured level; libraries log only warnings and
    up. Bad `ETL_LOG_*` settings raise after configuring the defaults, so that the error is logged like any other."""
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
    for package in ("etl_framework", *_included):
        logging.getLogger(package).setLevel(level)


def include(package: str) -> None:
    """Log `package`'s records at the configured level, as `etl_framework`'s are, rather than warnings only, as a
    library's are: a job's package."""
    if package not in _included:
        _included.append(package)
    logging.getLogger(package).setLevel(logging.getLogger("etl_framework").level)


def bind(**fields: object) -> None:
    """Add `fields` to every record this process logs from now on."""
    # process-wide, as the CLI runs one job per process; concurrent runs in one process need a contextvar
    _bound.update(fields)


@contextmanager
def fields(**added: object) -> Generator[None]:
    """Add `added` to every record logged in the block."""
    before = dict(_bound)
    _bound.update(added)
    try:
        yield
    finally:
        _bound.clear()
        _bound.update(before)


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
            **_extras(record),
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
    the chain, the line in this package where it surfaced (even when a library raised it), and the full traceback."""
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
    return _JSON.dump_json(value, fallback=_fallback).decode()


_JSON = TypeAdapter(object, config=ConfigDict(ser_json_inf_nan="null"))  # NaN is not JSON: it logs as null


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return _bound | {key: value for key, value in vars(record).items() if key not in _STANDARD}


def _fallback(value: object) -> object:
    match value:
        case pl.DataFrame():
            return {"rows": value.height, "columns": value.columns}
        case pl.Series():
            return value.to_list()
        case _:
            return str(value)


def _cause(error: BaseException) -> BaseException | None:
    return error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def _headline(error: BaseException) -> str:
    kind = type(error)
    name = kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"
    return f"{name}: {error}"


def _where(error: BaseException) -> str | None:
    """The deepest frame of `error` in this package, as `etl_framework/module.py:line in function`."""
    ours = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).is_relative_to(_PACKAGE)]
    return f"{Path(ours[-1].filename).relative_to(_PACKAGE.parent)}:{ours[-1].lineno} in {ours[-1].name}" if ours else None
