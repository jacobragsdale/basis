import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest

from basis.core import clone, command, full, hooks, review, sync
from basis.core.errors import BasisError
from basis.core.verbs import Difference, Part, Qa, QaPlan, QaRun, Regress, Regressed, Regressions, Runs, Shared

type Git = Callable[..., str]
"""conftest's `git` fixture."""

AT = datetime(2026, 11, 7, 14, 2, tzinfo=UTC)
CHANGED = Regressed("prices", "R1", (), (Difference("prices", "upsert: 1 changed (price 1)", {"output": "prices"}),))
SAME = Regressed("prices", "R2", (), ())


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    """A clone of a repo whose files are what basis writes, with every change committed."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    sync.write(folder, [])
    git(folder, "init", "--quiet")
    hooks.ensure(folder)
    git(folder, "add", "--all")
    git(folder, "commit", "--quiet", "--no-verify", "--message", "First commit")  # the hooks run basis, which these tests stand in for
    return folder


def passing(_argv: Sequence[str], _folder: Path) -> int:
    return 0


def failing(argv: Sequence[str], _folder: Path) -> int:
    return 1 if "pytest" in argv else 0


def regressing(*found: Regressed, failures: Sequence[Exception] = ()) -> tuple[list[Regress], Runs]:
    asked: list[Regress] = []

    def regress(args: Regress) -> Regressions:
        asked.append(args)
        return Regressions(found, tuple(failures))

    return asked, Runs("job", lambda: {"prices": "prices.job"}, regress=Shared("replay recent published runs", lambda _: 0), regressions=regress)


def checked(repo: Path, part: Part, *, run: Callable[[Sequence[str], Path], int] = passing) -> int:
    return full.full(repo, [], {"alpha": part}, run=run, report=command.reported, folder=repo, clock=lambda: AT)


def record(repo: Path) -> review.Record:
    found = clone.of(repo)
    assert found is not None
    head = found.head()
    assert head is not None
    return review.Record.model_validate_json(review.path(found, head).read_bytes())


def lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if not line.startswith("{")]  # after any log records


def test_without_a_run_store_or_a_qa_tier_each_piece_says_it_was_skipped_and_why(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    asked, runs = regressing()
    assert checked(repo, Part(runs=runs)) == 0
    found = lines(capsys)
    assert found[:2] == [
        "SKIPPED: regress: BASIS_SNAPSHOT_ROOT is not set, so there is no run store to replay: set it, where git runs the push hook, to the store whose published runs to replay",
        "SKIPPED: the QA tier: pyproject.toml declares no [tool.basis.qa]: declare one to run the repo's parts against QA, as `uv run basis docs reference` shows",
    ]
    assert asked == []
    assert record(repo).passed


@pytest.mark.parametrize(("root", "why"), [("missing", "does not exist here")], ids=["a-store-that-is-not-there"])
def test_a_run_store_that_cannot_be_reached_skips_regress(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, root: str, why: str) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", root)
    asked, runs = regressing()
    assert checked(repo, Part(runs=runs)) == 0
    assert lines(capsys)[0] == f"SKIPPED: regress: the run store {(repo / root).as_posix()}, BASIS_SNAPSHOT_ROOT, {why}"
    assert asked == []


def test_regress_replays_from_the_store_and_a_change_it_sees_is_recorded_without_failing_the_check(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    (repo.parent / "store").mkdir()
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo.parent / "store"))
    asked, runs = regressing(CHANGED, SAME)
    assert checked(repo, Part(runs=runs)) == 0
    assert [(args.name, args.last, args.snapshots) for args in asked] == [(None, 10, str(repo.parent / "store"))]
    assert lines(capsys)[:2] == [
        f"basis check --full: regress replayed 2 published runs from {repo.parent / 'store'}: 1 differs, as `uv run basis review` lists",
        "basis check --full:   prices R1: prices: upsert: 1 changed (price 1)",
    ]
    found = record(repo)
    assert (found.passed, [entry.run_id for entry in found.regress.replayed]) == (True, ["R1", "R2"])


def test_a_part_that_gives_no_regressions_is_skipped_by_name_and_the_others_still_replay(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    (repo.parent / "store").mkdir()
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo.parent / "store"))
    asked, runs = regressing(SAME)
    models = Runs("model", lambda: {"income": "models.income"}, regress=Shared("replay a model's recent runs", lambda _: 0))
    code = full.full(repo, [], {"alpha": Part(runs=runs), "beta": Part(runs=models)}, run=passing, report=command.reported, folder=repo, clock=lambda: AT)
    assert code == 0
    assert lines(capsys)[:2] == [
        "SKIPPED: regress of the beta part: it gives `basis check --full` no regressions to record: `uv run basis regress` replays its models",
        f"basis check --full: regress replayed 1 published run from {repo.parent / 'store'}: none differs",
    ]
    assert len(asked) == 1
    assert [entry.run_id for entry in record(repo).regress.replayed] == ["R2"]


def test_a_failure_to_regress_fails_the_check(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo))
    _, runs = regressing(failures=[BasisError("while regressing prices: no job named 'prices': give one")])
    assert checked(repo, Part(runs=runs)) == 1
    assert record(repo).regress.failures == ["while regressing prices: no job named 'prices': give one"]


def test_a_part_whose_regress_raises_is_one_failure_and_the_record_is_kept(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo))

    def regress(_: Regress) -> Regressions:
        msg = "no module named 'jobs.prices'"
        raise ImportError(msg)

    assert checked(repo, Part(runs=Runs("job", lambda: {"prices": "prices.job"}, regress=Shared("replay recent published runs", lambda _: 0), regressions=regress))) == 1
    assert record(repo).regress.failures == ["ImportError: while regressing the alpha part's runs: no module named 'jobs.prices': a defect"]


def test_a_failing_check_skips_regress_and_the_qa_tier_and_records_that_the_commit_failed(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo))
    asked, runs = regressing()
    assert checked(repo, Part(runs=runs), run=failing) == 1
    assert [line for line in lines(capsys) if line.startswith("SKIPPED")] == [
        "SKIPPED: regress: basis check failed, as above",
        "SKIPPED: the QA tier: basis check failed, as above",
        "SKIPPED: the contract diffs: basis check failed, as above",
    ]
    assert asked == []
    assert not record(repo).passed


def test_a_folder_with_uncommitted_changes_is_checked_and_not_recorded(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "notes.md").write_text("# Not yet\n", encoding="utf-8")
    assert checked(repo, Part()) == 0
    assert lines(capsys)[-1].startswith("SKIPPED: the record for basis review: the repo's folder has changes no commit holds")
    found = clone.of(repo)
    assert found is not None
    assert not (found.common / "basis").exists()


QA = """
[tool.basis.qa]
settings = ".env.qa"
marker = "qa"
stores = ["runs-qa"]
writes = { warehouse = ["prices"] }
"""


def qa_part(*problems: tuple[str, str], failure: tuple[str, str] | None = None) -> Part:
    def plan(_: Qa) -> QaPlan:
        return QaPlan(problems, lambda: [QaRun("prices", "Q1", "published: prices (3 inserted)", failure)])

    return Part(qa=plan)


@pytest.fixture
def declared(repo: Path, git: Git) -> Path:
    """`repo`, declaring a QA tier, with QA's settings file beside it, which git ignores."""
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write(QA)
    (repo / ".gitignore").write_text(".env.qa\n", encoding="utf-8")
    (repo / ".env.qa").write_text("BASIS_SNAPSHOT_ROOT=runs-qa\n", encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--no-verify", "--message", "Declare the QA tier")
    return repo


def test_the_qa_tier_runs_and_what_it_ran_is_recorded(declared: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checked(declared, qa_part()) == 0
    assert "basis check --full: the QA tier ran alpha prices as run Q1: published: prices (3 inserted)" in lines(capsys)
    assert [(run.part, run.run_id, run.summary) for run in record(declared).qa.runs] == [("alpha", "Q1", "published: prices (3 inserted)")]


def redeclare(repo: Path, git: Git, store: str) -> None:
    """Make `store` the repo's one QA store, and the settings file's, and commit it."""
    text = (repo / "pyproject.toml").read_text(encoding="utf-8").replace('stores = ["runs-qa"]', f'stores = ["{store}"]')
    (repo / "pyproject.toml").write_text(text, encoding="utf-8")
    (repo / ".env.qa").write_text(f"BASIS_SNAPSHOT_ROOT={store}\n", encoding="utf-8")
    git(repo, "commit", "--quiet", "--no-verify", "--all", "--message", "Another QA store")


def test_a_qa_store_that_cannot_be_opened_is_one_failure_and_regress_is_still_recorded(declared: Path, git: Git, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    redeclare(declared, git, "nosuch://runs-qa")
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(declared))
    _, runs = regressing(SAME)
    assert checked(declared, Part(runs=runs, qa=qa_part().qa)) == 1
    refused, ran = [line for line in lines(capsys) if line.startswith("ERROR")]
    assert refused.startswith("ERROR: the QA store nosuch://runs-qa cannot be opened (")
    assert ran == "ERROR: the QA tier ran nothing, for the reasons above: fix each, and check again"
    found = record(declared)
    assert ([entry.run_id for entry in found.regress.replayed], found.qa.runs) == (["R2"], [])
    assert found.qa.refused[0].startswith("the QA store nosuch://runs-qa cannot be opened (")


def test_a_qa_tier_whatever_it_raises_is_one_failure_and_the_record_is_kept(declared: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def plan(_: Qa) -> QaPlan:
        msg = "cannot import name 'Prices' from 'jobs.prices.rows'"
        raise ImportError(msg)

    assert checked(declared, Part(qa=plan)) == 1
    assert [line for line in lines(capsys) if line.startswith("ERROR")][-1].startswith("ERROR: ImportError: while running the QA tier: cannot import name 'Prices'")
    assert record(declared).qa.refused == ["ImportError: while running the QA tier: cannot import name 'Prices' from 'jobs.prices.rows': a defect"]


def test_upgrades_check_leaves_the_qa_tier_to_the_push(declared: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def plan(_: Qa) -> QaPlan:
        pytest.fail("the QA tier ran")

    assert full.full(declared, [], {"alpha": Part(qa=plan)}, run=passing, report=command.reported, folder=declared, clock=lambda: AT, with_qa=False) == 0
    assert "SKIPPED: the QA tier: it runs when you push, once the change is committed" in lines(capsys)


def test_without_a_part_that_offers_a_contract_the_diff_is_skipped_and_review_says_why(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checked(repo, Part()) == 0
    skipped = "no part of this repo offers a contract, such as an API's openapi.json, so there is no diff to show"
    assert f"SKIPPED: the contract diffs: {skipped}" in lines(capsys)
    assert review.review(repo, "HEAD") == 0
    assert f"### Contracts\n\nSkipped: {skipped}.\n" in capsys.readouterr().out


def test_a_qa_tier_that_refuses_or_a_qa_run_that_fails_fails_the_check(declared: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checked(declared, qa_part(("prices writes positions, which writes does not list", "list it"))) == 1
    assert [line for line in lines(capsys) if line.startswith("ERROR")] == [
        "ERROR: prices writes positions, which writes does not list: list it",
        "ERROR: the QA tier ran nothing, for the reasons above: fix each, and check again",
    ]
    assert checked(declared, qa_part(failure=("run Q1 of prices failed: login failed", "fix it"))) == 1
    assert record(declared).qa.runs[0].failure == "run Q1 of prices failed: login failed: fix it"


def test_review_prints_what_regress_and_the_qa_tier_found_on_the_commit_checked(declared: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(declared))
    _, runs = regressing(CHANGED, SAME)
    part = Part(runs=runs, qa=qa_part().qa, contract=lambda _: "Changed since v0.1.0, compatibly.\n\n- `GET /prices`: `price` is now a decimal")
    assert checked(declared, part) == 0
    assert "basis check --full: the alpha part's contract: Changed since v0.1.0, compatibly, as `uv run basis review` shows" in capsys.readouterr().err
    assert review.review(declared, "HEAD") == 0
    head = clone.of(declared)
    assert head is not None
    commit = head.head()
    printed = capsys.readouterr().out
    assert f"`basis check --full` passed on commit `{commit}`, on 2026-11-07 14:02 UTC, with basis-core {version('basis-core')}" in printed
    assert "| prices | `R1` | prices: upsert: 1 changed (price 1) |" in printed
    assert "| alpha | prices | `Q1` | published: prices (3 inserted) |" in printed
    assert "### The alpha contract\n\nChanged since v0.1.0, compatibly.\n\n- `GET /prices`: `price` is now a decimal" in printed
    assert f"\n- basis-core {version('basis-core')}\n" in printed


def test_review_of_a_commit_no_check_recorded_says_how_to_record_one(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert review.review(repo, "HEAD") == 1
    line = capsys.readouterr().err
    assert line.startswith("ERROR: commit ")
    assert line.endswith("has no `basis check --full` recorded in this clone: push it, which runs the check, or run `uv run basis check --full` with every change committed\n")


def test_review_of_a_commit_that_failed_prints_its_summary_and_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checked(repo, Part(), run=failing)
    capsys.readouterr()
    assert review.review(repo, "HEAD") == 1
    captured = capsys.readouterr()
    assert "`basis check --full` failed on commit" in captured.out
    assert "`basis check` failed, so neither regress nor the QA tier ran." in captured.out
    assert captured.err.startswith("ERROR: commit ")


def test_a_record_is_kept_where_every_worktree_of_the_clone_finds_it(repo: Path, git: Git, tmp_path: Path) -> None:
    git(repo, "worktree", "add", "--quiet", "--detach", str(tmp_path / "other"), "HEAD")
    assert checked(tmp_path / "other", Part()) == 0
    assert record(repo).commit == git(repo, "rev-parse", "HEAD").strip()
