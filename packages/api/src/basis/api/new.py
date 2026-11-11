"""`basis-api new app NAME`: an API repo, from basis's shared templates, this package's own and its example service,
in its layers, with a job beside it, so a new repo matches the basis-api version that made it. Its package is the
app's name with each hyphen an underscore; its `pyproject.toml` declares both parts, `[tool.basis] parts = ["api",
"job"]`, and the coverage floor. The first job comes from basis-job's
own generator, through `basis new job`, since basis-api imports nothing of basis-job's."""

import re
from pathlib import Path

from basis.api.service import NAME
from basis.api.version import VERSION
from basis.core import new as core_new

_PACKAGE = Path(__file__).parent
EXAMPLE = _PACKAGE / "example"
_TEMPLATES = _PACKAGE / "templates" / "repo"
_FOLDERS = {"basis-api": "packages/api", "basis-job": "packages/job"}
"""Each package's folder in a checkout of basis."""
EXAMPLE_FILES = (
    "__init__.py", "compute.py", "dev.py", "domain.py", "errors.py", "handlers.py", "repository.py", "resources.py", "service.py",
    "tests/__init__.py", "tests/test_compute.py", "tests/test_domain.py", "tests/test_handlers.py", "tests/test_repository.py",
)  # fmt: skip
"""What a new app copies of the example, by name, so a cache or a stray file in a checkout's example is never copied.
Left out: its `ruff.toml`, and what writes and tests the example's own samples (`__main__.py`, `openapi.json`,
`openapi.released.json`, which a release writes, `coupons.arrows`, `tests/test_openapi.py`), which a repo's `basis sync`
writes at its root instead."""
EXAMPLE_NAME = re.compile(r'name="accruals"')


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
    core_new.check_package(package, "the app's package")
    values = {"repo": path.name, "app": name, "package": package, "job": job or "JOB", "version": VERSION}
    with core_new.building(path) as folder:
        core_new.repo(folder, templates=_TEMPLATES, values=values, distributions=_FOLDERS, version=VERSION, index=index, checkout=framework_path)
        _copy_example(folder / package, package, name)
        (folder / "jobs").mkdir()
        (folder / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    if install:
        core_new.install(path)
        if job is not None:
            core_new.run(["uv", "run", "--quiet", "basis", "new", "job", job], path)
        core_new.run(["uv", "run", "--quiet", "basis", "sync"], path)  # both parts' files, and openapi.json from the installed service
    return path


def _copy_example(target: Path, package: str, name: str) -> None:
    for relative in EXAMPLE_FILES:
        text = EXAMPLE_NAME.sub(f'name="{name}"', (EXAMPLE / relative).read_text(encoding="utf-8").replace("basis.api.example", package))
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        (target / relative).write_text(text, encoding="utf-8")
