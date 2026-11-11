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
- what a success answers is what the app sends: a float may be `null`, since NaN and ±inf answer null, and each of a
  model's fields is required, since the app sends every one, its default too. A model a request takes as well, which
  takes no null and needs no defaulted field, is then two schemas, `NAME-Input` and `NAME-Output`, as FastAPI names
  them when pydantic's differ;
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
    found = app.openapi()
    if service.ui is not None:
        found["servers"] = [{"url": routes.API}]  # the paths stay the routes' own, and a client reaches them under /api
    return found


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
    _outputs(found)
    return found


def _outputs(found: dict[str, JsonValue]) -> None:
    """Each schema a success answers with, in `found`, as the app sends it: every float `null` too, and every field
    required. A component a request reaches too becomes two where the two differ."""
    schemas = mapping(mapping(found.get("components")).get("schemas"))
    answered: list[JsonValue] = []
    asked: list[JsonValue] = []
    for _, _, operation in operations(found):
        asked.append(operation.get("requestBody"))
        asked.append(operation.get("parameters"))
        for status, response in mapping(operation.get("responses")).items():
            if status.isdigit() and int(status) < HTTPStatus.BAD_REQUEST:
                answered.append(mapping(response).get("content"))
            else:
                asked.append(mapping(response).get("content"))  # a problem's own schema, which no success reaches, stays as it is
    sent, taken = _reached(answered, schemas), _reached(asked, schemas)
    local = {name: _as_sent(schemas[name], {}) != schemas[name] for name in sent}
    differs = {name: any(local.get(other, False) for other in _reached([schemas[name]], schemas) | {name}) for name in sent}
    split = {name for name in sent & taken if differs[name]}
    outputs = {name: f"{name}-Output" if name in split else name for name in sent}
    inputs = {name: f"{name}-Input" for name in split}
    for name in sorted(sent):
        output = _as_sent(schemas[name], outputs)
        if name in split:
            schemas[inputs[name]] = _renamed(schemas[name], inputs)
            del schemas[name]
        schemas[outputs[name]] = output
    for name in set(schemas) - set(outputs.values()) - set(inputs.values()):
        schemas[name] = _renamed(schemas[name], inputs)
    for container in answered:
        _rewrite(container, outputs, sent=True)
    for container in asked:
        _rewrite(container, inputs, sent=False)


def _as_sent(schema: JsonValue, names: dict[str, str]) -> JsonValue:
    """`schema` as a success sends it: each float `null` too, each object's every property required, and each
    reference to a component `names` renames, renamed."""
    if isinstance(schema, list):
        return [_as_sent(item, names) for item in schema]
    if not isinstance(schema, dict):
        return schema
    made: dict[str, JsonValue] = {key: value if key in _LITERAL else _as_sent(value, names) for key, value in schema.items()}
    if made.get("type") == "number":
        made["type"] = ["number", "null"]
    if isinstance(properties := made.get("properties"), dict) and properties:
        made["required"] = list(properties)
    if isinstance(reference := made.get("$ref"), str):
        made["$ref"] = _target(reference, names)
    return made


def _renamed(schema: JsonValue, names: dict[str, str]) -> JsonValue:
    """`schema`, each reference to a component `names` renames, renamed."""
    if isinstance(schema, list):
        return [_renamed(item, names) for item in schema]
    if not isinstance(schema, dict):
        return schema
    made: dict[str, JsonValue] = {key: value if key in _LITERAL else _renamed(value, names) for key, value in schema.items()}
    if isinstance(reference := made.get("$ref"), str):
        made["$ref"] = _target(reference, names)
    return made


def _rewrite(container: JsonValue | None, names: dict[str, str], *, sent: bool) -> None:
    """Each schema in a request's or a response's `container`, in place, as `_as_sent` or `_renamed` gives it."""
    if isinstance(container, list):
        for item in container:
            _rewrite(item, names, sent=sent)
    elif isinstance(container, dict):
        if isinstance(container.get("schema"), dict):
            container["schema"] = _as_sent(container["schema"], names) if sent else _renamed(container["schema"], names)
        for key, value in container.items():
            if key != "schema" and key not in _LITERAL:
                _rewrite(value, names, sent=sent)


def _target(reference: str, names: dict[str, str]) -> str:
    match = _REF.fullmatch(reference)
    return f"#/components/schemas/{names[match['name']]}" if match is not None and match["name"] in names else reference


_LITERAL = frozenset({"default", "example", "examples", "const", "enum", "x-basis-format"})
"""The keys whose values are values, never schemas."""


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
