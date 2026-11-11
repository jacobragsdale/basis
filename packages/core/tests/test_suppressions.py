"""What is left to an agent once basis owns the tools' settings (step 8.2): a suppression in a line, a skipped test, a
deleted one. A suppression with no reason fails `basis check`, in `allow-mocks`'s `-- WHY` form, and `basis review`
lists what a change adds of them, and the tests it removes, against where it branched."""

from collections.abc import Callable
from pathlib import Path

import pytest

from basis.core import gates, review

type Git = Callable[..., str]


@pytest.mark.parametrize(
    ("line", "bare"),
    [
        ("x = eval(given)  # noqa: S307\n", True),
        ("x = eval(given)  # noqa\n", True),
        ("x = eval(given)  # noqa: S307 -- the rule engine's own expressions, which no user writes\n", False),
        ("x = value.attr  # ty: ignore[unresolved-attribute]\n", True),
        ("x = value.attr  # ty: ignore[unresolved-attribute] -- pyarrow is untyped\n", False),
        ("if never:  # pragma: no cover\n    pass\n", True),
        ("x = 1  # type: ignore\n", True),
        ("text = '# noqa'  # a string, not a comment\n", False),
    ],
)
def test_a_suppression_without_its_reason_is_refused(tmp_path: Path, line: str, *, bare: bool) -> None:
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "x.py").write_text(f"given = '1'\nnever = False\nvalue = object()\n{line}", encoding="utf-8")
    found = gates.suppressions(tmp_path)
    assert bool(found) is bare
    if bare:
        [(reason, fix)] = found
        assert reason.startswith("jobs/x.py:4 ")
        assert fix.startswith("say why after it, as ")


@pytest.mark.parametrize(
    ("test", "bare"),
    [
        ("@pytest.mark.skip\ndef test_a() -> None: ...\n", True),
        ("@pytest.mark.skip()\ndef test_a() -> None: ...\n", True),
        ("@pytest.mark.xfail\ndef test_a() -> None: ...\n", True),
        ("@pytest.mark.skip(reason='the vendor's sandbox is down until 2026-12')\ndef test_a() -> None: ...\n", False),
        ("@pytest.mark.skipif(True, reason='windows only')\ndef test_a() -> None: ...\n", False),
        ("def test_a() -> None:\n    pytest.skip()\n", True),
        ("def test_a() -> None:\n    pytest.skip('no Mongo here')\n", False),
    ],
)
def test_a_skip_without_its_reason_is_refused(tmp_path: Path, test: str, *, bare: bool) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(f"import pytest\n\n\n{test}", encoding="utf-8")
    assert bool(gates.suppressions(tmp_path)) is bare


def test_review_lists_the_suppressions_and_skips_a_change_adds_and_the_tests_it_removes(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_kept() -> None: ...\n\n\ndef test_gone() -> None: ...\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "first")
    git(tmp_path, "checkout", "--quiet", "-b", "change")
    (tmp_path / "tests" / "test_x.py").write_text("import pytest\n\n\n@pytest.mark.skip(reason='flaky upstream')\ndef test_kept() -> None: ...\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("x = eval('1')  # noqa: S307 -- a constant\n", encoding="utf-8")
    (tmp_path / "tests" / "test_y.py").write_text("def test_y(monkeypatch) -> None:\n    monkeypatch.setattr('os.sep', '/')\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "change")
    listed = review.added_markdown(tmp_path, "HEAD")
    assert "| `tests/test_x.py` | a skip | `@pytest.mark.skip(reason='flaky upstream')` |" in listed
    assert "| `x.py` | a suppression | `x = eval('1') # noqa: S307 -- a constant` |" in listed  # a cell's spaces collapsed
    assert "| `tests/test_x.py` | a test removed | `def test_gone() -> None: ...` |" in listed
    assert "| `tests/test_y.py` | a patch | `monkeypatch.setattr('os.sep', '/')` |" in listed
