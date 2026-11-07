"""optim: one run loop for every portfolio model. A model is a module that declares its `Params` and `Data` and decides
each portfolio's trades in a pure `decide`; optim loads the data, runs the model in parallel, rounds and checks each
decision, and saves every run as a snapshot that replays.

`optim.run`, `optim.replay` and `optim.sweep` run a model from Python, as its `main.py` does (`optim.api`). They load
on first use, so that model code, which imports `optim` for its base classes, imports no source or driver."""

from importlib.metadata import version
from typing import TYPE_CHECKING

from optim.contract import Datasets, Decision, Metrics, Params, SolverStats
from optim.data.dataset import Dataset
from optim.schema import Row

if TYPE_CHECKING:
    from optim.api import replay, run, sweep

__version__ = version("optim")
__all__ = ["Dataset", "Datasets", "Decision", "Metrics", "Params", "Row", "SolverStats", "__version__", "replay", "run", "sweep"]


def __getattr__(name: str) -> object:
    if name in ("run", "replay", "sweep"):
        from optim import api  # noqa: PLC0415 -- on first use alone: see the module's docstring

        return getattr(api, name)
    msg = f"module 'optim' has no attribute {name!r}"
    raise AttributeError(msg)
