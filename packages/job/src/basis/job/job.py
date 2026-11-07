"""Finding a job and reading its contract. A job is a module, its `job.py`, that exports `Params`, `Inputs`, `Outputs`
and `transform(inputs, params) -> Outputs`, and may export `check(inputs, outputs, params) -> list[str]`, its own
reasons not to write. A repo lists each job as an entry point in the `basis.jobs` group, whose name is the
job's name in snapshots, logs and the ledger; a module that is not listed is named by its module path."""

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from basis.core.errors import BasisError
from basis.job.inputs import Inputs
from basis.job.outputs import Outputs
from basis.job.params import Params

GROUP = "basis.jobs"


class JobError(BasisError, TypeError):
    """A job module that does not export the contract."""


@dataclass(frozen=True)
class Job:
    name: str
    module: str
    params: type[Params]
    inputs: type[Inputs]
    outputs: type[Outputs]
    transform: Callable[..., object]
    check: Callable[..., object] | None
    directory: Path
    """The folder of the job's module, which its params and fixture paths are relative to."""

    def run_transform(self, inputs: Inputs, params: Params) -> Outputs:
        outputs = self.transform(inputs, params)
        if not isinstance(outputs, self.outputs):
            msg = f"{self.name}'s transform returned a {type(outputs).__name__}, not its Outputs: return Outputs(name=frame, ...)"
            raise JobError(msg)
        return outputs

    def run_check(self, inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
        if self.check is None:
            return []
        reasons = self.check(inputs, outputs, params)
        if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
            msg = f"{self.name}'s check returned a {type(reasons).__name__}, not a list of reasons"
            raise JobError(msg)
        return [str(reason) for reason in reasons]


def repo_root(directory: Path) -> Path:
    """The nearest folder at or above `directory` that holds a `pyproject.toml`: the job's repo."""
    for folder in (directory, *directory.parents):
        if (folder / "pyproject.toml").exists():
            return folder
    return directory


def listed() -> dict[str, str]:
    """Every job the installed packages list, by name: its module path."""
    return {entry.name: entry.value for entry in entry_points(group=GROUP)}


def load(reference: str) -> Job:
    """A job by its entry point name, or by its module path, as `jobs.positions.job`."""
    jobs = listed()
    module = jobs.get(reference, reference)
    try:
        imported = importlib.import_module(module)
    except ModuleNotFoundError as error:
        if error.name is not None and module.startswith(error.name):
            msg = f"no job named {reference!r}, and it is no module path: give one the repo lists ({', '.join(sorted(jobs)) or 'none yet'}), or a job's module path, as jobs.NAME.job"
            raise JobError(msg) from error
        raise
    return read(imported)


def read(module: ModuleType) -> Job:
    """The contract a job module exports."""
    names = {value: name for name, value in listed().items()}
    where = module.__name__

    def exported[T](attribute: str, base: type[T]) -> type[T]:
        value = getattr(module, attribute, None)
        if not (isinstance(value, type) and issubclass(value, base)):
            msg = f"{where} exports no {attribute}, a subclass of etl.{base.__name__}"
            raise JobError(msg)
        return value

    transform = getattr(module, "transform", None)
    if not callable(transform) or len(inspect.signature(transform).parameters) != 2:  # noqa: PLR2004 -- the contract's arity
        msg = f"{where} exports no transform(inputs, params) -> Outputs"
        raise JobError(msg)
    check = getattr(module, "check", None)
    if check is not None and (not callable(check) or len(inspect.signature(check).parameters) != 3):  # noqa: PLR2004 -- the contract's arity
        msg = f"{where}'s check is not check(inputs, outputs, params) -> list[str]"
        raise JobError(msg)
    if module.__file__ is None:
        msg = f"{where} is not a file"
        raise JobError(msg)
    return Job(
        name=names.get(where, where),
        module=where,
        params=exported("Params", Params),
        inputs=exported("Inputs", Inputs),
        outputs=exported("Outputs", Outputs),
        transform=transform,
        check=check,
        directory=Path(module.__file__).resolve().parent,
    )
