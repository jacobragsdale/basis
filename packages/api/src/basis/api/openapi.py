"""A service's OpenAPI document: what its app serves at `/openapi.json`, and what an API repo commits as its own
`openapi.json`, which `basis sync` writes and `basis check` compares, and which a UI's client is generated from.

FastAPI makes the document from the routes, and basis finishes it, so that it says what the app does:

- each operation's id is its handler's name, so a generated client reads `getPositions`, not
  `read_positions_positions_get`;
- every error is `application/problem+json`, the media type the app answers errors with: a 422 a `ValidationProblem`,
  whose `errors` name the fields at fault, and any other a `Problem`;
- every model is strict, `additionalProperties: false`: a request's unknown key is refused, and a response holds
  only the fields its model declares;
- a decimal a request gives is a string of its digits, with `strict.DECIMAL` as its pattern, never a number, as
  basis reads one; a response gives a decimal as a string too, and a time in either always carries its offset, which
  JSON Schema's `date-time` format requires;
- a dataset route's success lists `application/vnd.apache.arrow.stream` beside its JSON, as a binary string, the one
  form of a binary body that generated clients read as one.

The paths carry no `/api`: the deployment that serves a UI and its API on one host routes `/api` to the API and
removes it, and the UI's client adds it.

The committed file is the document with basis's marker as its first key, `x-basis`, and two spaces of indent, as
`text` writes it."""

import json
import re
from collections.abc import Iterator
from http import HTTPStatus
from typing import override

from fastapi import FastAPI
from pydantic import JsonValue, TypeAdapter

from basis.api import routes, strict, sync
from basis.api.errors import MEDIA_TYPE
from basis.api.service import Service
from basis.core.sync import MARKER

FILE = sync.DOCUMENT
"""Where an API repo commits its document: at its root."""
DECIMAL: dict[str, JsonValue] = {"type": "string", "pattern": f"^{strict.DECIMAL}$"}
"""A request's decimal, as the document gives it."""
_REF = re.compile(r"#/components/schemas/(?P<name>.+)")
_DOCUMENT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class Application(FastAPI):
    """FastAPI, whose OpenAPI document is basis's, as `finish` makes it from FastAPI's."""

    _document: dict[str, JsonValue] | None = None

    @override
    def openapi(self) -> dict[str, JsonValue]:
        if self._document is None:
            self._document = self.openapi_schema = finish(_DOCUMENT.validate_python(super().openapi()))
        return self._document


def document(service: Service) -> dict[str, JsonValue]:
    """`service`'s document, as its app serves it, made without its settings or its resources."""
    app = Application(title=service.name)
    routes.add(app, service)
    return app.openapi()


def text(service: Service) -> str:
    """`service`'s document as its repo commits it, with basis's marker first."""
    return json.dumps({"x-basis": MARKER, **document(service)}, indent=2, ensure_ascii=False) + "\n"


def finish(found: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """FastAPI's document `found`, as basis gives it: in place, and returned."""
    schemas = _object(_object(found.get("components")).get("schemas"))
    asked: list[JsonValue] = []
    answered: list[JsonValue] = []
    for operation in _operations(found):
        asked.extend(_object(parameter).get("schema") for parameter in _array(operation.get("parameters")))
        asked.extend(_object(media).get("schema") for media in _object(_object(operation.get("requestBody")).get("content")).values())
        for status, response in _object(operation.get("responses")).items():
            content = _object(_object(response).get("content"))
            if (status == "default" or (status.isdigit() and int(status) >= HTTPStatus.BAD_REQUEST)) and "application/json" in content:
                content[MEDIA_TYPE] = content.pop("application/json")
            answered.extend(_object(media).get("schema") for media in content.values())
    request_only = _reached(asked, schemas) - _reached(answered, schemas)  # a model in both has no decimal, which differs in each
    for schema in [*asked, *(schemas[name] for name in sorted(request_only))]:
        _decimals_as_strings(schema)
    _strict(found)
    return found


def _decimals_as_strings(schema: JsonValue) -> None:
    """Each decimal in a request's `schema`, which pydantic gives as a number or a string, as a string of its digits.
    pydantic gives a decimal as a number's schema followed by a string's in one `anyOf`, and nothing else so; a
    union of a float and a str in a request body would read the same, and be given as a decimal too."""
    if isinstance(schema, list):
        for item in schema:
            _decimals_as_strings(item)
        return
    if not isinstance(schema, dict):
        return
    if isinstance(choices := schema.get("anyOf"), list):
        found: list[JsonValue] = []
        for choice in choices:
            if found and _is_number(found[-1]) and choice == {"type": "string"}:
                found[-1] = dict(DECIMAL)
            else:
                found.append(choice)
        schema["anyOf"] = found
        if len(found) == 1 and isinstance(only := found[0], dict):
            del schema["anyOf"]
            schema.update(only)
    for value in schema.values():
        if isinstance(value, dict | list):
            _decimals_as_strings(value)


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


def _operations(found: dict[str, JsonValue]) -> Iterator[dict[str, JsonValue]]:
    for item in _object(found.get("paths")).values():
        for operation in _object(item).values():
            if isinstance(operation, dict):
                yield operation


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _array(value: JsonValue | None) -> list[JsonValue]:
    return value if isinstance(value, list) else []
