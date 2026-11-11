"""The tasks, each a folder of `evals/tasks/`: `task.toml`, which says what repo it starts from and how it is graded,
`instruction.md`, what the agent is asked, as a developer would ask it, `setup/`, the files laid over the generated
repo before the agent starts (a seeded bug, a legacy script), `solution/`, the reference's files, laid over it to prove
the task can pass, and `hidden/`, the tests the grader adds once the agent is done."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent / "tasks"
PROTECTED = (".githooks", ".claude/settings.json", ".agents", "ruff.toml", "ty.toml", "pytest.ini", "tests/test_agent_files.py")
"""What no task's agent may change: the hooks, the agent's own settings and skills, and the tools' settings, which
basis writes. `basis check` refuses most changes to them anyway; this names the change, whatever the check says."""


class Spec(BaseModel):
    """A task's `task.toml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: Literal["job", "api"]
    """What it starts from: `basis-job new repo pricing-etl`, whose job is `positions`, or `basis-api new app quotes`."""
    summary: str
    adversarial: bool = False
    """Whether the dishonest way to make the checks pass is the easy one, so that any suppression or exception the change
    adds fails the trial, as a skip or a removed test fails every trial."""
    protected: list[str] = Field(default_factory=list)
    """Paths beyond `PROTECTED` that must be as the task began, such as the tests a fix must leave alone."""
    hidden_to: str
    """The folder, in the repo, the hidden tests are copied into to run."""
    setup: list[list[str]] = Field(default_factory=list)
    """Commands run in the repo before `setup/` is laid over it, each an argv."""
    solve: list[list[str]] = Field(default_factory=list)
    """Commands the reference runs before `solution/` is laid over the repo, each an argv."""
    after: list[list[str]] = Field(default_factory=list)
    """Commands the reference runs after, such as `uv run basis sync` for a file basis makes from the code."""


class Task(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    folder: Path
    spec: Spec
    instruction: str

    @property
    def protected(self) -> tuple[str, ...]:
        return (*PROTECTED, *self.spec.protected)

    def files(self, part: Literal["setup", "solution", "hidden"]) -> list[Path]:
        """The files of one of its folders, relative to it, in a stable order."""
        base = self.folder / part
        return sorted(path.relative_to(base) for path in base.rglob("*") if path.is_file() and "__pycache__" not in path.parts) if base.is_dir() else []


class TaskError(Exception):
    pass


def load(folder: Path) -> Task:
    """The task in `folder`; TaskError, naming the file, if it is not one."""
    toml, instruction = folder / "task.toml", folder / "instruction.md"
    if not toml.is_file() or not instruction.is_file():
        msg = f"{folder} is no task: it needs task.toml and instruction.md"
        raise TaskError(msg)
    try:
        spec = Spec.model_validate(tomllib.loads(toml.read_text(encoding="utf-8")))
    except (tomllib.TOMLDecodeError, ValueError) as error:
        msg = f"{toml} is not a task's settings: {error}"
        raise TaskError(msg) from error
    task = Task(name=folder.name, folder=folder, spec=spec, instruction=instruction.read_text(encoding="utf-8").strip())
    if not task.files("hidden"):
        msg = f"{folder} has no hidden tests: add hidden/test_*.py"
        raise TaskError(msg)
    return task


def every(root: Path = ROOT, names: list[str] | None = None) -> list[Task]:
    """The tasks under `root`, or those `names` names; TaskError for a name with no task."""
    found = {folder.name: folder for folder in sorted(root.iterdir()) if folder.is_dir()}
    if names is not None and (unknown := sorted(set(names) - set(found))):
        msg = f"no tasks named {unknown}: the tasks are {sorted(found)}"
        raise TaskError(msg)
    return [load(found[name]) for name in (names or sorted(found))]
