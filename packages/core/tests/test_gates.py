"""The gates `basis check` holds a repo's tests to: a coverage floor, no mocks, and no sockets in unit tests, each
failure one line with its fix."""

import json
from pathlib import Path

import pytest

from basis.core import gates

FLOOR_FIX = "add `coverage = 90` to it, the percentage of the code's lines and branches its unit tests must cover, as every generator writes it"
FAKE = "build what the test runs on a fake, from basis's fakes or the repo's own; where none can stand in, give the file a line `# basis: allow-mocks -- WHY`"


def project(repo: Path, table: str) -> Path:
    (repo / "pyproject.toml").write_text(f'[tool.basis]\nparts = ["alpha"]\n{table}', encoding="utf-8")
    return repo


def test_the_floor_is_the_repos_whole_percentage(tmp_path: Path) -> None:
    assert gates.floor(project(tmp_path, "coverage = 85\n")) == 85


def test_a_repo_that_gives_no_floor_is_told_to_add_the_generators(tmp_path: Path) -> None:
    assert gates.floor(project(tmp_path, "")) == ("pyproject.toml's [tool.basis] gives no coverage floor", FLOOR_FIX)


@pytest.mark.parametrize("given", ["0", "101", "true", "90.5", '"90"'])
def test_a_floor_that_is_no_whole_percentage_is_refused(tmp_path: Path, given: str) -> None:
    found = gates.floor(project(tmp_path, f"coverage = {given}\n"))
    assert isinstance(found, tuple)
    assert found[1] == "give a whole percentage from 1 to 100, as `coverage = 90`"


def mocking(repo: Path, name: str, text: str) -> list[tuple[str, str]]:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return gates.mocks(repo)


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("from unittest import mock\n", "unittest.mock"),
        ("from unittest.mock import patch\n", "unittest.mock"),
        ("import unittest.mock\n", "unittest.mock"),
        ("import unittest\n\nunittest.mock.patch('x')\n", "unittest.mock"),
        ("import mock\n", "mock"),
        ("from pytest_mock import MockerFixture\n", "pytest_mock"),
        ("def test_it(mocker) -> None: ...\n", "pytest-mock's mocker fixture"),
        ("async def test_it(module_mocker) -> None: ...\n", "pytest-mock's module_mocker fixture"),
    ],
)
def test_a_test_that_mocks_is_refused(tmp_path: Path, text: str, what: str) -> None:
    [(reason, fix)] = mocking(tmp_path, "tests/test_prices.py", text)
    assert reason.startswith("tests/test_prices.py:")
    assert reason.endswith(f" uses {what}, and a test's only double is a fake")
    assert fix == FAKE


@pytest.mark.parametrize("name", ["test_prices.py", "jobs/rates/prices_test.py", "conftest.py", "api/tests/builders.py"])
def test_every_kind_of_test_module_is_read(tmp_path: Path, name: str) -> None:
    assert len(mocking(tmp_path, name, "from unittest import mock\n")) == 1


def test_code_that_is_no_test_is_not_read(tmp_path: Path) -> None:
    assert mocking(tmp_path, "api/doubles.py", "from unittest import mock\n") == []


def test_a_virtual_environments_tests_are_not_the_repos(tmp_path: Path) -> None:
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "pyvenv.cfg").write_text("", encoding="utf-8")
    assert mocking(tmp_path, "env/lib/tests/test_mock.py", "from unittest import mock\n") == []


def test_a_file_that_says_why_it_mocks_may(tmp_path: Path) -> None:
    text = "# basis: allow-mocks -- the vendor's SDK has no fake, and its client opens a socket as it is made\nfrom unittest import mock\n"
    assert mocking(tmp_path, "tests/test_vendor.py", text) == []


@pytest.mark.parametrize("line", ["# basis: allow-mocks", "# basis: allow-mocks --", "# basis: allow-mocks --  "])
def test_a_file_that_allows_mocks_without_a_reason_is_refused(tmp_path: Path, line: str) -> None:
    [(reason, fix)] = mocking(tmp_path, "tests/test_vendor.py", f"{line}\nfrom unittest import mock\n")
    assert reason == "tests/test_vendor.py:2 uses unittest.mock, and its `# basis: allow-mocks` line gives no reason"
    assert fix == "say why after it, as `# basis: allow-mocks -- WHY`"


def test_a_test_that_does_not_parse_is_left_to_ruff(tmp_path: Path) -> None:
    assert mocking(tmp_path, "tests/test_broken.py", "from unittest import mock\ndef (:\n") == []


def test_pytest_measures_coverage_by_basiss_settings_and_writes_where_basis_reads(tmp_path: Path) -> None:
    with gates.measured(90) as measured:
        argv = measured.argv()
        settings = (measured.folder / "coveragerc").read_text(encoding="utf-8")
        assert argv == (f"--basis-gates={measured.gates}", "--cov", f"--cov-config={measured.folder / 'coveragerc'}", "--cov-report=term-missing:skip-covered", f"--cov-report=json:{measured.report}")
        folder = measured.folder
    assert "branch = True\nsource = .\n" in settings
    assert f"data_file = {folder / '.coverage'}\n" in settings  # never the repo's folder
    assert "    **/repository.py\n    **/repository/**\n" in settings
    assert not folder.exists()
    assert not list(tmp_path.iterdir())


def test_without_a_floor_pytest_is_only_told_where_to_write_its_sockets() -> None:
    with gates.measured(None) as measured:
        assert measured.argv() == (f"--basis-gates={measured.gates}",)
        assert measured.found(0).problems == []


def write(measured: gates.Measured, *, covered: float | None = None, sockets: dict[str, list[str]] | None = None, failed: tuple[str, ...] = ()) -> None:
    if covered is not None:
        measured.report.write_text(json.dumps({"totals": {"percent_covered": covered}}), encoding="utf-8")
    measured.gates.write_text(json.dumps({"exit_status": 1 if failed else 0, "failed": list(failed), "sockets": sockets or {}}), encoding="utf-8")


def test_coverage_under_the_floor_is_its_line() -> None:
    with gates.measured(90) as measured:
        write(measured, covered=87.5)
        found = measured.found(0)
    assert found.problems == [
        (
            "the unit tests cover 87.5% of the code's lines and branches, under the floor of 90% that [tool.basis] coverage gives",
            "test what the coverage report above lists as missing; a repository module counts for nothing here, since the integration tier covers it",
        )
    ]


def test_coverage_at_the_floor_passes() -> None:
    with gates.measured(90) as measured:
        write(measured, covered=90.0)
        assert measured.found(0).problems == []


def test_a_passing_run_that_wrote_no_coverage_report_is_refused() -> None:
    """As a repo's `--no-cov` in its pytest options would make it, which would leave the floor unchecked."""
    with gates.measured(90) as measured:
        [(reason, _)] = measured.found(0).problems
    assert reason == "pytest wrote no coverage report, so basis cannot hold the unit tests to their floor"


def test_a_failing_run_that_wrote_no_coverage_report_is_pytests_to_explain() -> None:
    with gates.measured(90) as measured:
        assert measured.found(2) == gates.Found([], explained=False)


def test_each_test_that_opened_a_socket_is_a_line_and_explains_the_failure() -> None:
    with gates.measured(90) as measured:
        write(measured, covered=100.0, sockets={"tests/test_qa.py::test_reaches_qa": ["connect 10.0.0.5:1433"]}, failed=("tests/test_qa.py::test_reaches_qa",))
        found = measured.found(1)
    assert found.explained
    assert found.problems == [
        (
            "tests/test_qa.py::test_reaches_qa opened a socket (connect 10.0.0.5:1433), and a unit test reaches nothing outside its process",
            "give it a fake of what it reaches, from basis's fakes or the repo's own, or, for a test that needs a real database or service, mark it @pytest.mark.integration",
        )
    ]


def test_a_failure_beside_a_socket_still_needs_pytests_line() -> None:
    with gates.measured(90) as measured:
        write(measured, covered=100.0, sockets={"tests/test_qa.py::test_a": ["connect 10.0.0.5:1433"]}, failed=("tests/test_qa.py::test_a", "tests/test_qa.py::test_b"))
        assert not measured.found(1).explained
