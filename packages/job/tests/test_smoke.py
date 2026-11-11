"""`basis smoke --env ENV`, basis-job's side (step 2.10): a dry run of each job `[tool.basis.job.smoke]` declares, on
that environment's inputs, publishing nothing. The run itself is basis-job's own, which its tests cover; these check
what smoke asks of it, and what it makes of the result."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from basis.job import runs, smoke


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    (config / "basis").mkdir(parents=True)
    (config / "basis" / "qa.env").write_text("BASIS_ENV=qa\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("APPDATA", str(config))  # where Windows keeps the same folder
    for name in [name for name in __import__("os").environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    folder = tmp_path / "repo"
    folder.mkdir()
    return folder


def declare(repo: Path, table: str) -> None:
    (repo / "pyproject.toml").write_text(f'[tool.basis]\nparts = ["job"]\n\n[tool.basis.job.smoke]\n{table}\n', encoding="utf-8")


def test_a_repo_that_declares_no_dry_run_passes(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[tool.basis]\nparts = ["job"]\n', encoding="utf-8")
    assert smoke.smoke(repo, "qa") == []


@pytest.mark.parametrize(
    "table", ['positions = { params = "p.json" }', 'positions = { params = "p.json", days = 0 }', "positions = { params = 1, days = 1 }"], ids=["no-days", "no-days-at-all", "a-number-for-params"]
)
def test_a_dry_run_declared_wrong_says_how_to_declare_it(repo: Path, table: str) -> None:
    declare(repo, table)
    [(reason, fix)] = smoke.smoke(repo, "qa")
    assert reason.startswith("[tool.basis.job.smoke] positions is not a job's params")
    assert fix == 'give it as `positions = { params = "jobs/positions/params/default.json", days = 1 }`'


@pytest.mark.parametrize(("status", "reasons", "failed"), [("checked", [], False), ("failed", ["a check refused it"], True)], ids=["checked", "refused"])
def test_a_dry_run_runs_the_job_unpublished_and_fails_where_its_checks_do(repo: Path, monkeypatch: pytest.MonkeyPatch, status: str, reasons: list[str], failed: bool) -> None:  # noqa: FBT001 -- a table's column
    declare(repo, 'positions = { params = "jobs/positions/params/default.json", days = 2 }')
    asked: list[runs.Request] = []

    def run(job: object, settings: object, request: runs.Request, **_: object) -> object:
        del job, settings
        asked.append(request)
        record = SimpleNamespace(status=status, run_id="r1", job="positions", error=None, reasons=reasons, inputs={}, publishes={})
        return SimpleNamespace(record=record)

    monkeypatch.setattr(smoke.jobs, "load", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(smoke.runs, "run", run)
    found = smoke.smoke(repo, "qa")
    [request] = asked
    assert (request.publish, request.params, request.labels) == (False, repo / "jobs/positions/params/default.json", {"smoke": "qa"})
    assert request.interval is not None
    assert (request.interval.end - request.interval.start).days == 2
    assert bool(found) is failed
