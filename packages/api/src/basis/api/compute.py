"""Pure compute that a route runs in a process pool, off the event loop: CPU-bound code, such as a solver, which would
otherwise stall every request its worker serves while it runs. A service declares each function with `api.compute`,
and a handler asks for it with `api.given`, as for a resource, annotated with the function's parameters and result:

    # accruals/compute.py: plain functions, which the import contract keeps free of I/O
    def yield_to_maturity(cash_flows: tuple[CashFlow, ...], dirty: Decimal) -> Decimal: ...

    # accruals/service.py
    SOLVE = api.compute(compute.yield_to_maturity)

    async def bond_yield(..., solve: Annotated[api.Compute[[tuple[CashFlow, ...], Decimal], Decimal], api.given(SOLVE)]) -> Yield:
        rate = await solve(cash_flows, dirty)

The rules, each checked where it can be:

- A compute function lives in a module named `compute`, or in a package of that name, which the repo's import
  contract keeps free of I/O: no resource, database driver, HTTP client, settings or basis-api itself.
- It is a plain `def` at its module's top level, since it crosses to the pool by its name, and so it never reaches a
  resource: it is given values, and returns one.
- Its arguments and its result cross to and from the pool's processes by pickling, so each is something pickle
  carries, such as numbers, dates, decimals, frozen models and tuples of them.
- A call has what is left of its route's deadline, its wait for a free process included, and one that misses it
  answers 504, as a resource's call does. A function that has started runs on in its process until it returns, and
  its result is dropped.
- The pool's processes start with the `spawn` method on every platform, as Windows has no other, and each imports the
  service's compute modules once, before the app is ready. How many there are is the setting
  `BASIS_COMPUTE__WORKERS`, which a service with compute must give; a test may fake a function with
  `fakes.Compute`, which runs it in this process."""

import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, ParamSpec, Protocol, TypeVar, final

from basis.api.errors import ServiceError

MODULE = "compute"
"""The name of a compute function's module, or of a package it is in."""


P = ParamSpec("P")
R_co = TypeVar("R_co", covariant=True)


@final
@dataclass(frozen=True)
class ComputeFunction(Generic[P, R_co]):  # noqa: UP046 -- covariant in its result, so any is a ComputeFunction[..., object]; ty infers a frozen dataclass's parameter invariant
    function: Callable[P, R_co]
    name: str = field(init=False)
    """The function's name, which its spans carry."""
    parameters: tuple[object, ...] = field(init=False)
    """Each parameter's type, in order: what the handler's `api.Compute[[...], R]` lists."""
    result: object = field(init=False)

    def __post_init__(self) -> None:
        function: object = self.function
        if not inspect.isfunction(function) or function.__name__ == "<lambda>":
            msg = f"api.compute({function!r}): a compute function is a named function, declared with def at the top level of its module"
            raise ServiceError(msg)
        name, module = function.__name__, function.__module__
        if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            msg = f"compute function {name} is not a plain def: a compute function computes and returns, so declare it with def, without await or yield"
            raise ServiceError(msg)
        if function.__qualname__ != name or getattr(sys.modules.get(module), name, None) is not function:
            msg = f"compute function {function.__qualname__} is not at the top level of {module}: move it there, since it crosses to the pool's processes by its name"
            raise ServiceError(msg)
        if MODULE not in module.split("."):
            msg = f"compute function {name} is in {module}: move it to a module named {MODULE}, or into a package of that name, which the import contract keeps free of I/O"
            raise ServiceError(msg)
        signature = inspect.signature(function, eval_str=True)
        kinds = {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        for parameter in signature.parameters.values():
            if parameter.kind not in kinds:
                msg = f"compute function {name}'s parameter {parameter.name} is {parameter.kind.description}: give it only parameters a call can pass in order"
                raise ServiceError(msg)
            if parameter.annotation is inspect.Parameter.empty:
                msg = f"compute function {name}'s parameter {parameter.name} has no type: annotate it, as the handler's api.Compute[[...], ...] lists it"
                raise ServiceError(msg)
        if signature.return_annotation is inspect.Signature.empty:
            msg = f"compute function {name} has no return type: annotate it, as the handler's api.Compute[[...], ...] gives it"
            raise ServiceError(msg)
        object.__setattr__(self, "name", name)  # frozen: set once, from the function
        object.__setattr__(self, "parameters", tuple(parameter.annotation for parameter in signature.parameters.values()))
        object.__setattr__(self, "result", signature.return_annotation)


def compute[**P, R](function: Callable[P, R]) -> ComputeFunction[P, R]:
    """`function`, declared as pure compute that a route runs in the service's process pool."""
    return ComputeFunction(function)


class Compute[**P, R](Protocol):
    """A compute function, as a handler that asks for it sees it: awaiting a call runs the function in the pool, with
    what is left of the route's deadline, and gives its result."""

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...
