import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

import basis.job as etl
from basis.core.environments import EnvError
from basis.job.example import job


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


def test_a_run_in_an_environment_reads_the_user_s_file_for_it_and_sets_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "basis").mkdir(parents=True)
    (tmp_path / "config" / "basis" / "dev.env").write_text("BASIS_LOG_LEVEL=WARNING\nBASIS_GIT_SHA=abc123\nOTHER_TOOL=1\n", encoding="utf-8")
    result = etl.run(job, params="params/default.json", data="tests/fixture", env="dev", snapshot_root=tmp_path / "snapshots")
    assert result.record.status == "checked"
    assert "BASIS_LOG_LEVEL" not in os.environ
    assert "OTHER_TOOL" not in os.environ


def test_a_run_in_an_environment_nothing_configures_is_refused_with_the_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with pytest.raises(EnvError, match=r"there is no .*qa\.env, and the process's environment sets no BASIS_ settings"):
        etl.run(job, params="params/default.json", data="tests/fixture", env="qa", snapshot_root=tmp_path / "snapshots")


def test_a_replay_finds_a_run_from_python_and_saves_beside_it(tmp_path: Path) -> None:
    first = etl.run(job, params="params/default.json", data="tests/fixture", env=None, snapshot_root=tmp_path)
    replayed, diffs = etl.replay(job, first.record.run_id, snapshot_root=tmp_path)
    assert replayed.record.replay_of == first.record.run_id
    assert diffs == []


STAMPED = """
from datetime import date, datetime
from typing import Annotated

import basis.job as etl
from basis.core.environments import EnvError
import pandas as pd


class Day(etl.Row):
    day: date


class Stamp(etl.Row):
    stamped_at: Annotated[datetime, etl.Zone("UTC")]
    stamp_id: Annotated[str, etl.MaxLength(36)]


class Params(etl.Params):
    pass


class Inputs(etl.Inputs):
    days = etl.File(Day, path="days.csv", empty="allow")
    clock = etl.Clock()
    ids = etl.Ids()


class Outputs(etl.Outputs):
    stamps = etl.Table(Stamp, database="warehouse", table="stamps", write=etl.Append())


def transform(inputs: Inputs, params: Params) -> Outputs:
    return Outputs(stamps=pd.DataFrame({"stamped_at": [inputs.clock.now()], "stamp_id": [str(inputs.ids.new())]}))
"""


def stamped(tmp_path: Path, *, now: str | None) -> ModuleType:
    """A job with a clock and ids, in a folder of its own, beside a fixture whose `fixture.json` gives `now`, if any."""
    folder = tmp_path / "stamped"
    (folder / "fixture").mkdir(parents=True)
    (folder / "pyproject.toml").write_text("", encoding="utf-8")
    (folder / "job.py").write_text(STAMPED, encoding="utf-8")
    (folder / "params.json").write_text("{}", encoding="utf-8")
    (folder / "fixture" / "days.csv").write_text("day\n2026-11-05\n", encoding="utf-8")
    (folder / "fixture" / "fixture.json").write_text(json.dumps({"interval": "2026-11-05..2026-11-06"} | ({"now": now} if now is not None else {})), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("stamped_job", folder / "job.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("interval", [None, "2026-11-04..2026-11-05"], ids=["the-fixtures", "its-own"])
def test_a_run_on_a_fixture_reads_its_time_and_counts_its_ids_whatever_its_interval(tmp_path: Path, interval: str | None) -> None:
    result = etl.run(stamped(tmp_path, now="2026-11-06T06:00:00+00:00"), params="params.json", data="fixture", interval=interval, env=None, snapshot_root=tmp_path / "snapshots")
    assert (result.record.status, result.record.clocks, result.record.ids) == (
        "checked", {"clock": [datetime(2026, 11, 6, 6, 0, tzinfo=UTC)]}, {"ids": [UUID("00000000-0000-7000-8000-000000000001")]},
    )  # fmt: skip


def test_a_run_on_a_fixture_whose_clock_has_no_time_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r'Inputs\.clock is a clock, and no time is given for it to read: add "now": "2026-11-06T06:00:00\+00:00" to .*fixture\.json'):
        etl.run(stamped(tmp_path, now=None), params="params.json", data="fixture", interval="2026-11-05..2026-11-06", env=None, snapshot_root=tmp_path / "snapshots")


def test_a_replay_from_python_exports_its_spans_where_the_settings_say(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    first = etl.run(job, params="params/default.json", data="tests/fixture", env=None, snapshot_root=tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "basis").mkdir(parents=True)
    (tmp_path / "config" / "basis" / "dev.env").write_text("BASIS_TRACES__EXPORTER=console\n", encoding="utf-8")
    capsys.readouterr()
    etl.replay(job, first.record.run_id, env="dev", snapshot_root=tmp_path)
    assert '"name": "replay"' in capsys.readouterr().err
