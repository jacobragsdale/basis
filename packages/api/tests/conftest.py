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
