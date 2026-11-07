"""Finding and loading models. A repo lists its models as entry points in the `optim.models` group, by name, each the
module path of its `model.py`, such as `models.income.model`, and a run names one; a module path names a model too.
Worker processes import a model by its module path."""

import importlib
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from optim.contract import Datasets, Metrics, Params

GROUP = "optim.models"
HOOKS = ("prepare_market", "prepare_portfolio", "reserve", "order", "check")
"""The hooks a model may define besides `decide`."""


@dataclass(frozen=True)
class LoadedModel:
    """A model's module, read: its classes and hooks. A hook it does not define is None."""

    name: str
    """As the run named it: an entry point's name, or the module path."""
    module: str
    directory: Path
    """The model's folder, where its `model.py` is: what paths in a call from Python are relative to, and which
    checkout's commit a run records."""
    params: type[Params]
    data: type[Datasets]
    decide: Callable[..., object]
    prepare_market: Callable[..., object] | None
    prepare_portfolio: Callable[..., object] | None
    reserve: Callable[..., object] | None
    order: Callable[..., Iterable[str]] | None
    check: Callable[..., Iterable[str]] | None
    metrics: type[Metrics] | None


def installed() -> dict[str, str]:
    """Every model the environment lists, by name: its module path."""
    return {point.name: point.value for point in entry_points(group=GROUP)}


def name_of(module: ModuleType) -> str:
    """The name the environment lists a model's module by, or else its module path."""
    return next((name for name, path in installed().items() if path == module.__name__), module.__name__)


def listed(repo: Path) -> dict[str, str]:
    """The models a repo's `pyproject.toml` lists, by name: their module paths."""
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(pyproject.get("project", {}).get("entry-points", {}).get(GROUP, {}))


def load(name: str) -> LoadedModel:
    """The model an entry point names, or the module at a module path."""
    models = installed()
    path = models.get(name, name)
    try:
        module = importlib.import_module(path)
    except ModuleNotFoundError as error:
        if not (error.name and f"{path}.".startswith(f"{error.name}.")):
            raise  # the model's own module imports something that is missing
        msg = f"no model {name!r}: it is not an entry point in {GROUP} ({', '.join(sorted(models)) or 'none installed'}), nor a module"
        raise LookupError(msg) from error
    return read(name, module)


def read(name: str, module: ModuleType) -> LoadedModel:
    """A model's classes and hooks. Its `Params` must subclass `optim.Params`, or a misspelled key would be ignored, and
    its `Data` `optim.Datasets`."""
    params, data, metrics = getattr(module, "Params", None), getattr(module, "Data", None), getattr(module, "Metrics", None)
    if not (isinstance(params, type) and issubclass(params, Params)):
        msg = f"model {name}: {module.__name__}.Params must subclass optim.Params, which refuses unknown keys and coercion"
        raise TypeError(msg)
    if not (isinstance(data, type) and issubclass(data, Datasets)):
        msg = f"model {name}: {module.__name__}.Data must subclass optim.Datasets"
        raise TypeError(msg)
    if not callable(decide := getattr(module, "decide", None)):
        msg = f"model {name}: {module.__name__} has no decide(portfolio, market, params) function"
        raise TypeError(msg)
    hooks = {hook: getattr(module, hook, None) for hook in HOOKS}
    file = getattr(module, "__file__", None)  # a namespace package, or a module built in memory, has none
    directory = Path(file).parent if file else Path.cwd()
    return LoadedModel(name, module.__name__, directory, params, data, decide, metrics=metrics if isinstance(metrics, type) and issubclass(metrics, Metrics) else None, **hooks)
