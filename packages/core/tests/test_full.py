import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest

from basis.core import check, clone, command, environments, full, hooks, review, supply, sync
from basis.core.errors import BasisError
from basis.core.verbs import Difference, Part, Qa, QaPlan, QaRun, Regress, Regressed, Regressions, Runs, Shared

type Git = Callable[..., str]
"""conftest's `git` fixture."""

AT = datetime(2026, 11, 7, 14, 2, tzinfo=UTC)
CHANGED = Regressed("prices", "R1", (), (Difference("prices", "upsert: 1 changed (price 1)", {"output": "prices"}),))
SAME = Regressed("prices", "R2", (), ())


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No BASIS_ variable, and a user's own basis folder, outside the repo, with no qa.env yet."""
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))  # where Windows keeps the same folder


def qa_env(text: str) -> None:
    path = environments.user_file("qa")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    """A clone of a repo whose files are what basis writes, with every change committed."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\ncoverage = 90\n\n[tool.uv]\nexclude-newer = "7 days"\n', encoding="utf-8")
    sync.write(folder, [])
    git(folder, "init", "--quiet")
    hooks.ensure(folder)
    git(folder, "add", "--all")
    git(folder, "commit", "--quiet", "--no-verify", "--message", "First commit")  # the hooks run basis, which these tests stand in for
    return folder


def passing(argv: Sequence[str], _folder: Path) -> int:
    covered(argv)
    return 0


def failing(argv: Sequence[str], _folder: Path) -> int:
    covered(argv)
    return 1 if "pytest" in argv else 0


def covered(argv: Sequence[str]) -> None:
    """Write the coverage report pytest-cov writes, of every line, where `basis check` asks for one."""
    for argument in argv:
        if argument.startswith("--cov-report=json:"):
            Path(argument.removeprefix("--cov-report=json:")).write_text('{"totals": {"percent_covered": 100.0}}', encoding="utf-8")


def regressing(*found: Regressed, failures: Sequence[Exception] = ()) -> tuple[list[Regress], Runs]:
    asked: list[Regress] = []

    def regress(args: Regress) -> Regressions:
        asked.append(args)
        return Regressions(found, tuple(failures))

    return asked, Runs("job", lambda: {"prices": "prices.job"}, regressions=regress, verbs={"regress": Shared("replay recent published runs", lambda _: 0)})


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


def test_mutation_testing_runs_by_default(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Path] = []
    monkeypatch.setattr(full.mutation, "mutate", lambda given: ran.append(given) or review.MutationRecord(skipped="the change touches no domain function"))
    assert full._mutation(repo).skipped == "the change touches no domain function"  # noqa: SLF001 -- the one piece under test
    assert ran == [repo]


def test_a_parts_checks_for_full_run_after_basis_check_passes_and_fail_it(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A check too slow for every commit, such as basis-api's Schemathesis requests, runs in `basis check --full`
    alone, whose push hook it holds, and never in `basis check`, which the commit hook runs."""
    asked: list[Path] = []

    def deeper(given: Path) -> list[tuple[str, str]]:
        asked.append(given)
        return [("Schemathesis found GET /halves answers 500", "make the request again, and fix the route")]

    assert checked(repo, Part(full_checks=lambda _: [])) == 0
    assert "basis check --full: the alpha part's checks for --full found nothing" in lines(capsys)
    part = Part(full_checks=deeper)
    assert check.check(repo, [], run=passing, checks=check.checks_of({"alpha": part})) == 0
    assert asked == []
    capsys.readouterr()
    assert checked(repo, part) == 1
    assert asked == [repo]
    assert "ERROR: Schemathesis found GET /halves answers 500: make the request again, and fix the route" in lines(capsys)
    assert not record(repo).passed


def test_a_parts_checks_for_full_wait_for_basis_check(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    part = Part(full_checks=lambda _: pytest.fail("ran on a repo basis check failed"))
    assert checked(repo, part, run=failing) == 1
    assert "SKIPPED: each part's checks for --full: basis check failed, as above" in lines(capsys)


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
    reports = Runs("report", lambda: {"daily": "reports.daily"}, verbs={"regress": Shared("replay a report's recent runs", lambda _: 0)})
    code = full.full(repo, [], {"alpha": Part(runs=runs), "beta": Part(runs=reports)}, run=passing, report=command.reported, folder=repo, clock=lambda: AT)
    assert code == 0
    assert lines(capsys)[:2] == [
        "SKIPPED: regress of the beta part: it gives `basis check --full` no regressions to record: `uv run basis regress` replays its reports",
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

    assert checked(repo, Part(runs=Runs("job", lambda: {"prices": "prices.job"}, regressions=regress, verbs={"regress": Shared("replay recent published runs", lambda _: 0)}))) == 1
    assert record(repo).regress.failures == ["ImportError: while regressing the alpha part's runs: no module named 'jobs.prices': a defect"]


def test_a_failing_check_skips_regress_and_the_qa_tier_and_records_that_the_commit_failed(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(repo))
    asked, runs = regressing()
    assert checked(repo, Part(runs=runs), run=failing) == 1
    assert [line for line in lines(capsys) if line.startswith("SKIPPED")] == [
        "SKIPPED: regress: basis check failed, as above",
        "SKIPPED: the QA tier: basis check failed, as above",
        "SKIPPED: the contract diffs: basis check failed, as above",
        "SKIPPED: mutation testing: basis check failed, as above",
        "SKIPPED: the supply chain's checks: basis check failed, as above",
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
    """`repo`, declaring a QA tier, with the user's qa.env."""
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write(QA)
    qa_env("BASIS_SNAPSHOT_ROOT=runs-qa\n")
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
    qa_env(f"BASIS_SNAPSHOT_ROOT={store}\n")
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


def test_review_of_a_record_another_version_of_basis_wrote_names_it_and_says_to_check_again(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    found = clone.of(repo)
    assert found is not None
    head = found.head()
    assert head is not None
    kept = review.path(found, head)
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text('{"commit": "%s", "checked": "yes", "recorded_by": "basis 0.0.1"}' % head, encoding="utf-8")  # noqa: UP031 -- JSON's braces
    assert review.review(repo, "HEAD") == 1
    assert capsys.readouterr().err == (
        f"ERROR: {kept} records commit {head}'s basis check --full in a form this version of basis does not read, as another version wrote it:"
        " run `uv run basis check --full` again, with every change committed, which records it anew\n"
    )


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


@pytest.mark.parametrize(
    ("diff", "line"),
    [
        ("No earlier release to compare with.", "No earlier release to compare with"),
        ("## Changed since v0.1.0\n\n- `GET /prices`: added", "Changed since v0.1.0"),
        ("- `GET /prices`: `price` is now a decimal", "`GET /prices`: `price` is now a decimal"),
        ("\n\n- `GET /prices`: added", "changed"),
        ("", "changed"),
    ],
    ids=["a-sentence", "a-heading", "a-list-item", "a-blank-first-line", "empty"],
)
def test_the_contract_line_is_its_diffs_plain_first_line(declared: Path, capsys: pytest.CaptureFixture[str], diff: str, line: str) -> None:
    assert checked(declared, Part(contract=lambda _: diff)) == 0
    assert f"basis check --full: the alpha part's contract: {line}, as `uv run basis review` shows" in capsys.readouterr().err


DOMAIN = "def clamp(x: int, low: int, high: int) -> int:\n    if x < low:\n        return low\n    return x\n"
CLAMPED = "from rates import domain\n\n\ndef test_clamp() -> None:\n    assert domain.clamp(-1, 0, 10) == 0\n    assert domain.clamp(5, 0, 10) == 5\n"
PYTEST = '\n[tool.pytest.ini_options]\naddopts = "-ra --import-mode=importlib -p no:randomly"\ntestpaths = ["rates"]\n'


@pytest.mark.skipif(sys.platform != "linux", reason="mutmut runs in WSL or on Linux only")
def test_review_lists_a_mutant_of_a_domain_function_the_change_touches_that_survived(repo: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    """The change's mutants survive or die by the repo's own unit tests, which mutmut runs; a survivor fails nothing."""
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write(PYTEST)
    for name, text in {"rates/__init__.py": "", "rates/domain.py": DOMAIN, "rates/tests/__init__.py": "", "rates/tests/test_domain.py": CLAMPED}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--no-verify", "--message", "Clamp rates")
    assert checked(repo, Part()) == 0
    assert "basis check --full: mutation testing made 1 mutant of the 1 domain function the change touches: 1 survived, as `uv run basis review` lists" in lines(capsys)
    assert record(repo).passed
    assert review.review(repo, "HEAD") == 0
    printed = capsys.readouterr().out
    first = clone.of(repo)
    assert first is not None
    base = first.commit("HEAD^")
    assert f"### Mutation testing\n\nmutmut made 1 mutant of the domain functions the change since `{base}` touches, `rates.domain.clamp`: the tests killed 0, and 1 survived" in printed
    assert "| `rates.domain.clamp` | `rates.domain.x_clamp__mutmut_1` | `if x < low: -> if x <= low:` |" in printed


def test_without_a_domain_function_the_change_touches_mutation_testing_is_skipped(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checked(repo, Part()) == 0
    why = "the change touches no domain function" if sys.platform == "linux" else "mutmut runs in WSL or on Linux only, since it forks a process for each mutant: push from WSL to have it run"
    assert f"SKIPPED: mutation testing: {why}" in lines(capsys)
    assert review.review(repo, "HEAD") == 0
    assert f"### Mutation testing\n\nSkipped: {why}.\n" in capsys.readouterr().out


def test_a_defect_in_mutation_testing_fails_nothing_and_is_recorded(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:

    def broken(_repo: Path) -> review.MutationRecord:
        msg = "no mutants today"
        raise RuntimeError(msg)

    monkeypatch.setattr(full.mutation, "mutate", broken)
    assert checked(repo, Part()) == 0
    said = "RuntimeError: no mutants today, a defect in basis; the log record above holds its traceback"
    assert f"basis check --full: mutation testing failed, which fails nothing: {said}" in lines(capsys)
    assert record(repo).mutation.failure == said


@pytest.mark.parametrize(
    ("found", "printed"),
    [
        (
            review.MutationRecord(base="abc", functions=["rates.domain.clamp"], mutants=3, killed=3),
            ["mutmut made 3 mutants of the domain functions the change since `abc` touches, `rates.domain.clamp`: the tests killed 3, and none survived."],
        ),
        (
            review.MutationRecord(functions=["rates.domain.clamp"], mutants=4, killed=1, unsettled={"timeout": 1}, survived=2, shown=[review.MutantRecord(name="m1", function="f", change="a -> b")]),
            [
                "mutmut made 4 mutants of the domain functions the change in the first commit touches, `rates.domain.clamp`: the tests killed 1, 1 timeout, and 2 survived, which no test fails:",
                "",
                "| Function | Mutant | What it changed |",
                "|---|---|---|",
                "| `f` | `m1` | `a -> b` |",
                "",
                "And 1 more.",
            ],
        ),
        (
            review.MutationRecord(functions=["rates.domain.clamp"], failure="mutmut stopped"),
            ["mutmut failed before it tested a mutant, which fails nothing: mutmut stopped"],  # never "none survived"
        ),
        (
            review.MutationRecord(functions=["rates.domain.clamp"], mutants=2, killed=2, failure="mutmut stopped"),
            [
                "mutmut failed, which fails nothing: mutmut stopped",
                "",
                "mutmut made 2 mutants of the domain functions the change in the first commit touches, `rates.domain.clamp`: the tests killed 2, and none survived.",
            ],
        ),
    ],
    ids=["all-killed", "survivors", "mutmut-failed-first", "mutmut-failed-after"],
)
def test_review_says_what_mutation_testing_found(found: review.MutationRecord, printed: list[str]) -> None:
    record = review.Record(commit="c", folder="", checked_at=AT, checked=True, passed=True, versions={}, regress=review.RegressRecord(skipped="x"), qa=review.QaRecord(skipped="y"), mutation=found)
    assert "\n### Mutation testing\n\n" + "\n".join(printed) + "\n\n### Versions" in review.markdown(record)


def test_a_record_from_before_mutation_testing_still_reads() -> None:
    kept = review.Record(commit="c", folder="", checked_at=AT, checked=True, passed=True, versions={}, regress=review.RegressRecord(skipped="x"), qa=review.QaRecord(skipped="y"))
    older = kept.model_dump(mode="json", exclude={"mutation"})
    assert review.Record.model_validate(older).mutation.skipped == "the basis that checked this commit tested no mutants"


def test_a_vulnerable_dependency_or_a_missing_cooldown_fails_the_full_check_one_line_each(repo: Path, capsys: pytest.CaptureFixture[str], git: Git) -> None:
    (repo / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\ncoverage = 90\n', encoding="utf-8")
    git(repo, "commit", "--quiet", "--no-verify", "--all", "--message", "No cooldown")
    vulnerable = supply.Audited(problems=[("jinja2 2.11.2 has the known vulnerability PYSEC-2021-66", "upgrade it to 2.11.3 or later")], packages=2)
    assert full.full(repo, [], {"alpha": Part()}, run=passing, report=command.reported, folder=repo, clock=lambda: AT, audit=lambda _: vulnerable) == 1
    errors = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]
    assert errors == [
        'ERROR: pyproject.toml takes a dependency as soon as it is published: add exclude-newer = "7 days" under [tool.uv], as `basis new` writes it',
        "ERROR: jinja2 2.11.2 has the known vulnerability PYSEC-2021-66: upgrade it to 2.11.3 or later",
    ]
