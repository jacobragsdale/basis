"""A run end to end: what it loads, how it decides each portfolio, what its snapshot holds, and how a replay decides
it again from the snapshot alone."""

import json
from pathlib import Path

import pandas as pd
import pytest
from builders import Clock, Rows, loader
from pandas.testing import assert_frame_equal

import optim
from optim import models
from optim.cli import main
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import replay_run, run_model
from optim.snapshots import read_info, read_inputs, read_results
from optim.storage import Root
from optim.testing import AS_OF, CASH, frame

TOY = models.load("toys.basic")
HELD = frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P1", "security_id": "A", "quantity": 5000.0}, {"portfolio_id": "P2"})
MARKET = frame(SECURITIES, CASH, {"security_id": "A"})


def params(tmp_path: Path, **values: object) -> Path:
    path = tmp_path / "params.json"
    path.write_text(json.dumps(values))
    return path


def toy_run(tmp_path: Path, source: Rows | None = None, **values: object) -> tuple[Root, str]:
    root = Root.at(tmp_path / "snapshots")
    ran = run_model(
        loader(source or Rows({HOLDINGS: HELD, SECURITIES: MARKET})), root, TOY, params(tmp_path, **values), as_of_date=AS_OF, portfolio_ids=["P1", "P2"], labels={"purpose": "test"}, clock=Clock()
    )
    return root, ran.run_id


def test_a_run_records_what_it_was_asked_with_which_code_and_data_and_when(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path, buy="A")

    info = read_info(root / run_id)

    assert (info.run_id, info.optim_version, info.model.module) == (run_id, optim.__version__, "toys.basic")
    assert (info.as_of_date, info.portfolios, info.labels) == (AS_OF, ["P1", "P2"], {"purpose": "test"})
    assert info.params == {"buy": "A", "fail": [], "unsolved": []}  # defaults included
    assert info.params_file.path.endswith("params.json")
    assert info.started_at < info.loaded_at < info.finished_at
    assert info.datasets is not None
    assert {name: (read.source, read.read) for name, read in info.datasets.items()} == {"holdings": ("mssql", "live"), "securities": ("oracle", "live")}


def test_a_snapshot_holds_each_dataset_as_loaded_and_every_decision(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path)

    inputs = read_inputs(root / run_id, {"holdings": HOLDINGS, "securities": SECURITIES})
    results = read_results(root / run_id)

    assert_frame_equal(inputs["holdings"], HELD)  # dtypes included: replay must decide on exactly what the run loaded
    assert_frame_equal(inputs["securities"], MARKET)
    assert results["decisions"][["portfolio_id", "status", "trades"]].to_dict("records") == [
        {"portfolio_id": "P1", "status": "accepted", "trades": 1},
        {"portfolio_id": "P2", "status": "accepted", "trades": 1},
    ]
    assert results["trades"][["portfolio_id", "security_id", "side", "quantity"]].to_dict("records") == [
        {"portfolio_id": p, "security_id": "A", "side": "buy", "quantity": 1000.0} for p in ("P1", "P2")
    ]
    assert results["metrics"]["value"].tolist() == [2.0, 1.0]  # each portfolio saw its own holdings alone


def test_a_portfolio_that_fails_or_is_rejected_leaves_the_others_decided(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path, fail=["P1"], buy="BAD")

    results = read_results(root / run_id)

    assert results["decisions"].set_index("portfolio_id")["status"].to_dict() == {"P1": "rejected", "P2": "rejected"}
    assert results["reasons"].to_numpy().tolist() == [
        ["P1", "ValueError: cannot decide P1, at tests/toys/basic.py:29 in decide"],  # the error, and the model's line that raised it
        ["P2", "trades securities that are not loaded: ['BAD']"],  # the core's reason, then the model's
        ["P2", "buys BAD"],
    ]


def test_a_replay_decides_as_the_run_did_from_its_snapshot_alone(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path, unsolved=["P2"])

    replayed = replay_run(root / run_id, root, workers=1)

    assert [o.reasons for o in replayed.outcomes] == [[], ["not solved: the model says so"]]
    info = read_info(root / replayed.run_id)
    assert (info.replay and info.replay.of, info.datasets, info.params["unsolved"]) == (run_id, None, ["P2"])


def test_a_replay_with_other_params_records_them_and_a_replay_of_a_subset_replays_that_subset(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path)

    other = replay_run(root / run_id, root, params_file=params(tmp_path, buy="BAD"), portfolio_ids=["P2"])
    again = replay_run(root / other.run_id, root)

    assert [(o.portfolio_id, o.reasons[-1]) for o in other.outcomes] == [("P2", "buys BAD")]
    assert [o.portfolio_id for o in again.outcomes] == ["P2"]
    assert read_info(root / other.run_id).params["buy"] == "BAD"


def test_spawned_workers_decide_as_this_process_does(tmp_path: Path) -> None:
    root, run_id = toy_run(tmp_path)

    pooled = replay_run(root / run_id, root, workers=2)

    assert_frame_equal(read_results(root / pooled.run_id)["trades"], read_results(root / run_id)["trades"])


def test_a_run_reads_a_batchs_per_portfolio_datasets_in_one_read_only_transaction(tmp_path: Path) -> None:
    source = Rows({HOLDINGS: HELD, SECURITIES: MARKET})

    toy_run(tmp_path, source)

    assert source.transactions == 1  # two portfolios, one batch, one source
    assert {params["portfolios"] for query, params in source.fetches if query == str(HOLDINGS.query)} == {("P1", "P2")}
    assert [set(params) for query, params in source.fetches if query == str(SECURITIES.query)] == [{"as_of_date"}]


@pytest.mark.parametrize("values", [{"buy": 1}, {"bye": "A"}], ids=["wrong-type", "misspelled"])
def test_params_that_break_the_models_schema_fail_before_anything_loads(tmp_path: Path, values: dict[str, object]) -> None:
    source = Rows({HOLDINGS: HELD, SECURITIES: MARKET})

    with pytest.raises(ValueError, match="params"):
        toy_run(tmp_path, source, **values)

    assert source.fetches == []


def test_the_command_exits_zero_only_when_every_portfolio_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    HELD.to_csv(data / "holdings.csv", index=False)
    MARKET.to_parquet(data / "securities.parquet", index=False)
    monkeypatch.setenv("OPTIM_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    command = ["run", "toys.basic", "--data", str(data), "--as-of", "2026-10-16", "--params"]

    assert main([*command, str(params(tmp_path))]) == 0  # every portfolio in the holdings file
    assert main([*command, str(params(tmp_path, unsolved=["P1"]))]) == 1
    [first, _] = sorted(path.name for path in (tmp_path / "snapshots").iterdir())
    assert main(["replay", first]) == 0
    assert pd.read_parquet(tmp_path / "snapshots" / first / "decisions.parquet")["portfolio_id"].tolist() == ["P1", "P2"]


def test_a_model_that_lists_its_portfolios_decides_every_one_it_lists_when_given_none(tmp_path: Path) -> None:
    """`toys.listed` lists P1, P2 and P3; P3 holds nothing."""
    ran = run_model(loader(Rows({HOLDINGS: HELD, SECURITIES: MARKET})), Root.at(tmp_path), models.load("toys.listed"), params(tmp_path), as_of_date=AS_OF, clock=Clock())

    assert read_info(Root.at(tmp_path) / ran.run_id).portfolios == ["P1", "P2", "P3"]
    assert [outcome.reasons[:1] for outcome in ran.outcomes] == [[], [], ["it has no holdings: is its id right?"]]


def test_a_portfolio_given_that_the_model_does_not_list_is_rejected(tmp_path: Path) -> None:
    ran = run_model(loader(Rows({HOLDINGS: HELD, SECURITIES: MARKET})), Root.at(tmp_path), models.load("toys.listed"), params(tmp_path), as_of_date=AS_OF, portfolio_ids=["P1", "P9"])

    assert [outcome.reasons for outcome in ran.outcomes] == [[], ["not one of the portfolios the model lists on 2026-10-16"]]


def test_a_model_that_lists_no_portfolios_must_be_given_them_unless_its_holdings_are_a_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="give the portfolios to decide"):
        run_model(loader(Rows({HOLDINGS: HELD, SECURITIES: MARKET})), Root.at(tmp_path), TOY, params(tmp_path), as_of_date=AS_OF)
