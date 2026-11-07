"""The docs that ship with optim hang together: every page the skills and the contract point at exists, and every
relative link and anchor in the docs resolves."""

import re
from pathlib import Path

import pytest
from fakes import Basis

from optim import sync

PACKAGE = Path(__file__).parent.parent / "src" / "optim"
PAGES = sorted((PACKAGE / "docs").rglob("*.md"))
LINKED = [*PAGES, Path(__file__).parent.parent / "README.md", Path(__file__).parent.parent / "docs" / "design.md"]


def anchors(page: Path) -> set[str]:
    """GitHub's anchors for a page's headings."""
    return {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-") for heading in re.findall(r"^#+ (.+)$", page.read_text(encoding="utf-8"), re.MULTILINE)}


def test_every_page_the_skills_the_contract_and_a_new_repo_name_exists(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    """The skills, the contract block and the example's READMEs name pages as `basis docs optim/PAGE`; the new repo's
    README names them in its table's guide column."""
    guidance = [*(PACKAGE / "skills").glob("*/SKILL.md"), PACKAGE / "templates" / "contract.md", *(PACKAGE / "example").rglob("README.md"), PACKAGE / "templates" / "shared" / "README.md"]
    named = {page for file in guidance for page in re.findall(r"basis docs optim/([\w/-]+)", file.read_text(encoding="utf-8")) if page != "PAGE"}
    named |= set(re.findall(r"\| `((?:how-to/)?[\w-]+)` \|$", (PACKAGE / "templates" / "repo" / "README.md").read_text(encoding="utf-8"), re.MULTILINE))

    for page in sorted(named):
        assert basis("docs", f"optim/{page}") == 0, page
    assert named
    assert "# Build, replay and sweep your first model" in capsys.readouterr().out


@pytest.mark.parametrize("page", LINKED, ids=lambda page: page.name)
def test_every_relative_link_and_anchor_resolves(page: Path) -> None:
    for target, anchor in re.findall(r"\]\((?!https?://)([^)#\s]*)(?:#([^)\s]+))?\)", page.read_text(encoding="utf-8")):
        linked = (page.parent / target).resolve() if target else page
        assert linked.exists(), f"{page.name} links to {target}"
        if anchor:
            assert anchor in anchors(linked), f"{page.name} links to {target}#{anchor}"


SCHEDULED = re.compile(r"(?:Airflow|schedule|production)[^\n`]*`((?:basis|optim) run[^`]*)`", re.IGNORECASE)
"""A command a line says Airflow, a schedule or production runs, as `basis run ...` in backticks after it."""


def test_every_run_the_docs_and_templates_say_production_makes_publishes() -> None:
    """Only `--publish` writes orders, so a production command without it would write none."""
    root = Path(__file__).parent.parent
    files = [*PAGES, *(PACKAGE / "skills").rglob("*.md"), *(path for path in (PACKAGE / "templates").rglob("*") if path.is_file()), root / "README.md", *(root / "docs").glob("*.md")]
    commands = [(file.name, command) for file in files for command in SCHEDULED.findall(file.read_text(encoding="utf-8"))]

    assert {"port-a-legacy-model.md", "design.md"} <= {name for name, _ in commands}
    assert [(name, command) for name, command in commands if "--publish" not in command] == []
    assert "--publish" in sync.IMAGE, "the Dockerfile's line for the command a scheduler gives a model's container"
