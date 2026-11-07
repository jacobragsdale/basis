"""`optim new repo` and `basis new model`: a model repo, or a model in one, from basis's shared templates, this
package's own and its example, so a new repo matches the optim version that made it. `repo` writes the repo, with its
first model in `models/MODEL/`, runs `git init` and `uv sync`, and leaves the first commit to the quant; `model` adds a
model to a repo, with its entry point and its row in the README, and `models/shared/` with the repo's second model. A
repo's `pyproject.toml` declares the part, as `[tool.basis] parts = ["optim"]`, and says where it takes optim from,
as `basis.core.new.sources` writes it: an index, a checkout of basis, or the commit or tag optim came from, never
PyPI, which has an unrelated package of that name."""

import re
import tomllib
from pathlib import Path

import optim
from basis.core import new as core_new
from basis.core.new import NewError
from optim import sync
from optim.models import GROUP

PACKAGE = Path(__file__).parent
EXAMPLE = PACKAGE / "example"
TEMPLATES = PACKAGE / "templates"
FOLDER = "packages/optim"
"""optim's folder in a checkout of basis."""
_SKIP = {"__pycache__", "ruff.toml"}  # the example's ruff.toml sorts its imports as a model repo's pyproject.toml does


def repo(path: Path, model: str, *, index: str | None, framework_path: Path | None, install: bool = True) -> Path:
    """A new repo at `path` with one model, `model`, copied from the example into `models/MODEL/`, its dependencies
    installed and git initialised; the first commit is the quant's. optim, and basis-core through it, come from
    `index`, from a checkout of basis at `framework_path` installed editable, or, with neither, from the commit optim
    was installed from, or else basis's GitHub repository at this version's tag, which needs a release.
    `install=False` leaves out `git init` and `uv sync`."""
    module = _identifier(model)
    _, minor, *_ = optim.__version__.split(".")
    values = {"repo": path.name, "model": model, "module": module, "version": optim.__version__, "next_minor": str(int(minor) + 1)}
    core_new.repo(path, templates=TEMPLATES / "repo", values=values, distributions={"optim": FOLDER}, version=optim.__version__, index=index, checkout=framework_path)
    _write(path / "models" / "__init__.py", "")
    _copy_model(path, model)
    sync.write(path)
    if install:
        core_new.install(path)
    return path


def model(repo_path: Path, name: str, *, install: bool = True) -> Path:
    """A new model in the repo at `repo_path`, copied from the example and listed as an entry point and in the README;
    with the repo's second model, `models/shared/` too. `install=False` leaves out `uv sync`, which lists its entry
    point in the installed metadata."""
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists() or not (repo_path / "models").is_dir():
        msg = f"{repo_path} is not a model repo: it has no pyproject.toml and models/; make one with `uvx optim new repo NAME MODEL`"
        raise NewError(msg)
    others = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"].get("entry-points", {}).get(GROUP, {})
    if name in others:
        msg = f"{repo_path} already has a model named {name}: give the new model another name"
        raise NewError(msg)
    directory = _copy_model(repo_path, name)
    text = pyproject.read_text(encoding="utf-8")
    header = f'[project.entry-points."{GROUP}"]\n'
    line = f'{name} = "models.{_identifier(name)}.model"\n'
    pyproject.write_text(text.replace(header, header + line) if header in text else f"{text.rstrip()}\n\n{header}{line}", encoding="utf-8", newline="\n")
    _list_in_readme(repo_path / "README.md", name, f"models/{_identifier(name)}/")
    if others and not (repo_path / "models" / "shared").exists():
        core_new.write_templates(repo_path / "models" / "shared", TEMPLATES / "shared", {})
    if install:  # the entry point is installed metadata: uv reinstalls the project to list it
        core_new.run(["uv", "sync", "--quiet"], repo_path)
    return directory


def _list_in_readme(readme: Path, model: str, directory: str) -> None:
    """Add the model to the README's table of models, after its last row, where the README still has the table."""
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True) if readme.exists() else []
    if (header := next((i for i, line in enumerate(lines) if line.startswith("| Model | Folder |")), None)) is None:
        return
    last = header
    while last + 1 < len(lines) and lines[last + 1].startswith("|"):
        last += 1
    lines.insert(last + 1, f"| `{model}` | `{directory}` |{' |' * (lines[header].count('|') - 3)}\n")
    lines[last] = lines[last].rstrip("\n") + "\n"
    readme.write_text("".join(lines), encoding="utf-8", newline="\n")


def _copy_model(repo_path: Path, model: str) -> Path:
    """The example into `models/MODEL/`, with its imports pointed at the new module."""
    module = _identifier(model)
    directory = repo_path / "models" / module
    if directory.exists():
        msg = f"{directory} already exists: give the new model another name"
        raise NewError(msg)
    for file in sorted(EXAMPLE.rglob("*")):
        if file.is_file() and not _SKIP & set(file.relative_to(EXAMPLE).parts):
            _write(directory / file.relative_to(EXAMPLE), re.sub(r"\boptim\.example\b", f"models.{module}", file.read_text(encoding="utf-8")))
    return directory


def _identifier(name: str) -> str:
    identifier = re.sub(r"\W", "_", name.replace("-", "_")).lower()
    if not identifier.isidentifier() or identifier[0].isdigit():
        msg = f"{name!r} cannot name a Python package or module: start with a letter, and use letters, digits, - and _"
        raise NewError(msg)
    return identifier


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
