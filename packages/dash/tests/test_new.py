"""`basis-dash new app NAME` and `basis new page NAME`, and what `basis sync` writes for a Dash app's repo: its block of
`AGENTS.md`, its skills, its Dockerfile line, and its share of the tool settings. The example's fixtures are the ones
its generator makes. These make a repo without installing it; a repo installed, checked and committed through its
hook is the root `AGENTS.md`'s commands, which CI's generated-repo job runs too."""

from pathlib import Path

import example_fixtures
import pytest

from basis.core import sync
from basis.dash import new
from basis.dash import sync as dash_sync


def _repo(tmp_path: Path) -> Path:
    return new.app(tmp_path / "desk-app", "desk-app", index=None, framework_path=Path(__file__).parents[3], install=False)


def test_a_new_app_is_the_example_in_its_layers_on_its_fixtures(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    package = repo / "desk_app"
    assert sorted(path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()) == sorted([*new.EXAMPLE_FILES, "tests/__init__.py"])
    assert (repo / "fixtures" / "holdings.csv").read_bytes() == (new.EXAMPLE / "fixtures" / "holdings.csv").read_bytes()
    assert 'name="desk-app", title="Desk App"' in (package / "app.py").read_text(encoding="utf-8")
    assert "basis.dash.example" not in "".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
    assert "fixtures.database(WAREHOUSE)}\n" in (package / "dev.py").read_text(encoding="utf-8")  # the repo's fixtures/, not the package's
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'desk-app = "desk_app.app:app"' in pyproject
    assert 'parts = ["dash"]' in pyproject


def test_a_name_that_is_no_apps_is_refused(tmp_path: Path) -> None:
    with pytest.raises(new.NewError, match=r"'Desk' is no app's name"):
        new.app(tmp_path / "Desk", "Desk", index=None, framework_path=None, install=False)


def test_a_new_page_is_written_tested_and_listed_in_the_apps_pages(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    made = new.page(repo, "risk_limits")
    assert made == repo / "desk_app" / "pages" / "risk_limits.py"
    assert 'bd.page("/risk-limits", "Risk limits"' in made.read_text(encoding="utf-8")
    assert (repo / "desk_app" / "tests" / "test_risk_limits.py").is_file()
    declared = (repo / "desk_app" / "app.py").read_text(encoding="utf-8")
    assert "from desk_app.pages import positions, risk_limits\n" in declared
    assert "pages=[positions.page, risk_limits.page]" in declared
    with pytest.raises(new.NewError, match=r"desk_app/pages/risk_limits.py exists already"):
        new.page(repo, "risk_limits")
    with pytest.raises(new.NewError, match=r"'Risk' is no page's name"):
        new.page(repo, "Risk")


def test_sync_writes_the_dash_parts_block_skills_image_and_tool_settings(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.basis]\nparts = ["dash"]\ncoverage = 90\n', encoding="utf-8")
    sync.write(tmp_path, [dash_sync.agent_files()])
    assert "## basis-dash" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert sorted(path.name for path in (tmp_path / ".agents" / "skills").iterdir()) == ["dash-add-dataset", "dash-add-page", "dash-why-failed"]
    assert "#   a Dash app: serve\n" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert '"basis.dash" = "bd"' in (tmp_path / "ruff.toml").read_text(encoding="utf-8")
    assert " -p no:dash\n" in (tmp_path / "pytest.ini").read_text(encoding="utf-8")


def test_the_examples_fixtures_are_the_ones_its_generator_makes() -> None:
    assert (example_fixtures.FIXTURES / "holdings.csv").read_text(encoding="utf-8") == example_fixtures.text()
