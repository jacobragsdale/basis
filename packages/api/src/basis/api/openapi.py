"""A service's OpenAPI document: what its app serves at `/openapi.json`, and what an API repo commits as its own
`openapi.json`, which `basis sync` writes and `basis check` compares, and which a UI's client is generated from.

FastAPI makes the document from the routes, and basis finishes it, so that it says what the app does:

- each operation's id is its handler's name, so a generated client reads `getPositions`, not
  `read_positions_positions_get`;
- every error is `application/problem+json`, the media type the app answers errors with: a 422 a `ValidationProblem`,
  whose `errors` name the fields at fault, and any other a `Problem`;
- every model is strict, `additionalProperties: false`: a request's unknown key is refused, and a response holds
  only the fields its model declares;
- a decimal a request gives is a string of its digits, never a number, as basis reads one, with the digits and the
  places its type declares in its `pattern`, and its bounds in `x-basis-decimal` (`decimals.py`); a response gives a
  decimal as a string too, and a time in either always carries its offset, which JSON Schema's `date-time` format
  requires;
- a dataset route's success lists `application/vnd.apache.arrow.stream` beside its JSON, as a binary string, the one
  form of a binary body that generated clients read as one.

The paths carry no `/api`: the deployment that serves a UI and its API on one host routes `/api` to the API and
removes it, and the UI's client adds it.

The committed file is the document with basis's marker as its first key, `x-basis`, and two spaces of indent, as
`text` writes it."""

import json
import re
from collections.abc import Iterator, Sequence
from http import HTTPStatus
from typing import override

from fastapi import FastAPI
from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic.fields import FieldInfo

from basis.api import decimals, routes, strict, sync
from basis.api.errors import MEDIA_TYPE
from basis.api.service import Route, Service
from basis.core.sync import MARKER

FILE = sync.DOCUMENT
"""Where an API repo commits its document: at its root."""
_REF = re.compile(r"#/components/schemas/(?P<name>.+)")
_DOCUMENT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class Application(FastAPI):
    """FastAPI, whose OpenAPI document is basis's, as `finish` makes it from FastAPI's."""

    _document: dict[str, JsonValue] | None = None

    @override
    def openapi(self) -> dict[str, JsonValue]:
        if self._document is None:
            declared = [route.declared for route in self.routes if isinstance(route, routes.Served)]
            self._document = self.openapi_schema = finish(_DOCUMENT.validate_python(super().openapi()), declared)
        return self._document


def document(service: Service) -> dict[str, JsonValue]:
    """`service`'s document, as its app serves it, made without its settings or its resources."""
    app = Application(title=service.name)
    routes.add(app, service)
    return app.openapi()


def text(service: Service) -> str:
    """`service`'s document as its repo commits it, with basis's marker first."""
    return json.dumps({"x-basis": MARKER, **document(service)}, indent=2, ensure_ascii=False) + "\n"


def finish(found: dict[str, JsonValue], declared: Sequence[Route] = ()) -> dict[str, JsonValue]:
    """FastAPI's document `found`, of the routes `declared`, as basis gives it: in place, and returned."""
    schemas = mapping(mapping(found.get("components")).get("schemas"))
    by_name = {route.name: route for route in declared}
    asked: list[JsonValue] = []
    answered: list[JsonValue] = []
    for _, _, operation in operations(found):
        reads = route.reads if (route := by_name.get(str(operation.get("operationId")))) is not None else None
        for parameter in array(operation.get("parameters")):
            given = mapping(parameter)
            asked.append(schema := given.get("schema"))
            info = _parameter(reads, str(given.get("in")), str(given.get("name")))
            _decimals_as_strings(schema, iter(decimals.of_field(info, info.annotation) if info is not None else []))
        for media in mapping(mapping(operation.get("requestBody")).get("content")).values():
            asked.append(schema := mapping(media).get("schema"))
            info = reads.body.info if reads is not None and reads.body is not None else None
            _decimals_as_strings(schema, iter(decimals.of_field(info, info.annotation) if info is not None else []))
        for status, response in mapping(operation.get("responses")).items():
            content = mapping(mapping(response).get("content"))
            if (status == "default" or (status.isdigit() and int(status) >= HTTPStatus.BAD_REQUEST)) and "application/json" in content:
                content[MEDIA_TYPE] = content.pop("application/json")
            answered.extend(mapping(media).get("schema") for media in content.values())
    request_only = _reached(asked, schemas) - _reached(answered, schemas)  # a model in both has no decimal, which differs in each
    models = decimals.models([found.annotation for route in declared for found in _infos(route)])
    for name in sorted(request_only):
        component = mapping(schemas.get(name))
        model = _model(name, component, models)
        held = decimals.fields(model) if model is not None else {}
        for key, inner in mapping(component.get("properties")).items():
            _decimals_as_strings(inner, iter(held.get(key, [])))
        for key, inner in component.items():
            if key != "properties":
                _decimals_as_strings(inner, iter([]))
    _strict(found)
    return found


def _parameter(reads: strict.Shape | None, where: str, name: str) -> FieldInfo | None:
    """What the route declares of its parameter `name`, in its path or its query."""
    listed = (reads.path if where == "path" else reads.query if where == "query" else ()) if reads is not None else ()
    return next((parameter.info for parameter in listed if parameter.key == name), None)


def _infos(route: Route) -> Iterator[FieldInfo]:
    """What `route` reads, each a parameter's or its body's type and what it declares."""
    yield from (parameter.info for parameter in (*route.reads.path, *route.reads.query))
    if route.reads.body is not None and route.reads.body.info is not None:
        yield route.reads.body.info


def _model(name: str, component: dict[str, JsonValue], models: Sequence[type[BaseModel]]) -> type[BaseModel] | None:
    """The model of the request-only component `name`, pydantic's name for it, `NAME` or `NAME-Input`, or a module's
    path before it where two models share a name: the one whose keys are its properties, or None where none is."""
    base = name.removesuffix("-Input")
    keys = set(mapping(component.get("properties")))
    matching = [model for model in models if (model.__name__ == base or base.endswith(f"__{model.__name__}")) and {field.alias or key for key, field in model.model_fields.items()} == keys]
    return matching[0] if len(matching) == 1 else None


def _decimals_as_strings(schema: JsonValue | None, given: Iterator[dict[str, JsonValue]]) -> None:
    """Each decimal in a request's `schema`, which pydantic gives as a number or a string, as a string of its digits:
    as `given` has each, in the order the schema holds them, or with no bounds once `given` is spent. pydantic gives a
    decimal as a number's schema followed by a string's in one `anyOf`, and nothing else so; a union of a float and a
    str in a request body would read the same, and be given as a decimal too."""
    if isinstance(schema, list):
        for item in schema:
            _decimals_as_strings(item, given)
        return
    if not isinstance(schema, dict):
        return
    if isinstance(choices := schema.get("anyOf"), list):
        found: list[JsonValue] = []
        for choice in choices:
            if found and _is_number(found[-1]) and choice == {"type": "string"}:
                found[-1] = next(given, None) or decimals.schema([])
            else:
                found.append(choice)
        schema["anyOf"] = found
        if len(found) == 1 and isinstance(only := found[0], dict):
            del schema["anyOf"]
            schema.update(only)
            return
    for key, value in schema.items():
        if isinstance(value, dict | list) and key != decimals.EXTENSION:
            _decimals_as_strings(value, given)


def _is_number(schema: JsonValue) -> bool:
    bounds = {"type", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    return isinstance(schema, dict) and schema.get("type") == "number" and set(schema) <= bounds


def _strict(schema: JsonValue) -> None:
    """Every model's schema in `schema`, which has `properties`, with `additionalProperties: false`, unless pydantic
    says otherwise; a mapping's, which has none, keeps its own."""
    if isinstance(schema, list):
        for item in schema:
            _strict(item)
    elif isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            schema.setdefault("additionalProperties", False)
        for value in schema.values():
            _strict(value)


def _reached(roots: list[JsonValue], schemas: dict[str, JsonValue]) -> set[str]:
    """The names of the components `roots` refer to, at any depth."""
    found: set[str] = set()
    waiting = list(roots)
    while waiting:
        node = waiting.pop()
        if isinstance(node, list):
            waiting.extend(node)
        elif isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and (match := _REF.fullmatch(reference)) is not None and (name := match["name"]) not in found:
                found.add(name)
                waiting.append(schemas.get(name))
            waiting.extend(value for key, value in node.items() if key != "$ref")
    return found


def operations(document: dict[str, JsonValue]) -> Iterator[tuple[str, str, dict[str, JsonValue]]]:
    """Each operation in `document`, with its path and its method, in lower case as the document gives it."""
    for path, item in mapping(document.get("paths")).items():
        for method, operation in mapping(item).items():
            if isinstance(operation, dict):
                yield path, method, operation


def mapping(value: JsonValue | None) -> dict[str, JsonValue]:
    """`value` where it is an object of the document's, which an edit of it changes, or an empty one."""
    return value if isinstance(value, dict) else {}


def array(value: JsonValue | None) -> list[JsonValue]:
    """`value` where it is an array of the document's, or an empty one."""
    return value if isinstance(value, list) else []
