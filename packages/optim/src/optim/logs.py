"""Logging. Each record is one JSON object per line on stderr, for agents and Azure Monitor; `OPTIM_LOG_FORMAT=text`
prints lines for a person instead. INFO tells a run's story: what loaded, each portfolio's result and trades, what was
saved, and a record for every load (with its cache outcome), query, HTTP call and solve, and for the rules before each
solve, with timings in `*ms` fields. `OPTIM_LOG_LEVEL=DEBUG` adds a record for every rule, with what it changed.

To log, use stdlib logging and put data in `extra`. Each key becomes a JSON field, and frames, series, numpy values,
dates and pydantic models serialize as they are:

    log = logging.getLogger(__name__)
    log.debug("rule applied", extra={"rule": rule, "dropped": 12})

Keys that clash with `LogRecord` attributes (`name`, `module`, `filename`, ...) raise. Keep records small: AKS splits
lines over 16KB, so bulk data belongs in the run's snapshot, which the logs point to. To time a block, use `timed`.

A run binds its id and its model first, with `bind(run_id=..., model=...)`, and each portfolio's id while it decides
it, with `fields(portfolio_id=...)`. Every record carries them, worker processes' records included, so filtering on
`run_id` finds a whole run. A model logs with `logging.getLogger(__name__)`; `include` gives its package's loggers the
configured level, as `optim`'s have.

Exceptions need nothing: the CLI and the engine log them once, with the notes added on the way up, the root cause, and
the line in this package where it surfaced. Where a frame knows what the message does not, such as which dataset was
loading, it adds that with `error.add_note(...)`.
"""

import logging
import time
import traceback
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, override

import numpy as np
import pandas as pd
import pydantic_core
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE = Path(__file__).parent
_STANDARD = {*vars(logging.makeLogRecord({})), "message", "asctime"}  # LogRecord's own attributes, not `extra` fields
_bound: dict[str, object] = {}  # fields on every record this process logs; see `bind`
_included: list[str] = []  # packages besides `optim` whose loggers log at the configured level; see `include`


class LogSettings(BaseSettings):
    """Read on its own, before `Settings`, so that a bad environment is logged like any other error."""

    model_config = SettingsConfigDict(env_prefix="OPTIM_LOG_", extra="forbid", frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"


def setup() -> None:
    """Configure this process: the CLI calls it first, and each solver worker as it starts. `optim` loggers log at the
    configured level; libraries log only warnings and up. Bad `OPTIM_LOG_*` settings raise after configuring the
    defaults, so that the error is logged as JSON like any other."""
    try:
        settings = LogSettings()
    except ValidationError:
        _configure(LogSettings.model_construct())
        raise
    _configure(settings)


def _configure(settings: LogSettings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.format == "json" else TextFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.captureWarnings(True)  # library warnings, such as cvxpy's, as JSON records rather than raw stderr lines
    for package in ("optim", *_included):
        logging.getLogger(package).setLevel(settings.level)


def include(package: str) -> None:
    """Log `package`'s records at the configured level, as `optim`'s are, rather than warnings only, as a library's
    are: a model's package. The engine includes the same packages in each worker it starts."""
    if package not in _included:
        _included.append(package)
    logging.getLogger(package).setLevel(logging.getLogger("optim").level)


def included() -> list[str]:
    """The packages `include` added, for a worker to include in its own process."""
    return list(_included)


def bind(**fields: object) -> None:
    """Add `fields` to every record this process logs from now on. The engine binds the same fields in each solver
    worker it starts."""
    # process-wide, as the CLI runs one job per process; concurrent runs in one process need a contextvar
    _bound.update(fields)


def bound() -> dict[str, object]:
    """The fields `bind` added, for a worker to bind in its own process."""
    return dict(_bound)


@contextmanager
def fields(**added: object) -> Generator[None]:
    """Add `added` to every record logged in the block, as while one portfolio is decided."""
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
    """Fields for a record or a message: a long list of ids as its size and first few, since AKS splits long lines."""
    return {k: f"{len(v)} ids: {', '.join(map(str, v[:3]))}, ..." if isinstance(v, list | tuple) and len(v) > 3 else v for k, v in fields.items()}


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
        if record.exc_info and record.exc_info[1]:
            entry |= describe(record.exc_info[1])
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
        "where": next((where for e in reversed(chain) if (where := _where(e))), None),
        "traceback": "".join(traceback.format_exception(error)),
    }


def to_json(value: object) -> str:
    return pydantic_core.to_json(value, fallback=_fallback, inf_nan_mode="null").decode()


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return _bound | {key: value for key, value in vars(record).items() if key not in _STANDARD}


def _fallback(value: object) -> object:
    match value:
        case pd.DataFrame():
            return value.to_dict("records")
        case pd.Series():
            return value.to_dict()
        case pd.Index() | np.ndarray() | np.generic():
            return value.tolist()
        case _:
            return str(value)


def _cause(error: BaseException) -> BaseException | None:
    return error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def _headline(error: BaseException) -> str:
    kind = type(error)
    name = kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"
    return f"{name}: {error}"


def _where(error: BaseException) -> str | None:
    """The deepest frame of `error` in this package, as `optim/module.py:line in function`."""
    ours = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).is_relative_to(_PACKAGE)]
    return f"{Path(ours[-1].filename).relative_to(_PACKAGE.parent)}:{ours[-1].lineno} in {ours[-1].name}" if ours else None
