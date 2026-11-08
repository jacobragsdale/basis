"""A service's OpenAPI document: what its app serves at `/openapi.json`, and what an API repo commits as its own
`openapi.json`, which `basis sync` writes and `basis check` compares, and which a UI's client is generated from.

FastAPI makes the document from the routes, and basis finishes it, so that it says what the app does:

- each operation's id is its handler's name, so a generated client reads `getPositions`, not
  `read_positions_positions_get`;
- every error is `application/problem+json`, the media type the app answers errors with: a 422 a `ValidationProblem`,
  whose `errors` name the fields at fault, and any other a `Problem`;
- every model is strict, `additionalProperties: false`: a request's unknown key is refused, and a response holds
  only the fields its model declares;
- every number is a JSON number, a float or an int, never a string (design.md, "Numbers"), as pydantic gives one, and
  a time always carries its offset, which JSON Schema's `date-time` format requires;
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
from pydantic import JsonValue, TypeAdapter

from basis.api import routes, sync
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


def finish(found: dict[str, JsonValue], declared: Sequence[Route] = ()) -> dict[str, JsonValue]:  # noqa: ARG001 -- the routes, which a later rule may read
    """FastAPI's document `found`, of the routes `declared`, as basis gives it: in place, and returned."""
    for _, _, operation in operations(found):
        for status, response in mapping(operation.get("responses")).items():
            content = mapping(mapping(response).get("content"))
            if (status == "default" or (status.isdigit() and int(status) >= HTTPStatus.BAD_REQUEST)) and "application/json" in content:
                content[MEDIA_TYPE] = content.pop("application/json")
    _strict(found)
    return found


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
