import asyncio
import importlib.util
import io
import json
import logging
from pathlib import Path

import pytest

from basis.core import logs
from basis.core.interval import Interval
from basis.core.logs import describe


def test_an_error_in_basis_core_is_located_in_its_code() -> None:
    with pytest.raises(ValueError, match=r"an interval is START\.\.END") as raised:
        Interval.parse("2026-11-05")
    where = describe(raised.value)["where"]
    assert isinstance(where, str)
    assert where.startswith("basis/core/interval.py:")
    assert where.endswith(" in parse")


def test_a_basis_folder_that_is_no_basis_package_is_not_framework_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder named `basis` on the path, such as one in the working directory, joins the namespace, and is not basis."""
    (tmp_path / "basis").mkdir()
    (tmp_path / "basis" / "local.py").write_text("def fail() -> None:\n    raise RuntimeError('local')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    spec = importlib.util.spec_from_file_location("basis.local", tmp_path / "basis" / "local.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="local") as raised:
        module.fail()
    assert describe(raised.value)["where"] is None


def test_fields_hold_in_their_own_context_alone() -> None:
    """Two tasks each log inside their own `fields` block while the other's is open, as two requests served at once do."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    logger = logging.getLogger("basis.core.tests")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    async def main() -> None:
        both_open = asyncio.Barrier(3)

        async def task(request_id: str) -> None:
            with logs.fields(request_id=request_id):
                await both_open.wait()
                logger.info(request_id)
                await both_open.wait()

        async with asyncio.TaskGroup() as group:
            group.create_task(task("a"))
            group.create_task(task("b"))
            await both_open.wait()
            await both_open.wait()

    try:
        asyncio.run(main())
        logger.info("outside")
    finally:
        logger.removeHandler(handler)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert sorted((record["message"], record.get("request_id")) for record in records) == [("a", "a"), ("b", "b"), ("outside", None)]


def test_a_worker_binds_the_fields_a_record_logged_where_it_starts_carries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawned process starts with no fields, so a part that starts one binds what `bound` gives: the process's own
    fields, and those of the `fields` block it starts in, such as a request's."""
    monkeypatch.setattr(logs, "_bound", {})
    logs.bind(run_id="r1")
    with logs.fields(request_id="q1"):
        assert logs.bound() == {"run_id": "r1", "request_id": "q1"}
    assert logs.bound() == {"run_id": "r1"}
