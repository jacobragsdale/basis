"""`etl new repo` and `etl new job`: a job repo, or a job in one, from the package's example and templates, so a new
repo matches the etl-framework version that made it. A repo's package is `jobs`, each job a folder in it."""

import re
import shutil
import subprocess
from pathlib import Path

from etl_framework import sync
from etl_framework.rows import IDENTIFIER
from etl_framework.version import VERSION

_PACKAGE = Path(__file__).parent
_EXAMPLE = _PACKAGE / "example"
_TEMPLATES = _PACKAGE / "templates" / "repo"
_DOTTED = {"gitignore", "dockerignore", "env.example", "python-version", "pre-commit-config.yaml"}
_REPO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_GIT = "https://github.com/jacobragsdale/etl-framework"


def repo(path: Path, job_name: str, *, index: str | None, framework_path: Path | None, install: bool = True) -> Path:
    """A new repo at `path` with one job, its dependencies installed and git initialised; the first commit is the
    author's. etl-framework comes from `index`, from a checkout at `framework_path` installed editable, or, with
    neither, from its GitHub repository at this version's tag. `install=False` leaves out `git init` and `uv sync`."""
    _check_job_name(job_name)
    if path.exists():
        msg = f"{path} already exists: give a new folder"
        raise FileExistsError(msg)
    if not _REPO.match(path.name):
        msg = f"{path.name}: a repo's name is lower-case letters, digits, '.', '_' and '-'"
        raise ValueError(msg)
    if index is not None and framework_path is not None:
        msg = "give --index or --framework-path, not both"
        raise ValueError(msg)
    path.mkdir(parents=True)
    for template in sorted(_TEMPLATES.rglob("*")):
        if template.is_dir() or "__pycache__" in template.parts:
            continue
        relative = template.relative_to(_TEMPLATES)
        name = f".{relative.name}" if relative.name in _DOTTED else relative.name.removesuffix(".in")
        target = path / relative.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(template.read_text(encoding="utf-8"), path.name, job_name, _source(index, framework_path)), encoding="utf-8")
    (path / "jobs").mkdir()
    (path / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    _copy_example(path / "jobs" / job_name, job_name)
    sync.write(path)
    if install:
        _run(["git", "init", "--quiet", "--initial-branch=main"], path)
        _run(["uv", "sync", "--quiet"], path)
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
    header = '[project.entry-points."etl_framework.jobs"]\n'
    if header not in text:
        msg = f"{pyproject} has no {header.strip()} table to list the job in"
        raise ValueError(msg)
    pyproject.write_text(text.replace(header, f'{header}{name} = "jobs.{name}.job"\n'), encoding="utf-8")
    if install:
        _run(["uv", "sync", "--quiet"], repo_path)
    return target


def _check_job_name(name: str) -> None:
    if not IDENTIFIER.match(name) or name.startswith("_"):
        msg = f"{name}: a job's name is lower-case letters, digits and underscores, starting with a letter"
        raise ValueError(msg)


def _copy_example(target: Path, name: str) -> None:
    shutil.copytree(_EXAMPLE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ruff.toml"))
    for path in target.rglob("*"):
        if path.suffix in {".py", ".md"}:
            path.write_text(path.read_text(encoding="utf-8").replace("etl_framework.example", f"jobs.{name}"), encoding="utf-8")


def _source(index: str | None, framework_path: Path | None) -> str:
    if index is not None:
        return f'[[tool.uv.index]]\nname = "internal"\nurl = "{index}"\nexplicit = true\n\n[tool.uv.sources]\netl-framework = {{ index = "internal" }}\n'
    if framework_path is not None:
        return f'[tool.uv.sources]\netl-framework = {{ path = "{framework_path.resolve().as_posix()}", editable = true }}\n'
    return f'[tool.uv.sources]\netl-framework = {{ git = "{_GIT}", tag = "v{VERSION}" }}\n'


def _render(text: str, repo_name: str, job_name: str, source: str) -> str:
    major, minor, *_ = VERSION.split(".")
    del major
    return text.replace("{repo}", repo_name).replace("{job}", job_name).replace("{version}", VERSION).replace("{next_minor}", str(int(minor) + 1)).replace("{source}", source)


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)
