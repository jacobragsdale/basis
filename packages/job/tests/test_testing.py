from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from builders import Price, make_job, params_file

import basis.job as etl
from basis.core.testing import ContractError
from basis.job.job import Job
from basis.job.params import Params
from basis.job.testing import check_fixture, check_imports, inputs


@pytest.mark.parametrize("statement", ["import pyarrow.fs", "from pyarrow import fs", "from pyarrow.fs import LocalFileSystem", "import pyarrow as pa"])
def test_a_job_that_imports_pyarrow_itself_is_caught_though_basis_frames_types_come_from_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str) -> None:
    package = tmp_path / "leaky_jobs"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "job.py").write_text(f"{statement}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    job = Job("leaky", "leaky_jobs.job", Params, object, object, print, None, package)  # ty: ignore[invalid-argument-type] -- the import check reads the module alone
    with pytest.raises(ContractError, match=r"leaky_jobs\.job -> pyarrow"):
        check_imports(job)


class _Stamped(etl.Inputs):
    prices = etl.Fetch(Price, http="pricing", path="/prices", deadline_s=5, empty="allow")
    clock = etl.Clock()
    ids = etl.Ids()


def test_a_transforms_inputs_give_the_time_a_test_names_and_ids_that_count_up() -> None:
    at = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    given = inputs(_Stamped, now=at)
    assert (given.clock.now(), given.ids.new(), given.ids.new(2), len(given.prices)) == (
        at, UUID("00000000-0000-7000-8000-000000000001"), [UUID("00000000-0000-7000-8000-000000000002"), UUID("00000000-0000-7000-8000-000000000003")], 0,
    )  # fmt: skip
    with pytest.raises(ValueError, match=r"_Stamped\.clock is a clock, and no time is given for it to read: give it, as inputs\(\.\.\., now="):
        inputs(_Stamped)
    with pytest.raises(LookupError, match=r"_Stamped\.clock is a clock the run gives, and these inputs have none"):
        _Stamped({}, interval=None).clock.now()


def test_a_fixture_holds_a_file_for_each_fetch_and_the_time_each_clock_reads(tmp_path: Path) -> None:
    job = make_job(write=etl.Append())
    stamped = Job(job.name, job.module, job.params, _Stamped, job.outputs, job.transform, None, job.directory)
    (tmp_path / "fixture.json").write_text('{"interval": null}', encoding="utf-8")
    with pytest.raises(ContractError, match=r"has no file for \['prices'\]"):
        check_fixture(stamped, tmp_path, params_file(tmp_path), tmp_path / "snapshots")
    (tmp_path / "prices.csv").write_text("day,security_id,price,note,booked_at\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r'no time is given for it to read: add "now": "2026-11-06T06:00:00\+00:00" to .*fixture\.json'):
        check_fixture(stamped, tmp_path, params_file(tmp_path), tmp_path / "snapshots")


def test_a_clock_or_ids_is_named_as_a_resource_is_and_declared_once() -> None:
    with pytest.raises(TypeError, match="clock '_clock': a resource's name is lower-case letters") as raised:

        class Hidden(etl.Inputs):
            _clock = etl.Clock()

    assert "declared as test_a_clock_or_ids_is_named_as_a_resource_is_and_declared_once.<locals>.Hidden._clock: rename it" in raised.value.__notes__
    shared = etl.Ids()
    with pytest.raises(TypeError, match=r"Twice\.second is already declared as 'first': declare each ids once per name"):

        class Twice(etl.Inputs):
            first = shared
            second = shared
