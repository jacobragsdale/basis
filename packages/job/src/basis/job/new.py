"""`etl new repo` and `etl new job`: a job repo, or a job in one, from basis's shared templates, this package's own and
its example, so a new repo matches the basis-job version that made it. A repo's package is `jobs`, each job a folder
in it."""

import shutil
from pathlib import Path

from basis.core import new as core_new
from basis.core.tables import IDENTIFIER
from basis.job import sync
from basis.job.job import GROUP
from basis.job.version import VERSION

_PACKAGE = Path(__file__).parent
_EXAMPLE = _PACKAGE / "example"
_TEMPLATES = _PACKAGE / "templates" / "repo"
_FOLDER = "packages/job"
"""basis-job's folder in a checkout of basis."""


def repo(path: Path, job_name: str, *, index: str | None, framework_path: Path | None, install: bool = True) -> Path:
    """A new repo at `path` with one job, its dependencies installed and git initialised; the first commit is the
    author's. basis-job, and basis-core through it, come from `index`, from a checkout of basis at `framework_path`
    installed editable, or, with neither, from basis's GitHub repository at this version's tag, which needs a release
    (see `basis.core.new.sources`). `install=False` leaves out `git init` and `uv sync`."""
    _check_job_name(job_name)
    _, minor, *_ = VERSION.split(".")
    values = {"repo": path.name, "job": job_name, "version": VERSION, "next_minor": str(int(minor) + 1)}
    core_new.repo(path, templates=_TEMPLATES, values=values, distributions={"basis-job": _FOLDER}, version=VERSION, index=index, checkout=framework_path)
    (path / "jobs").mkdir()
    (path / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    _copy_example(path / "jobs" / job_name, job_name)
    sync.write(path)
    if install:
        core_new.install(path)
    return path


def job(repo_path: Path, name: str, *, install: bool = True) -> Path:
    """A new job in the repo at `repo_path`, copied from the example and listed as an entry point."""
    _check_job_name(name)
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists() or not (repo_path / "jobs").is_dir():
        msg = f"{repo_path} is not a job repo: it has no pyproject.toml and jobs/; make one with `etl new repo`"
        raise FileNotFoundError(msg)
    target = repo_path / "jobs" / name
    if target.exists():
        msg = f"{target} already exists"
        raise FileExistsError(msg)
    _copy_example(target, name)
    text = pyproject.read_text(encoding="utf-8")
    header = f'[project.entry-points."{GROUP}"]\n'
    if header not in text:
        msg = f"{pyproject} has no {header.strip()} table to list the job in"
        raise ValueError(msg)
    pyproject.write_text(text.replace(header, f'{header}{name} = "jobs.{name}.job"\n'), encoding="utf-8")
    if install:
        core_new.run(["uv", "sync", "--quiet"], repo_path)
    return target


def _check_job_name(name: str) -> None:
    if not IDENTIFIER.match(name) or name.startswith("_"):
        msg = f"{name}: a job's name is lower-case letters, digits and underscores, starting with a letter"
        raise ValueError(msg)


def _copy_example(target: Path, name: str) -> None:
    shutil.copytree(_EXAMPLE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ruff.toml"))
    for path in target.rglob("*"):
        if path.suffix in {".py", ".md"}:
            path.write_text(path.read_text(encoding="utf-8").replace("basis.job.example", f"jobs.{name}"), encoding="utf-8")
