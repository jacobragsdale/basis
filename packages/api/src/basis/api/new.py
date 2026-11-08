"""`basis-api new app NAME`: an API repo, from basis's shared templates, this package's own and its example service,
in its layers, with a job beside it, so a new repo matches the basis-api version that made it. Its package is the
app's name with each hyphen an underscore; its `pyproject.toml` declares both parts, `[tool.basis] parts = ["api",
"job"]`, the coverage floor and the release tags its contract is checked against. The first job comes from basis-job's
own generator, through `basis new job`, since basis-api imports nothing of basis-job's."""

import re
import shutil
from pathlib import Path

from basis.api.service import NAME
from basis.api.version import VERSION
from basis.core import new as core_new

_PACKAGE = Path(__file__).parent
_EXAMPLE = _PACKAGE / "example"
_TEMPLATES = _PACKAGE / "templates" / "repo"
_FOLDERS = {"basis-api": "packages/api", "basis-job": "packages/job"}
"""Each package's folder in a checkout of basis."""
_LEFT_OUT = ("__pycache__", "*.pyc", "ruff.toml", "__main__.py", "openapi.json", "test_openapi.py")
"""What a new app does not copy of the example: what writes and tests the example's own sample, which a repo's
`basis sync` writes at its root instead."""
_EXAMPLE_NAME = re.compile(r'name="accruals"')


def app(path: Path, name: str, job: str | None, *, index: str | None, framework_path: Path | None, install: bool = True) -> Path:  # noqa: PLR0913 -- where, what, and where its packages come from, by keyword
    """A new repo at `path` with the API `name`, copied from the example in its layers, and, with `job`, a job named
    so beside it; its dependencies installed and git initialised, the first commit the author's. basis-api and
    basis-job come from `index`, from a checkout of basis at `framework_path` installed editable, or, with neither, from
    basis's GitHub repository at this version's tag (see `basis.core.new.sources`). `install=False` leaves out
    `git init`, `uv sync`, the job and `basis sync`, which make the rest of the repo once it is installed."""
    if not NAME.fullmatch(name):
        msg = f"{name!r} is no app's name: give one of lower-case letters, digits and hyphens, starting with a letter"
        raise core_new.NewError(msg)
    package = name.replace("-", "_")
    _, minor, *_ = VERSION.split(".")
    values = {"repo": path.name, "app": name, "package": package, "job": job or "JOB", "version": VERSION, "next_minor": str(int(minor) + 1)}
    core_new.repo(path, templates=_TEMPLATES, values=values, distributions=_FOLDERS, version=VERSION, index=index, checkout=framework_path)
    _copy_example(path / package, package, name)
    (path / "jobs").mkdir()
    (path / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    if install:
        core_new.install(path)
        if job is not None:
            core_new.run(["uv", "run", "--quiet", "basis", "new", "job", job], path)
        core_new.run(["uv", "run", "--quiet", "basis", "sync"], path)  # both parts' files, and openapi.json from the installed service
    return path


def _copy_example(target: Path, package: str, name: str) -> None:
    shutil.copytree(_EXAMPLE, target, ignore=shutil.ignore_patterns(*_LEFT_OUT))
    for path in target.rglob("*"):
        if path.suffix in {".py", ".md"}:
            text = path.read_text(encoding="utf-8").replace("basis.api.example", package)
            path.write_text(_EXAMPLE_NAME.sub(f'name="{name}"', text), encoding="utf-8")
