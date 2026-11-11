"""FastAPI's internals that basis-api builds on, held to what `basis.api.internals` takes of them: a FastAPI upgrade that
moves or reshapes one fails here, by name, before it fails a route's requests."""

import dataclasses
import inspect
from typing import Annotated

from fastapi import Body
from pydantic import BaseModel, field_validator

from basis.api import internals


class Order(BaseModel):
    quantity: int

    @field_validator("quantity")
    @classmethod
    def counted(cls, value: int) -> int:
        CALLS.append(value)
        return value


CALLS: list[int] = []


def handler(order: Annotated[Order, Body()], account: str) -> None:
    del order, account


def test_a_dependant_has_the_parameters_basis_reads_and_the_call_it_replaces() -> None:
    names = {field.name for field in dataclasses.fields(internals.Dependant)}
    assert {"path_params", "query_params", "body_params", "dependencies", "call", "background_tasks_param_name"} <= names


def test_get_dependant_takes_a_path_and_a_call_and_finds_the_body_and_the_query() -> None:
    dependant = internals.get_dependant(path="/orders", call=handler)
    assert [field.name for field in dependant.body_params] == ["order"]
    assert [field.name for field in dependant.query_params] == ["account"]


def test_a_model_field_is_made_from_its_parts_and_validates_with_its_values_and_loc() -> None:
    assert {"field_info", "name", "mode", "config"} <= set(inspect.signature(internals.ModelField).parameters)
    assert list(inspect.signature(internals.ModelField.validate).parameters) == ["self", "value", "values", "loc"]


def test_a_body_validated_once_passes_basis_s_value_as_it_is_and_validates_anything_else() -> None:
    dependant = internals.get_dependant(path="/orders", call=handler)
    internals.validate_body_once(dependant)
    [field] = dependant.body_params
    assert isinstance(field, internals.ValidatedOnce)
    CALLS.clear()
    made = Order(quantity=3)
    assert CALLS == [3]
    assert field.validate(internals.Validated(made), {}, loc=("body",)) == (made, [])
    assert CALLS == [3]  # never validated again
    validated, errors = field.validate({"quantity": 4}, {}, loc=("body",))
    assert CALLS == [3, 4]  # anything else, FastAPI's own validation
    assert (validated, errors) == (Order(quantity=4), [])
    internals.validate_body_once(dependant)
    assert dependant.body_params == [field]  # made once, however often a route's handler is built
