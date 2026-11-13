"""The example, as an API repo's own package: its copy passes every gate `basis check` holds, and a copy that breaks
any one rule fails it with that rule's fix, one test for each. The copy is `accruals`, at the root of a repo of its
own, which declares the api part and a coverage floor of 90; `basis check` runs in this process, its lockfile, ruff
and ty steps passing as given, since they hold no rule here, and its pytest on the copy, in a process of its own, for
the rules pytest holds, coverage and sockets, and as given for the rest."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from basis.api import example, fuzz, loading
from basis.core import check, command, mutation, review

type Git = Callable[..., str]
"""conftest's `git` fixture."""

pytestmark = pytest.mark.usefixtures("logging_restored")

EXAMPLE = Path(example.__file__).parent
SAMPLE = ("__main__.py", "openapi.json", "ruff.toml", "test_openapi.py", "__pycache__")
"""The example's own: the sample basis-ui pins and its test, which are no repo's."""
PYPROJECT = """[project]
name = "accruals-api"
version = "0.1.0"
requires-python = ">=3.14"

[project.entry-points."basis.apis"]
accruals = "accruals.service:service"

[tool.basis]
parts = ["api"]
coverage = 90

[tool.uv]
exclude-newer = "7 days"
"""
"""An API repo's own: pytest's settings are basis's, in the pytest.ini `basis sync` writes; the dependency cooldown
`basis new` writes, which `basis check --full` checks (step 12.9)."""


class Steps:
    """`basis check`'s tools: pytest run for real on the copy where a test needs it, and as passing otherwise, with
    the reports basis reads; every other tool passes."""

    def __init__(self, *, real: bool) -> None:
        self.real = real

    def __call__(self, argv: Sequence[str], folder: Path) -> int:
        if "pytest" not in argv:
            return 0
        arguments = list(argv[argv.index("pytest") + 1 :])
        if self.real:
            kept = {name: value for name, value in os.environ.items() if name not in check.COVERAGE_VARIABLES}  # as check.run_command keeps them
            environment = {**kept, "PYTHONPATH": str(folder), "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:dash"}  # Dash's plugin, which this workspace has and an API repo does not
            return subprocess.run([sys.executable, "-m", "pytest", *arguments], cwd=folder, env=environment, check=False).returncode
        for argument in arguments:
            if argument.startswith("--cov-report=json:"):
                Path(argument.removeprefix("--cov-report=json:")).write_text(json.dumps({"totals": {"percent_covered": 100.0}}), encoding="utf-8")
        return 0


@pytest.fixture
def repo(tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The example as an API repo's package, `accruals`, with every file `basis sync` writes, in a clone of its own."""
    root = tmp_path / "accruals-api"
    shutil.copytree(EXAMPLE, root / "accruals", ignore=shutil.ignore_patterns(*SAMPLE))
    for path in (root / "accruals").rglob("*.py"):
        path.write_text(path.read_text(encoding="utf-8").replace("basis.api.example", "accruals"), encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(loading, "entry_points", lambda **_: [EntryPoint(name="accruals", value="accruals.service:service", group=loading.GROUP)])
    monkeypatch.setattr(fuzz, "EXAMPLES", 5)  # enough to find a route that answers 500 to every request; the example's own test makes the full run
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # so an edit of the same length within a second is never read from stale bytecode
    git(root, "init", "--quiet")
    assert basis(root, "sync") == 0
    yield root
    forget()


def forget() -> None:
    """Forget what this process imported of the copy, so that `basis check` imports it as a test has edited it."""
    for name in [name for name in sys.modules if name.partition(".")[0] == "accruals"]:
        del sys.modules[name]


def basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())


def checked(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, real: bool = False, full: bool = False) -> list[str]:
    """`basis check` on the repo, or with `full` `basis check --full`, which adds Schemathesis's requests, its
    mutation testing left out: its ERROR lines, which are none exactly when it passes."""
    capsys.readouterr()
    forget()
    monkeypatch.setattr(check, "run_command", Steps(real=real))
    monkeypatch.setattr(mutation, "mutate", lambda _: review.MutationRecord(skipped="the gates' tests leave it out"))
    code = basis(repo, "check", *(["--full"] if full else []))
    found = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]
    assert (code == 0) == (not found), found
    return found


def edit(repo: Path, name: str, old: str, new: str) -> None:
    path = repo / "accruals" / name
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_the_example_passes_every_gate(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Its layers, its app on its fakes and Schemathesis's requests, its document and agent files, and its unit tests,
    which cover 90% of it or more, mock nothing, and open no socket."""
    monkeypatch.setattr(fuzz, "EXAMPLES", 50)
    assert checked(repo, monkeypatch, capsys, real=True, full=True) == []


def test_a_domain_module_that_imports_up_the_layers_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, "domain.py", "from pydantic import AwareDatetime", "from accruals import handlers  # noqa: F401 -- the import under test\nfrom pydantic import AwareDatetime")
    [line] = checked(repo, monkeypatch, capsys)
    assert line.startswith("ERROR: accruals/domain.py:11 imports accruals.handlers into accruals.domain: a domain module imports a handlers module, above it")
    assert line.endswith(": move what accruals.domain needs of accruals.handlers down into accruals.domain, or into a layer below it, or move the code that needs it up into a handlers module")


def test_a_domain_module_that_reaches_io_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, "domain.py", "from pydantic import AwareDatetime", "from basis import api  # noqa: F401 -- the import under test\nfrom pydantic import AwareDatetime")
    [line] = checked(repo, monkeypatch, capsys)
    assert line == (
        "ERROR: accruals/domain.py:11 imports basis into accruals.domain, and a domain module stays pure: no I/O, no web framework and nothing of basis:"
        " take what it needs from basis as arguments, from a handler or a repository, which import it, so the same rules run in a job"
    )


def test_a_handler_that_calls_a_resources_method_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(
        repo,
        "handlers.py",
        "    return await repository.bond(warehouse, security)",
        "    return await warehouse.fetch_one(Bond, repository.BOND, security=security) or await repository.bond(warehouse, security)",
    )
    [line] = checked(repo, monkeypatch, capsys)
    assert line.startswith("ERROR: accruals/handlers.py:")
    assert line.endswith(
        ": bond calls warehouse.fetch_one, a method of what api.given gives it, and a handler passes a resource on and never calls it:"
        " call it in a function of repository.py that takes warehouse as an argument, and call that from bond"
    )


def test_unit_tests_that_cover_less_than_the_floor_fail(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "accruals" / "tests" / "test_handlers.py").unlink()
    [line] = checked(repo, monkeypatch, capsys, real=True)
    assert line.startswith("ERROR: the unit tests cover ")
    assert line.endswith(
        "% of the code's lines and branches, under the floor of 90% that [tool.basis] coverage gives:"
        " test what the coverage report above lists as missing; a repository module counts for nothing here, since the integration tier covers it"
    )


def test_a_repo_without_a_floor_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT.replace("coverage = 90\n", ""), encoding="utf-8")
    assert checked(repo, monkeypatch, capsys) == [
        (
            "ERROR: pyproject.toml's [tool.basis] gives no coverage floor:"
            " add `coverage = 90` to it, the percentage of the code's lines and branches its unit tests must cover, as every generator writes it"
        )
    ]


def test_a_test_that_mocks_fails_unless_it_says_why(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mocked = repo / "accruals" / "tests" / "test_mocked.py"
    mocked.write_text("from unittest import mock\n\n\ndef test_a_sentinel() -> None:\n    assert mock.sentinel.price is mock.sentinel.price\n", encoding="utf-8")
    assert checked(repo, monkeypatch, capsys) == [
        (
            "ERROR: accruals/tests/test_mocked.py:1 uses unittest.mock, and a test's only double is a fake:"
            " build what the test runs on a fake, from basis's fakes or the repo's own; where none can stand in, give the file a line `# basis: allow-mocks -- WHY`"
        )
    ]
    mocked.write_text("# basis: allow-mocks -- the vendor's SDK has no fake\n" + mocked.read_text(encoding="utf-8"), encoding="utf-8")
    assert checked(repo, monkeypatch, capsys) == []


def test_a_unit_test_that_opens_a_socket_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    reaching = "import socket\n\n\ndef test_reaches_the_warehouse() -> None:\n    socket.create_connection(('192.0.2.1', 1433), timeout=5).close()\n"
    (repo / "accruals" / "tests" / "test_reaching.py").write_text(reaching, encoding="utf-8")
    assert checked(repo, monkeypatch, capsys, real=True) == [
        (
            "ERROR: accruals/tests/test_reaching.py::test_reaches_the_warehouse opened a socket (connect 192.0.2.1:1433), and a unit test reaches nothing outside its process:"
            " give it a fake of what it reaches, from basis's fakes or the repo's own, or, for a test that needs a real database or service, mark it @pytest.mark.integration"
        )
    ]


def test_an_app_that_does_not_build_on_its_fakes_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, "dev.py", "resources.WAREHOUSE: Database(answers={repository.BOND: Respond(_bond), repository.RECORD: 1}), ", "")
    assert checked(repo, monkeypatch, capsys) == [
        (
            "ERROR: service accruals's app does not build on the fakes it declares:"
            " accruals.dev:fakes fakes none of warehouse, and service accruals runs on fakes alone here: return a fake for each database and other service it declares"
        )
    ]


def test_a_route_that_answers_500_fails_check_full_with_the_request_that_shows_it(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setattr(fuzz, "FAILURES", tmp_path / "found")
    edit(repo, "handlers.py", "    return domain.conventions()", "    return [domain.conventions()[3]]  # there are three")
    assert checked(repo, monkeypatch, capsys) == []
    [line] = checked(repo, monkeypatch, capsys, full=True)
    written = tmp_path / "found" / "accruals-conventions.json"
    assert line == (
        "ERROR: Schemathesis found GET /conventions answers a request its document allows as its document does not say, Server error (ST101); Undocumented status code (ST103),"
        f" to the request in {written}: make it again with `uv run basis call conventions {shlex.quote(f'@{written}')} --fake`, which answers as the route does, and fix the route,"
        " or declare the error it answers with in its errors=[...]"
    )


def test_a_route_that_declares_too_little_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, "service.py", 'api.get("/conventions", handlers.conventions, deadline_s=1,', 'api.get("/conventions", handlers.conventions, deadline_s=0,')
    [line] = checked(repo, monkeypatch, capsys)
    assert line == ("ERROR: basis cannot make openapi.json, since conventions's deadline is 0: give it deadline_s, the seconds the route has to answer, above 0: then run `uv run basis sync`")


def test_an_openapi_json_the_code_no_longer_makes_fails(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    edit(repo, "handlers.py", '"""The bond\'s coupons, from its first to its last, which is paid at maturity, as a table: a frame of them."""', '"""The bond\'s coupons, first to last."""')
    assert checked(repo, monkeypatch, capsys) == ["ERROR: openapi.json does not match what the installed basis writes: run `uv run basis sync`, which rewrites or removes each"]


def test_agent_files_that_differ_from_the_installed_versions_fail(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    agents = repo / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8").replace("Rules to keep:", "Rules to bend:"), encoding="utf-8")
    assert checked(repo, monkeypatch, capsys) == ["ERROR: AGENTS.md does not match what the installed basis writes: run `uv run basis sync`, which rewrites or removes each"]
