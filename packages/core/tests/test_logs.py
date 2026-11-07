import asyncio
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import override

import polars as pl
import pytest
from opentelemetry.sdk.trace import TracerProvider

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


def test_an_error_in_a_part_outside_the_namespace_is_located_in_its_code_whatever_the_part_is_named(tmp_path: Path) -> None:
    """A part is any installed package with an entry point in the `basis.verbs` group: here `quant`, a package of its
    own outside the namespace, in a process of its own, since the folders of basis's packages are found once."""
    (tmp_path / "quant").mkdir()
    (tmp_path / "quant" / "__init__.py").write_text("def fail() -> None:\n    raise RuntimeError('quant')\n", encoding="utf-8")
    (tmp_path / "quant-0.1.0.dist-info").mkdir()
    (tmp_path / "quant-0.1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: quant\nVersion: 0.1.0\n", encoding="utf-8")
    (tmp_path / "quant-0.1.0.dist-info" / "entry_points.txt").write_text("[basis.verbs]\nquant = quant.cli:part\n", encoding="utf-8")
    script = "import quant\nfrom basis.core.logs import describe\ntry:\n    quant.fail()\nexcept RuntimeError as error:\n    print(describe(error)['where'])\n"
    environment = os.environ | {"PYTHONPATH": os.pathsep.join([str(tmp_path), *filter(None, [os.environ.get("PYTHONPATH")])])}
    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    assert ran.stdout == "quant/__init__.py:2 in fail\n"


class _Frame:
    """What a pandas frame offers the log: its shape and its columns, whatever they are named by."""

    shape = (3, 2)
    columns = ("price", 2026)


class _Series:
    """What a pandas series offers: its values by its index."""

    index = ("A", "B")

    def to_dict(self) -> dict[str, float]:
        return {"A": 1.5, "B": 2.0}


class _Scalar:
    """What a numpy value or an index offers: its `tolist()`."""

    def tolist(self) -> list[int]:
        return [1, 2]


class _Other:
    @override
    def __str__(self) -> str:
        return "other"


@pytest.mark.parametrize(
    ("value", "logged"),
    [
        (pl.DataFrame({"price": [1.5, 2.0]}), {"rows": 2, "columns": ["price"]}),
        (pl.Series([1, 2]), [1, 2]),
        (_Frame(), {"rows": 3, "columns": ["price", "2026"]}),
        (_Series(), {"A": 1.5, "B": 2.0}),
        (_Scalar(), [1, 2]),
        (_Other(), "other"),
    ],
    ids=["polars-frame", "polars-series", "a-pandas-frame", "a-pandas-series", "a-numpy-value", "anything-else"],
)
def test_a_value_json_has_no_type_for_logs_by_its_shape_since_basis_core_imports_no_pandas_or_numpy(value: object, logged: object) -> None:
    assert json.loads(logs.to_json({"value": value})) == {"value": logged}


def test_a_record_for_a_person_is_the_message_then_its_fields_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs, "_bound", {})  # what other tests bound, process-wide
    record = logging.makeLogRecord({"name": "basis.job", "levelname": "INFO", "msg": "loaded %s", "args": ("prices",), "rows": 3})
    line = logs.TextFormatter().format(record)
    assert line.endswith(' INFO basis.job: loaded prices {"rows":3}')


def test_a_json_record_logged_in_a_span_carries_its_trace_and_span_ids_and_one_logged_outside_none() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    logger = logging.getLogger("basis.core.tests.traced")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    tracer = TracerProvider().get_tracer("test")
    try:
        with tracer.start_as_current_span("run") as run:
            logger.info("in the run")
            with tracer.start_as_current_span("warehouse SELECT") as statement:
                logger.info("in its statement")
                bound = logs.bound()
        logger.info("after it")
    finally:
        logger.removeHandler(handler)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    ids = [(record.get("trace_id"), record.get("span_id")) for record in records]
    trace_id = f"{run.get_span_context().trace_id:032x}"
    assert ids == [(trace_id, f"{run.get_span_context().span_id:016x}"), (trace_id, f"{statement.get_span_context().span_id:016x}"), (None, None)]
    assert (bound["trace_id"], bound["span_id"]) == ids[1]  # what a worker process binds, so its records link to the span that called it


def test_a_record_for_a_person_carries_no_trace_ids() -> None:
    record = logging.LogRecord("basis.test", logging.INFO, __file__, 1, "in the run", None, None)
    with TracerProvider().get_tracer("test").start_as_current_span("run"):
        line = logs.TextFormatter().format(record)
    assert "trace_id" not in line
