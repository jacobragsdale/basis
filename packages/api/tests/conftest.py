import io
import json
import logging
from collections.abc import Callable, Iterator

import pytest

from basis.core.logs import JsonFormatter

type Records = Callable[[], list[dict[str, object]]]


@pytest.fixture
def logged() -> Iterator[Records]:
    """The records logged during the test, each as the JSON line basis-core writes, with basis's loggers at INFO."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root, framework = logging.getLogger(), logging.getLogger("basis")
    level = framework.level
    root.addHandler(handler)
    framework.setLevel(logging.INFO)
    try:
        yield lambda: [_record(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)
        framework.setLevel(level)


def _record(line: str) -> dict[str, object]:
    record: object = json.loads(line)
    assert isinstance(record, dict)
    return {str(key): value for key, value in record.items()}


@pytest.fixture
def logging_restored() -> Iterator[None]:
    """The process's logging as it was, once the test ends: `basis` run in the test's process configures logging to
    write to the test's captured stderr, which closes with the test."""
    root, framework = logging.getLogger(), logging.getLogger("basis")
    handlers, level, framework_level = root.handlers[:], root.level, framework.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        framework.setLevel(framework_level)
