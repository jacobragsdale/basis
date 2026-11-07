import os
from pathlib import Path

import pytest

import basis.job as etl
from basis.job.example import job


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("ETL_")]:
        monkeypatch.delenv(name)


def test_a_settings_file_applies_to_one_call_and_may_hold_what_the_framework_does_not_read(tmp_path: Path) -> None:
    settings = tmp_path / "test.env"
    settings.write_text("ETL_LOG_LEVEL=WARNING\nETL_GIT_SHA=abc123\nOTHER_TOOL=1\n", encoding="utf-8")
    result = etl.run(job, params="params/default.json", data="tests/fixture", env=settings, snapshot_root=tmp_path / "snapshots")
    assert result.record.status == "checked"
    assert "ETL_LOG_LEVEL" not in os.environ
    assert "OTHER_TOOL" not in os.environ


def test_a_replay_finds_a_run_from_python_and_saves_beside_it(tmp_path: Path) -> None:
    first = etl.run(job, params="params/default.json", data="tests/fixture", env=None, snapshot_root=tmp_path)
    replayed, diffs = etl.replay(job, first.record.run_id, snapshot_root=tmp_path)
    assert replayed.record.replay_of == first.record.run_id
    assert diffs == []
