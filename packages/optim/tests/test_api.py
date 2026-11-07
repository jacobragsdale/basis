"""Running a model from Python, as its `main.py` does: `optim.run`, `optim.replay` and `optim.sweep`, in a repo `optim new`
made, with paths from the model's folder wherever the process runs, the settings file set for the call alone, snapshots
in the repo, and no orders."""

import importlib
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

import optim
from optim.new import new_model, new_repo
from optim.snapshots import read_info
from optim.storage import Root


@pytest.fixture
def environ() -> Generator[None]:
    """The process's environment, restored after the test, with no `OPTIM_*` variable set."""
    saved = dict(os.environ)
    for name in [name for name in os.environ if name.startswith("OPTIM_")]:
        del os.environ[name]
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def repo(tmp_path: Path, on_path: list[str], environ: None, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: ARG001 -- environ restores what the test sets
    """A generated repo, `sma`, with one model, `income`, and a working directory elsewhere, as an IDE's may be."""
    repo = new_repo("sma", "income", directory=tmp_path, index="https://pypi.firm/simple", uv_sync=False)
    on_path.insert(0, str(repo))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    return repo


def income() -> ModuleType:
    """The generated repo's model, importable once the fixture puts the repo on the path."""
    return importlib.import_module("models.income.model")


def test_a_run_reads_paths_from_the_models_folder_and_saves_in_the_repos_snapshots(repo: Path) -> None:
    results = optim.run(income(), data="tests/fixture")

    assert results.ok
    assert results.decisions["portfolio_id"].tolist() == ["P1", "P2"]  # every portfolio the fixture lists
    assert set(results.trades["portfolio_id"]) == {"P1", "P2"}
    info = read_info(Root.at(repo / "snapshots") / results.run_id)
    assert str(info.as_of_date) == "2026-10-16"  # the fixture's date
    assert info.params_file.path == str(repo / "models" / "income" / "params" / "default.json")
    assert not (repo.parent / "elsewhere" / "snapshots").exists()


def test_a_run_that_needs_a_source_and_has_no_settings_file_says_to_copy_env_example(repo: Path) -> None:  # noqa: ARG001
    with pytest.raises(FileNotFoundError, match=r"copy \.env\.example to \.env"):
        optim.run(income(), portfolios=["P1"])


def test_the_settings_file_above_the_models_folder_is_set_for_the_run_alone_and_writes_no_orders(repo: Path) -> None:
    """The settings file configures an orders table, which a run that wrote orders would fail to reach."""
    (repo / ".env").write_text("OPTIM_WORKERS=2\nOPTIM_ORDERS__CONNECTION_STRING=Server=nowhere.invalid\nOPTIM_ORDERS__TABLE=trading.orders\n")

    results = optim.run(income(), data="tests/fixture")

    assert results.ok
    assert read_info(Root.at(repo / "snapshots") / results.run_id).workers == 2
    assert not [name for name in os.environ if name.startswith("OPTIM_") and name != "OPTIM_LOG_FORMAT"]


def test_a_replay_finds_its_run_in_the_repos_snapshots_and_refuses_another_models(repo: Path) -> None:
    ran = optim.run(income(), data="tests/fixture")
    new_model("ladder", repo=repo, uv_sync=False)
    ladder = importlib.import_module("models.ladder.model")

    replayed = optim.replay(income(), ran.run_id, portfolios=["P1"])

    assert replayed.decisions["portfolio_id"].tolist() == ["P1"]
    assert replayed.trades.equals(ran.trades[ran.trades["portfolio_id"] == "P1"])
    with pytest.raises(ValueError, match=r"is a run of models\.income\.model"):
        optim.replay(ladder, ran.run_id)


def test_a_sweep_decides_a_saved_run_once_per_params_file_into_one_table(repo: Path) -> None:
    ran = optim.run(income(), data="tests/fixture")
    params = repo / "models" / "income" / "params"
    (params / "tight.json").write_text((params / "default.json").read_text().replace('"max_turnover": 0.25', '"max_turnover": 0.01'))

    table = optim.sweep(income(), ran.run_id, params=["params/default.json", "params/tight.json"])

    assert table[["variant", "portfolio_id"]].to_numpy().tolist() == [["default", "P1"], ["default", "P2"], ["tight", "P1"], ["tight", "P2"]]
    assert table.groupby("variant")["turnover"].sum()["tight"] < table.groupby("variant")["turnover"].sum()["default"]


def test_main_py_runs_the_model_when_run_as_a_file_from_anywhere_in_worker_processes(repo: Path) -> None:
    """As an IDE's Run button runs it: `python models/income/main.py`, whose spawned workers import it again."""
    main = repo / "models" / "income" / "main.py"
    main.write_text(main.read_text().replace('env=".env")', 'env=".env", data="tests/fixture", workers=2)'))
    environment = os.environ | {"PYTHONPATH": os.pathsep.join([str(repo), *sys.path])}

    ran = subprocess.run([sys.executable, str(main)], cwd=repo.parent / "elsewhere", env=environment, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603 -- this interpreter

    assert ran.returncode == 0, ran.stderr[-3000:]
    assert ran.stdout.splitlines()[0].startswith("run ")
    assert ran.stdout.count("accepted") == 2
    assert len(list((repo / "snapshots").iterdir())) == 1
