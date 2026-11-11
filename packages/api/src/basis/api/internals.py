"""The parts of FastAPI's internals basis-api builds on, in one place, each held by `tests/test_internals.py` to what
basis takes of it, so a FastAPI upgrade that moves one fails there, by name:

- `Dependant` and `get_dependant`, FastAPI's reading of a handler's parameters: `strict.shape` reads its path, query
  and body parameters, and its sub-dependencies, when a route is declared;
- `ModelField`, a parameter's field, whose `validate` is where FastAPI's handler validates a body: the rule that a
  body is validated once rests on it (`ValidatedOnce`), and with it the rule that a write commits after the response
  validates, since basis reads the body before FastAPI's handler runs;
- a route's `Dependant.body_params` and `call`, which `routes.Served` replaces when it builds the route's handler.

Nothing else in basis-api imports from `fastapi._compat`, `fastapi.dependencies` or a dependant's fields."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from fastapi._compat import ModelField
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant

__all__ = ["Dependant", "ModelField", "Validated", "ValidatedOnce", "get_dependant", "replace_call", "validate_body_once"]


@dataclass(frozen=True)
class Validated:
    """A body basis has read and validated, as FastAPI's handler gets it for its JSON."""

    value: object


class ValidatedOnce(ModelField):
    """A route's body field, as FastAPI's handler validates it: the value basis validated passes as it is, so no
    validator of the body's runs twice. The OpenAPI document reads the field as FastAPI made it."""

    @override
    def validate(self, value: object, values: dict[str, object] | None = None, *, loc: tuple[int | str, ...] = ()) -> tuple[object, list[dict[str, object]]]:
        if isinstance(value, Validated):
            return value.value, []
        validated, wrong = super().validate(value, values if values is not None else {}, loc=loc)
        found: object = validated
        errors: list[dict[str, object]] = [dict(entry) for entry in wrong]
        return found, errors


def validate_body_once(dependant: Dependant) -> None:
    """Make the handler's body field, its first, a `ValidatedOnce`, once however often its route's handler is built."""
    if dependant.body_params and not isinstance(field := dependant.body_params[0], ValidatedOnce):  # the handler's own, as strict.shape sees to
        dependant.body_params[0] = ValidatedOnce(field_info=field.field_info, name=field.name, mode=field.mode, config=field.config)


def replace_call(dependant: Dependant, handler: Callable[..., object], call: Callable[..., object]) -> None:
    """Have FastAPI call `call` where it would call `handler`, the route's own; a dependant that already calls
    something else is left as it is."""
    if dependant.call is handler:
        dependant.call = call
