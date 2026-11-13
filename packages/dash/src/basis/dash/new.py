"""`basis-dash new app NAME`, a Dash app's repo, and `basis new page NAME`, a page in one.

A new repo is basis's shared templates, this package's own (`templates/repo/`) and its example app, the positions page
of a fixed income desk's holdings, on its synthetic fixtures, in its layers, with its tests; its package is the app's
name with each hyphen an underscore, its `pyproject.toml` declares `[tool.basis] parts = ["dash"]` and the coverage
floor, and `basis sync`, once the repo is installed, writes the rest. A new page is a module of the app's `pages`
package, with a layout and a callback that loads, and its test, added to the app's `pages=[...]`."""

import re
from pathlib import Path

from basis.core import new as core_new
from basis.dash.version import VERSION

_PACKAGE = Path(__file__).parent
EXAMPLE = _PACKAGE / "example"
_TEMPLATES = _PACKAGE / "templates" / "repo"
_FOLDERS = {"basis-dash": "packages/dash"}
"""The package's folder in a checkout of basis."""
NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
PAGE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
EXAMPLE_FILES = (
    "__init__.py", "app.py", "data.py", "domain.py", "dev.py", "pages/__init__.py", "pages/positions.py", "sql/holdings.sql", "sql/curve.sql",
    "tests/test_domain.py", "tests/test_positions.py",
)  # fmt: skip
"""What a new app copies of the example, by name, so a cache or a stray file in a checkout's example is never copied:
every dataset and rule, and its positions page alone, at `/`. Left out: its other pages and their test, its `ruff.toml`,
which a repo's `basis sync` writes at its root instead, and its fixtures, which go to the repo's `fixtures/`."""
FIXTURES = ("holdings.csv", "curve.csv")
STARTER_PAGES = "from {package}.pages import positions\n"
"""The starter's one page, as its `app.py` imports it."""


class NewError(core_new.NewError):
    """A name or a repo `new` cannot make what it was asked for in; the message ends with the fix."""


def app(path: Path, name: str, *, index: str | None, framework_path: Path | None, install: bool = True) -> Path:
    """A new repo at `path` with the Dash app `name`, copied from the example; its dependencies installed, git
    initialised, and `basis sync` run, the first commit the author's. basis-dash comes from `index`, from a checkout of
    basis at `framework_path` installed editable, or, with neither, from basis's GitHub repository at this version's tag
    (see `basis.core.new.sources`). `install=False` leaves out `git init`, `uv sync` and `basis sync`."""
    if not NAME.fullmatch(name):
        msg = f"{name!r} is no app's name: give one of lower-case letters, digits and hyphens, starting with a letter"
        raise NewError(msg)
    package = name.replace("-", "_")
    core_new.check_package(package, "the app's package")
    values = {"repo": path.name, "app": name, "package": package, "version": VERSION}
    with core_new.building(path) as folder:
        core_new.repo(folder, templates=_TEMPLATES, values=values, distributions=_FOLDERS, version=VERSION, index=index, checkout=framework_path)
        _copy_example(folder, package, name)
    if install:
        core_new.install(path)
        core_new.run(["uv", "run", "--quiet", "basis", "sync"], path)
    return path


def _copy_example(repo: Path, package: str, name: str) -> None:
    title = name.replace("-", " ").title()
    for relative in EXAMPLE_FILES:
        text = (EXAMPLE / relative).read_text(encoding="utf-8").replace("basis.dash.example", package)
        if relative == "app.py":
            text = re.sub(r"^from \S+\.pages import .+\n", STARTER_PAGES.format(package=package), text, flags=re.MULTILINE)
            text = re.sub(r"pages=\[[^\]]*\]", "pages=[positions.page]", text)
            text = text.replace('name="holdings", title="Fixed Income Holdings"', f'name="{name}", title="{title}"')
            text = re.sub(r"app = bd\.App\(\n\s+(.+)\n\)", r"app = bd.App(\1)", text)  # one line, as the formatter writes what fits
        if relative == "pages/positions.py":
            text = text.replace('    "/positions",\n', '    "/",\n')
        if relative == "dev.py":
            text = (
                text.replace("from pathlib import Path\n\n", "")
                .replace('FIXTURES = Path(__file__).parent / "fixtures"\n\n', "")
                .replace("fixtures.database(WAREHOUSE, FIXTURES)", "fixtures.database(WAREHOUSE)")
            )
        (repo / package / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / package / relative).write_text(text, encoding="utf-8")
    (repo / package / "tests" / "__init__.py").write_text("", encoding="utf-8")
    for fixture in FIXTURES:
        (repo / "fixtures").mkdir(exist_ok=True)
        (repo / "fixtures" / fixture).write_bytes((EXAMPLE / "fixtures" / fixture).read_bytes())


PAGE_MODULE = '''"""{title}: WHAT_THE_PAGE_SHOWS."""

import basis.dash as bd
from dash import Input, Output, callback

from {package}.data import SUMMARY, Summary

page = bd.page("/{path}", "{title}", filters=[bd.as_of("{path}-as-of", calendar="sifma-us")], body=[bd.card("{title}", bd.grid("{path}-grid", Summary, height=None, search=False), flush=True)])


@callback(Output("{path}-grid", "rowData"), Input("{path}-as-of", "value"))
async def show(as_of: str) -> bd.Rows:
    summary = await bd.load(SUMMARY, as_of=bd.day(as_of), book="", search="")
    return bd.rows(summary)
'''
PAGE_TEST = '''"""The {title} page on the app's fakes."""

import basis.dash as bd

from {package}.app import app


def test_the_{name}_page_shows_its_rows() -> None:
    with bd.testing.client(app) as client:
        answered = client.fire("{path}-grid.rowData", {{"{path}-as-of.value": "2026-11-12"}})
    assert answered["{path}-grid"]["rowData"]
'''


def page(repo: Path, name: str) -> Path:
    """A page `name` in the repo's app at `repo`: `PACKAGE/pages/NAME.py`, its test, and its place in the app's
    `pages=[...]`, after the others."""
    if not PAGE.fullmatch(name):
        msg = f"{name!r} is no page's name: give one of lower-case letters, digits and underscores, starting with a letter, as risk"
        raise NewError(msg)
    package = _package(repo)
    module, test, declared = repo / package / "pages" / f"{name}.py", repo / package / "tests" / f"test_{name}.py", repo / package / "app.py"
    if module.exists():
        msg = f"{module.relative_to(repo).as_posix()} exists already: give the new page another name"
        raise NewError(msg)
    text = declared.read_text(encoding="utf-8")
    imports = re.search(rf"^from {re.escape(package)}\.pages import (?P<names>[\w, ]+)$", text, re.MULTILINE)
    listed = re.search(r"pages=\[(?P<pages>[^\]]*)\]", text)
    if imports is None or listed is None:
        msg = f"{declared.relative_to(repo).as_posix()} has no `from {package}.pages import ...` line or no `pages=[...]`, where basis adds the page: add them, or write the page by hand"
        raise NewError(msg)
    names = sorted({*(found.strip() for found in imports.group("names").split(",")), name})  # as ruff sorts them
    text = text[: imports.start("names")] + ", ".join(names) + text[imports.end("names") :]
    listed = re.search(r"pages=\[(?P<pages>[^\]]*)\]", text)
    if listed is None:  # the import's line holds no `]`, so the list is where it was
        raise AssertionError(text)
    text = text[: listed.end("pages")] + f", {name}.page" + text[listed.end("pages") :]
    values = {"title": name.replace("_", " ").capitalize(), "package": package, "path": name.replace("_", "-"), "name": name}
    module.write_text(core_new.render(PAGE_MODULE, values), encoding="utf-8")
    test.write_text(PAGE_TEST.format(**values), encoding="utf-8")
    declared.write_text(text, encoding="utf-8")
    return module


def _package(repo: Path) -> str:
    """The package of the repo's one app, by its `basis.dash_apps` entry point in its `pyproject.toml`."""
    found = re.search(r'^\[project\.entry-points\."basis\.dash_apps"\]\s*\n\s*[\w-]+\s*=\s*"(?P<module>[\w.]+):', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
    if found is None:
        msg = 'the repo lists no app under [project.entry-points."basis.dash_apps"] in its pyproject.toml, so basis cannot tell where its pages go: list it, as holdings = "holdings.app:app"'
        raise NewError(msg)
    return str(found.group("module")).partition(".")[0]
