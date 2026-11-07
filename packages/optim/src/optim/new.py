"""`optim new`: a model repo, or a model in one, from the installed package's example and templates, so a generated repo
matches the `optim` version that made it. `new_repo` writes the repo, with its first model in `models/MODEL/`, runs
`git init` and `uv sync`, and leaves the first commit to the quant; `new_model` adds a model to the repo it runs in,
with its entry point and its row in the README, and `models/shared/` with the repo's second model. A repo always says
where it takes `optim` from, the firm's index or a checkout, because PyPI has an unrelated package of that name."""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from optim import sync

PACKAGE = Path(__file__).parent
EXAMPLE = PACKAGE / "example"
TEMPLATES = PACKAGE / "templates"
OPTIM = "optim>=0.2,<0.3"  # 0.x: a minor release may break the contract, so a repo takes patches alone until it re-locks
_SKIP = {"__pycache__", "ruff.toml"}  # the example's ruff.toml sorts its imports as a model repo's pyproject.toml does
# Stored under other names, so they act on no repo but the one they are copied into: without their dot, and
# pyproject.toml, which ruff would read as this package's config, as .in.
_STORED = {"gitignore": ".gitignore", "dockerignore": ".dockerignore", "env.example": ".env.example", "python-version": ".python-version", "pyproject.toml.in": "pyproject.toml"}


def new_repo(name: str, model: str, *, directory: Path, index: str | None = None, optim_path: Path | None = None, uv_sync: bool = True) -> Path:
    """A model repo `directory/name`, with one model, `model`, copied from the example into `models/MODEL/`. It takes
    `optim` from `index`, the internal index it is published to, or from `optim_path`, a checkout of it, editable."""
    if (index is None) == (optim_path is None):
        msg = "say where the repo takes optim from, with one of --index, the internal index it is published to, or --optim-path, a checkout of it"
        msg += ": PyPI's optim is an unrelated package, which a repo without either would install"
        raise ValueError(msg)
    repo = directory / name
    if repo.exists() and any(repo.iterdir()):
        msg = f"{repo} exists and is not empty"
        raise FileExistsError(msg)
    names = {"repo": name, "model": model, "module": _identifier(model), "optim": OPTIM, "source": _source(index, optim_path)}
    _copy(TEMPLATES / "repo", repo, names)
    _write(repo / "models" / "__init__.py", "")
    _copy_model(repo, model)
    sync.sync(repo)
    _run(["git", "init", "--quiet", "--initial-branch=main"], repo)
    if uv_sync:
        _run(["uv", "sync", "--quiet"], repo)
    return repo


def new_model(name: str, *, repo: Path, uv_sync: bool = True) -> Path:
    """Add model `name`, copied from the example, to the model repo at `repo`, with its entry point and its row in the
    README; with the repo's second model, add `models/shared/` too."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.exists():
        msg = f"{repo} has no pyproject.toml: run optim new model in a model repo, or optim new repo to make one"
        raise FileNotFoundError(msg)
    others = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"].get("entry-points", {}).get("optim.models", {})
    if name in others:
        msg = f"{repo} already has a model named {name}"
        raise FileExistsError(msg)
    directory = _copy_model(repo, name)
    text = pyproject.read_text(encoding="utf-8")
    header = '[project.entry-points."optim.models"]\n'
    line = f'{name} = "models.{_identifier(name)}.model"\n'
    pyproject.write_text(text.replace(header, header + line) if header in text else f"{text.rstrip()}\n\n{header}{line}", encoding="utf-8", newline="\n")
    _list_in_readme(repo / "README.md", name, f"models/{_identifier(name)}/")
    if others and not (repo / "models" / "shared").exists():
        _copy(TEMPLATES / "shared", repo / "models" / "shared", {})
    if uv_sync:  # the entry point is installed metadata: uv reinstalls the project to list it
        _run(["uv", "sync", "--quiet"], repo)
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


def _copy_model(repo: Path, model: str) -> Path:
    """The example into `models/MODEL/`, with its imports pointed at the new module."""
    directory = repo / "models" / _identifier(model)
    if directory.exists():
        msg = f"{directory} exists"
        raise FileExistsError(msg)
    for file in sorted(EXAMPLE.rglob("*")):
        if file.is_file() and not _SKIP & set(file.relative_to(EXAMPLE).parts):
            _write(directory / file.relative_to(EXAMPLE), _fill(file.read_text(encoding="utf-8"), {}, {"optim.example": f"models.{_identifier(model)}"}))
    return directory


def _copy(templates: Path, directory: Path, names: dict[str, str]) -> None:
    for template in sorted(templates.rglob("*")):
        if template.is_file() and "__pycache__" not in template.parts:
            relative = template.relative_to(templates)
            _write(directory / relative.parent / _STORED.get(relative.name, relative.name), _fill(template.read_text(encoding="utf-8"), names))


def _fill(text: str, names: dict[str, str], rename: dict[str, str] | None = None) -> str:
    for old, new in (rename or {}).items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return re.sub(r"\{\{(\w+)\}\}", lambda found: names.get(found.group(1), found.group(0)), text)


def _source(index: str | None, checkout: Path | None) -> str:
    """Where the repo takes `optim` from, as its pyproject says it: the internal index, or a checkout, editable."""
    if checkout is not None:
        return f'\n[tool.uv.sources]\noptim = {{ path = "{checkout.resolve().as_posix()}", editable = true }}\n'
    return f'\n[[tool.uv.index]]\nname = "internal"\nurl = "{index}"\n\n[tool.uv.sources]\noptim = {{ index = "internal" }}\n'


def _identifier(name: str) -> str:
    identifier = re.sub(r"\W", "_", name.replace("-", "_")).lower()
    if not identifier.isidentifier() or identifier[0].isdigit():
        msg = f"{name!r} cannot name a Python package or module: start with a letter, and use letters, digits, - and _"
        raise ValueError(msg)
    return identifier


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _run(command: list[str], cwd: Path) -> None:
    if shutil.which(command[0]) is None:
        msg = f"{command[0]} is not on PATH: run `{' '.join(command)}` in {cwd} once it is"
        raise FileNotFoundError(msg)
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603 -- fixed arguments, from PATH, as the quant runs them
