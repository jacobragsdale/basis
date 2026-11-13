"""basis owns a repo's ruff, ty and pytest settings (step 8.1): `basis sync` writes them where every declared part gives
its share, `basis check` refuses the tables in `pyproject.toml` that would sit beside them and a coverage floor under
basis's without its reason, and `basis review` lists each exception the repo takes."""

import json
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from basis.core import hooks, review, sync, tools
from basis.core.check import Runner, check
from basis.core.sync import AgentFiles
from basis.core.tools import Tools

type Git = Callable[..., str]
SHARE = AgentFiles(
    "basis-alpha",
    "the alpha block\n",
    {},
    tools=Tools(testpaths=("tests", "alphas"), per_file_ignores={"**/hook.py": ("ARG001",)}, markers=("slow: takes a while",), aliases={"basis.alpha": "ba"}, plugins_off=("alphas",)),
)
UNSHARED = AgentFiles("basis-beta", "the beta block\n", {})
PYPROJECT = '[tool.basis]\nparts = ["alpha"]\ncoverage = 90\n'


def runner(*, covered: float = 100.0) -> tuple[list[tuple[str, ...]], Runner]:
    """A runner whose every step passes, its pytest covering `covered` percent of the code."""
    ran: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], _folder: Path) -> int:
        ran.append(tuple(argv))
        for argument in argv:
            if argument.startswith("--cov-report=json:"):
                Path(argument.removeprefix("--cov-report=json:")).write_text(json.dumps({"totals": {"percent_covered": covered}}), encoding="utf-8")
        return 0

    return ran, run


def errors(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR: ")]


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    sync.write(tmp_path, [SHARE])
    git(tmp_path, "init", "--quiet")
    hooks.ensure(tmp_path)
    return tmp_path


def test_sync_writes_the_tools_settings_with_the_marker_where_every_part_gives_its_share(repo: Path) -> None:
    for name in tools.FILES:
        assert (repo / name).read_text(encoding="utf-8").startswith(f"# {sync.MARKER}"), name
    ruff = tomllib.loads((repo / "ruff.toml").read_text(encoding="utf-8"))
    assert (ruff["required-version"], ruff["lint"]["select"], ruff["lint"]["per-file-ignores"]["**/hook.py"]) == (f"=={tools.RUFF}", ["ALL"], ["ARG001"])
    assert tomllib.loads((repo / "ty.toml").read_text(encoding="utf-8"))["rules"] == {"all": "error"}
    pytest_ini = (repo / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths = tests alphas\n" in pytest_ini
    assert "    slow: takes a while\n" in pytest_ini


def test_a_repo_with_a_part_that_gives_no_share_keeps_its_own_settings(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    sync.write(tmp_path, [SHARE, UNSHARED])
    assert [name for name in tools.FILES if (tmp_path / name).exists()] == []


@pytest.mark.parametrize("table", ['[tool.ruff.lint]\nignore = ["ANN", "S"]\n', '[tool.ty.rules]\nall = "ignore"\n', '[tool.pytest.ini_options]\naddopts = "-p no:basis"\n'])
def test_a_table_that_would_weaken_basiss_settings_fails_the_check(repo: Path, table: str, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pyproject.toml").write_text(f"{PYPROJECT}\n{table}", encoding="utf-8")
    _, run = runner()
    assert check(repo, [SHARE], run=run) == 1
    name = table.removeprefix("[tool.").split("]")[0].split(".")[0]
    file = tools.FILES[tools.OWNED.index(name)]
    fix = "delete the table, and give each exception the repo needs as [tool.basis] exceptions, with its reason"
    assert errors(capsys) == [f"ERROR: pyproject.toml has [tool.{name}], and basis writes the repo's {name} settings in {file}, which would read beside it: {fix}"]


def test_each_exception_with_its_reason_is_written_for_its_path_alone(repo: Path) -> None:
    exceptions = (
        '\nexceptions = [{ rule = "PLR0913", path = "alphas/big/hook.py", reason = "its signature is the hook protocol\'s" },'
        ' { rule = "possibly-missing-attribute", path = "alphas/legacy/**", reason = "the port keeps the system\'s names" }]\n'
    )
    (repo / "pyproject.toml").write_text(PYPROJECT.rstrip() + exceptions, encoding="utf-8")
    sync.write(repo, [SHARE])
    ruff = tomllib.loads((repo / "ruff.toml").read_text(encoding="utf-8"))
    assert ruff["lint"]["per-file-ignores"]["alphas/big/hook.py"] == ["PLR0913"]
    ty = tomllib.loads((repo / "ty.toml").read_text(encoding="utf-8"))
    assert ty["overrides"] == [{"include": ["alphas/legacy/**"], "rules": {"possibly-missing-attribute": "ignore"}}]
    listed = review.exceptions_markdown(repo / "pyproject.toml")
    assert "| PLR0913 | `alphas/big/hook.py` | its signature is the hook protocol's |" in listed


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ('{ rule = "PLR0913", path = "x.py" }', "reason: Field required"),
        ('{ rule = "PLR0913", path = "x.py", reason = "short" }', "reason: String should have at least 10 characters"),
        ('{ rule = "not a rule!", path = "x.py", reason = "a reason long enough" }', "which is neither a ruff rule's code"),
        ('{ rule = "PLR0913", path = "x.py", reason = "a reason long enough", why = "extra" }', "why: Extra inputs are not permitted"),
    ],
)
def test_an_exception_without_its_reason_or_a_rule_is_refused(repo: Path, entry: str, says: str) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT.rstrip() + f"\nexceptions = [{entry}]\n", encoding="utf-8")
    with pytest.raises(tools.ToolsError, match=says):
        tools.exceptions(repo / "pyproject.toml")
    assert any("exceptions[0]" in reason for reason, _ in sync.plan(repo, [SHARE]).problems())


def test_a_coverage_floor_under_basiss_fails_unless_the_repo_says_why(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT.replace("coverage = 90", "coverage = 10"), encoding="utf-8")
    _, run = runner(covered=50)
    assert check(repo, [SHARE], run=run) == 1
    fix = 'raise it, or say why as [tool.basis] exceptions, { rule = "coverage", path = ".", reason = "..." }, which basis review lists'
    assert errors(capsys) == [f"ERROR: [tool.basis] coverage is 10, under the 90 basis holds a repo's unit tests to: {fix}"]
    reason = '\nexceptions = [{ rule = "coverage", path = ".", reason = "a port whose legacy half has no tests yet" }]\n'
    (repo / "pyproject.toml").write_text(PYPROJECT.replace("coverage = 90", "coverage = 10").rstrip() + reason, encoding="utf-8")
    sync.write(repo, [SHARE])
    assert check(repo, [SHARE], run=run) == 0
    assert "The coverage floor is 10%, under basis's 90%." in review.exceptions_markdown(repo / "pyproject.toml")


def test_a_parts_import_aliases_and_the_pytest_plugins_it_turns_off_are_written(repo: Path) -> None:
    ruff = tomllib.loads((repo / "ruff.toml").read_text(encoding="utf-8"))
    assert ruff["lint"]["flake8-import-conventions"]["extend-aliases"] == {"basis.alpha": "ba"}
    addopts = next(line for line in (repo / "pytest.ini").read_text(encoding="utf-8").splitlines() if line.startswith("addopts = "))
    assert addopts.endswith(" -p no:alphas")
