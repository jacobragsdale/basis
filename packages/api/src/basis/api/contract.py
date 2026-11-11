"""The API's breaking-change check: its `openapi.json`, compared with the one at its last release, which fails when a
change would break a UI built against that release. `basis check --full` runs it as basis-api's `Part.contract`, and
`basis review` prints what it found.

The baseline is a file the repo commits, `openapi.released.json` beside `openapi.json`: the document UIs built against
the last release rely on, which `basis release-document VERSION` writes from `openapi.json` in the commit a release
tags, and which the check reads as it is, so it needs no tag, history or worktree, and a shallow clone or one that
fetches no tags checks the same as any other. With none committed yet, there is no earlier release to break, and the
check passes, saying so. The file carries no marker of basis's, since it is the repo's own: `basis sync` leaves it
alone, and only `basis release-document` writes it again.

A UI reads what a response gives and sends what a request takes, so each side breaks the other way:

- **Responses.** A UI drops a field it does not know, so a field added to a response is never breaking. A field
  removed or renamed is, and so is one whose type changes so that it may hold a value the UI does not expect: another
  type, `null`, an enum's new value, or absence where it was required.
- **Requests.** An API refuses a field it does not know, so a field a UI sends that the API removes or renames breaks
  it, and so does one whose type no longer takes what the UI sends: a type dropped, an enum's value dropped, or a
  field that becomes required. A field added to a request is breaking only when it is required; an optional one is
  not, which is why an API that takes a new field deploys before the UI that sends it.
- **Operations.** An operation removed, or moved to another method or path, breaks the UIs that call it, and so does a
  new operation id, its handler's name, which renames the function a UI's generated client calls; so do a new success
  status, a body the route no longer takes or now requires, and a dataset route that no longer answers Arrow. An
  operation added never breaks one.

A path parameter is compared by its place in the path, since a UI's URL names it by none; a query's by its name. Each
side's types are compared through references, unions and `null`, arrays and maps, by JSON type and format, a request's
decimal string apart from any other, and each model of a union by itself, by its component's name; a keyword the check
does not compare is listed where it is. Each operation's errors, its `x-basis-errors`, are compared by type: one that
answers another status breaks a UI, and one added, removed or titled anew is listed, as is a field's display format
that changes. Constraints such as a pattern or a bound are not compared."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

from pydantic import ConfigDict, JsonValue, ValidationError

from basis.api import arrow, openapi
from basis.core.errors import BasisError
from basis.core.model import Model
from basis.core.verbs import table

PART = "api"
_PARAMETER = re.compile(r"\{[^}]*\}")


class ContractError(BasisError, ValueError):
    """An `openapi.json` that breaks its contract with the last release's, or a repo that declares no baseline."""


RELEASED = "openapi.released.json"
"""Where an API repo commits the document its last release's UIs rely on: beside its `openapi.json`."""
RELEASE_KEY = "x-basis-release"
"""The released document's first key: the release it is, as `basis release-document` was given it."""


class Declared(Model):
    """`[tool.basis.api]`, what an API repo declares to basis-api: nothing yet, so a key in it is refused."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def declared(repo: Path) -> Declared:
    """The repo's `[tool.basis.api]`, read strictly: a key basis-api does not read is refused, with the fix."""
    found = table(repo, PART)
    where = f"{repo / 'pyproject.toml'}'s [tool.basis.{PART}]"
    if "releases" in found:
        msg = (
            f"{where} declares releases, the glob of release tags basis-api reads no longer: remove it, since the breaking-change check compares"
            f" openapi.json with the committed {RELEASED}, which `uv run basis release-document VERSION` writes at a release"
        )
        raise ContractError(msg)
    try:
        return Declared.model_validate(found)
    except ValidationError as error:
        unknown = [str(entry["loc"][0]) for entry in error.errors(include_url=False, include_input=False)]
        msg = f"{where} has {', '.join(unknown)}, which basis-api does not read: remove it"
        raise ContractError(msg) from None


def contract(repo: Path) -> str | None:
    """basis-api's `Part.contract`: how the repo's `openapi.json` differs from the released document committed beside
    it, as Markdown whose first line sums it up, or None when it does not; a `ContractError` when it breaks a UI built
    against it."""
    declared(repo)
    return check(repo / openapi.FILE, repo / RELEASED)


def check(current: Path, released: Path) -> str | None:
    """How the document at `current` differs from the released one at `released`, as `contract` gives it."""
    if not current.is_file():
        msg = f"{current} is missing, so its contract cannot be checked: run `uv run basis sync`, which writes it"
        raise ContractError(msg)
    after = _parsed(current.read_text(encoding="utf-8"), str(current))
    if not released.is_file():
        return f"No earlier release to compare with: no {released.name} is committed, which `basis release-document VERSION` writes at a release."
    before = _parsed(released.read_text(encoding="utf-8"), str(released))
    release = str(before.pop(RELEASE_KEY, released.name))
    found = compare(before, after)
    if found.breaking:
        listed = "; ".join(found.breaking)
        fix = "keep each field a UI reads or sends as it was, adding new ones beside it, or add the changed operation beside the old one, under a path of its own"
        msg = f"{current.name} breaks its contract with release {release}'s: {listed}: {fix}"
        raise ContractError(msg)
    return summed(found, release)


def release(repo: Path, version: str) -> Path:
    """Write the repo's released document, `openapi.released.json`, from its `openapi.json`, as release `version`:
    `basis release-document`, in the commit a release tags."""
    current = repo / openapi.FILE
    if not current.is_file():
        msg = f"{current} is missing, so no release of it can be recorded: run `uv run basis sync`, which writes it"
        raise ContractError(msg)
    target = repo / RELEASED
    target.write_text(released_text(_parsed(current.read_text(encoding="utf-8"), str(current)), version), encoding="utf-8", newline="")
    return target


def released_text(document: Document, version: str) -> str:
    """`document` as its release `version` commits it: with `x-basis-release` first, in place of basis's marker, so the
    file is the repo's own, which `basis sync` leaves alone."""
    given = {key: value for key, value in document.items() if key != "x-basis"}
    return json.dumps({RELEASE_KEY: version, **given}, indent=2, ensure_ascii=False) + "\n"


def summed(found: Changes, release: str) -> str | None:
    """What `found` holds that breaks nothing, as `contract` gives it, with each keyword the check did not compare."""
    if not found.compatible and not found.skipped:
        return None
    changed = f"Changed since release `{release}`, with nothing a UI built against it relies on broken."
    lines = [changed if found.compatible else f"Unchanged since release `{release}`, as far as the check compares."]
    lines += ["", *(f"- {change}" for change in found.compatible)] if found.compatible else []
    lines += ["", "Not compared, so a change there goes unseen:", "", *(f"- {where}" for where in found.skipped)] if found.skipped else []
    return "\n".join(lines)


@dataclass
class Changes:
    breaking: list[str] = field(default_factory=list[str])
    compatible: list[str] = field(default_factory=list[str])
    skipped: list[str] = field(default_factory=list[str])
    """Each keyword of a schema the check does not compare, where it is, so a change there is known to go unseen."""


@dataclass(frozen=True)
class Shape:
    """What one field of a request or a response holds."""

    kinds: frozenset[str]
    """Its JSON types, a string's format among them, as `string:date`, and `null` where it may be null."""
    required: bool
    values: frozenset[str] | None
    """An enum's values, as JSON writes each; None for a field of any value."""
    models: frozenset[str] = frozenset()
    """The models, of a union's or alone, it may be one of, by their components' names: a union of two or more names
    each one's fields by it, as `body<Swap>.rate`."""
    shown: str | None = None
    """Its display format, `x-basis-format`, as JSON writes it with its keys sorted; None for a field without one."""


_COMPARED = frozenset({"type", "format", "enum", "const", "properties", "required", "items", "prefixItems", "additionalProperties", "anyOf", "oneOf", "$ref"})
_UNCOMPARED = frozenset({
    "title", "description", "default", "examples", "example", "deprecated", "readOnly", "writeOnly", "discriminator", "contentMediaType", "contentEncoding",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
})  # fmt: skip
"""What the check knows and leaves alone: a schema's annotations, and constraints, which a UI does not rely on."""
_LABEL = re.compile(r"<[^>]*>")


type Document = dict[str, JsonValue]


def compare(before: Document, after: Document) -> Changes:
    """What changed from the document `before` to `after`, breaking and not."""
    found = Changes()
    old, new = _operations(before), _operations(after)
    for key, operation in old.items():
        if key not in new:
            found.breaking.append(f"{_named(key, operation)} is removed, or has moved")
    for key, operation in new.items():
        if key not in old:
            found.compatible.append(f"{_named(key, operation)} is added")
            continue
        _operation(_named(key, operation), old[key], operation, before=before, after=after, found=found)
    return found


def _operation(name: str, old: Document, new: Document, *, before: Document, after: Document, found: Changes) -> None:  # noqa: PLR0913 -- both operations, and the documents their references resolve in
    if old.get("operationId") != new.get("operationId"):
        found.breaking.append(f"{name} has the operation id {new.get('operationId')}, renaming its client's function")
    old_status, new_status = _success(old), _success(new)
    if old_status != new_status:
        found.breaking.append(f"{name} succeeds with {new_status}, not {old_status}")
    old_body, new_body = _body(old), _body(new)
    if old_body is not None and new_body is None:
        found.breaking.append(f"{name} takes no body")
    elif old_body is None and new_body is not None and new_body[1]:
        found.breaking.append(f"{name} requires a body")
    skipped: list[tuple[str, str]] = []
    requests = _fields(_parameters(old, before), before, skipped), _fields(_parameters(new, after), after, skipped)
    if old_body is not None and new_body is not None:
        requests[0].update(_flat("body", old_body[0], before, required=old_body[1], skipped=skipped))
        requests[1].update(_flat("body", new_body[0], after, required=new_body[1], skipped=skipped))
    _sent(name, *requests, found)
    old_answer, new_answer = _answer(old, old_status), _answer(new, new_status)
    answers = dict(_flat("the response", old_answer, before, required=True, skipped=skipped)), dict(_flat("the response", new_answer, after, required=True, skipped=skipped))
    _read(name, *answers, found)
    found.skipped.extend(f"{name}: {path}'s {keyword}" for path, keyword in dict.fromkeys(skipped))
    _errors(name, old, new, found)
    old_arrow, new_arrow = _arrow(old, old_status), _arrow(new, new_status)
    if old_arrow and not new_arrow:
        found.breaking.append(f"{name} no longer answers Arrow")
    elif new_arrow and not old_arrow:
        found.compatible.append(f"{name} answers Arrow too")


def _errors(name: str, old: Document, new: Document, found: Changes) -> None:
    """The errors an operation lists, by type: one that answers another status breaks a UI that tells it by both, and
    one added, removed or titled anew is listed."""
    before, after = _listed(old), _listed(new)
    for kind, (status, title) in before.items():
        if kind not in after:
            found.compatible.append(f"{name} no longer answers {kind}")
            continue
        now_status, now_title = after[kind]
        if now_status != status:
            found.breaking.append(f"{name} answers {kind} with {now_status}, not {status}")
        if now_title != title:
            found.compatible.append(f"{name} titles {kind} {now_title!r}, not {title!r}")
    found.compatible.extend(f"{name} may answer {kind}, {status}" for kind, (status, _) in after.items() if kind not in before)


def _listed(operation: Document) -> dict[str, tuple[JsonValue, JsonValue]]:
    return {str(entry.get("type")): (entry.get("status"), entry.get("title")) for entry in map(openapi.mapping, openapi.array(operation.get("x-basis-errors")))}


def _sent(name: str, old: dict[str, Shape], new: dict[str, Shape], found: Changes) -> None:
    """What a UI sends, which the API reads contravariantly: each field the old operation took that the new one no
    longer takes as it did, a type it narrows, an enum's value it drops, or a field it now requires."""
    for path, shape in old.items():
        for now_path, now in _counterparts(path, new):
            _sent_one(name, now_path, shape, now, found)
        if not _counterparts(path, new):
            found.breaking.append(f"{name} no longer takes {path}")
    for path, shape in new.items():
        if path not in old and not _counterparts(path, old):
            if shape.required:
                found.breaking.append(f"{name} requires {path}, which it did not take")
            else:
                found.compatible.append(f"{name} takes {path}")


def _sent_one(name: str, path: str, shape: Shape, now: Shape, found: Changes) -> None:
    if refused := frozenset(kind for kind in shape.kinds if not _covers(now.kinds, kind)):
        found.breaking.append(f"{name} no longer takes {path} as {_kinds(refused)}")
    if shape.values is not None and now.values is not None and (gone := shape.values - now.values):
        found.breaking.append(f"{name} no longer takes {path} as {_kinds(gone)}")
    if shape.values is None and now.values is not None and shape.kinds - {"null"}:
        found.breaking.append(f"{name} no longer takes {path} as any value but {_kinds(now.values)}")
    if len(shape.models) > 1 and (gone := shape.models - now.models):
        found.breaking.append(f"{name} no longer takes {path} as {_kinds(gone)}")
    if now.required and not shape.required:
        found.breaking.append(f"{name} requires {path}")
    widened = frozenset(kind for kind in now.kinds if not _covers(shape.kinds, kind))
    if shape.values is not None:
        widened |= (now.values - shape.values) if now.values is not None else frozenset({"any value"})
    if len(now.models) > max(len(shape.models), 1):
        widened |= now.models - shape.models
    if widened:
        found.compatible.append(f"{name} takes {path} as {_kinds(widened)} too")
    if shape.required and not now.required:
        found.compatible.append(f"{name} no longer requires {path}")


def _counterparts(path: str, other: dict[str, Shape]) -> list[tuple[str, Shape]]:
    """The field at `path` in `other`: at the same path, or, where one side is a union and the other is not, at each
    path that names the field but for the models it is of."""
    if path in other:
        return [(path, other[path])]
    bare = _LABEL.sub("", path)
    return [(found, shape) for found, shape in other.items() if _LABEL.sub("", found) == bare]


def _read(name: str, old: dict[str, Shape], new: dict[str, Shape], found: Changes) -> None:
    """What a UI reads, which it reads covariantly: each field the old response gave that the new one no longer gives
    as it did, a type or an enum's value it adds, or a field that may be absent."""
    for path, shape in old.items():
        counterparts = _counterparts(path, new)
        if not counterparts:
            found.breaking.append(f"{name}: {path} is removed, or renamed")
        for now_path, now in counterparts:
            if added := frozenset(kind for kind in now.kinds if not _covers(shape.kinds, kind)):
                found.breaking.append(f"{name}: {now_path} may be {_kinds(added)}")
            if shape.values is not None and (new_values := (now.values or frozenset({"any value"})) - shape.values):
                found.breaking.append(f"{name}: {now_path} may be {_kinds(new_values)}")
            if len(now.models) > len(shape.models) and (more := now.models - shape.models):
                found.breaking.append(f"{name}: {now_path} may be {_kinds(more)}")
            if shape.required and not now.required:
                found.breaking.append(f"{name}: {now_path} may be absent")
            if shape.shown != now.shown:
                found.compatible.append(f"{name}: {now_path} is shown as {now.shown or 'its type'}, not {shape.shown or 'its type'}")
    found.compatible.extend(f"{name}: {path} is added" for path in new if path not in old and not _counterparts(path, old))


def _covers(kinds: frozenset[str], kind: str) -> bool:
    """Whether a field of `kinds` takes every value of `kind`: its own, an integer as a number, a string of any format
    as a string, and anything as `any`."""
    base = kind.partition(":")[0]
    return kind in kinds or "any" in kinds or (kind == "integer" and "number" in kinds) or (base == "string" and kind != base and "string" in kinds)


def _operations(document: Document) -> dict[tuple[str, str], Document]:
    """Each operation, by its method and its path with its parameters unnamed, as a UI's URL reaches it."""
    return {(method.upper(), _PARAMETER.sub("{}", path)): operation | {"x-path": path} for path, method, operation in openapi.operations(document)}


def _named(key: tuple[str, str], operation: Document) -> str:
    return f"{key[0]} {operation.get('x-path')} ({operation.get('operationId')})"


def _success(operation: Document) -> str | None:
    statuses = sorted(status for status in openapi.mapping(operation.get("responses")) if status.isdigit() and int(status) < HTTPStatus.BAD_REQUEST)
    return statuses[0] if statuses else None


def _content(operation: Document, status: str | None) -> Document:
    return openapi.mapping(openapi.mapping(openapi.mapping(operation.get("responses")).get(status or "")).get("content"))


def _answer(operation: Document, status: str | None) -> JsonValue | None:
    return openapi.mapping(_content(operation, status).get("application/json")).get("schema")


def _arrow(operation: Document, status: str | None) -> bool:
    return arrow.MEDIA_TYPE in _content(operation, status)


def _body(operation: Document) -> tuple[JsonValue | None, bool] | None:
    body = openapi.mapping(operation.get("requestBody"))
    if not body:
        return None
    return openapi.mapping(openapi.mapping(body.get("content")).get("application/json")).get("schema"), body.get("required") is True


def _parameters(operation: Document, document: Document) -> Iterator[tuple[str, JsonValue | None, bool]]:
    """Each parameter's place, its schema and whether it is required: a path's by its place in the path."""
    path = str(operation.get("x-path"))
    places = [found[1:-1] for found in _PARAMETER.findall(path)]
    for given in openapi.array(operation.get("parameters")):
        parameter = _resolved(given, document)
        name, where = str(parameter.get("name")), str(parameter.get("in"))
        place = f"the path's parameter {places.index(name) + 1}" if where == "path" and name in places else f"the {where} parameter {name}"
        yield place, parameter.get("schema"), parameter.get("required") is True


def _fields(parameters: Iterator[tuple[str, JsonValue | None, bool]], document: Document, skipped: list[tuple[str, str]]) -> dict[str, Shape]:
    found: dict[str, Shape] = {}
    for place, schema, required in parameters:
        found.update(_flat(place, schema, document, required=required, skipped=skipped))
    return found


def _flat(  # noqa: PLR0913 -- where, what, and how it is walked, by keyword
    path: str, schema: JsonValue | None, document: Document, *, required: bool, skipped: list[tuple[str, str]], within: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Shape]]:
    """`schema`'s shape at `path`, and every field's within it, by its path: `.key` for a property, `[N]` for a
    tuple's item N, `[]` for an array's items, `{}` for a map's values, and `<Model>` before the fields of each model
    of a union of two or more. A component already being walked is not walked again, and each keyword the check
    does not compare is noted in `skipped`, with its path."""
    arms = _arms(schema, document, within)
    kinds = frozenset(kind for arm, _, _ in arms for kind in _kind(arm))
    values = [value for arm, _, _ in arms for value in _values(arm)]
    enumerated = all(_values(arm) for arm, _, _ in arms if _kind(arm) != {"null"})
    models = frozenset(label for arm, _, label in arms if "properties" in arm)
    shown = next((json.dumps(arm["x-basis-format"], sort_keys=True, separators=(",", ":")) for arm, _, _ in arms if "x-basis-format" in arm), None)
    yield path, Shape(kinds, required, frozenset(values) if values and enumerated else None, models, shown)
    union = len(models) > 1  # each model's fields by it, so one it adds or retypes is its own
    for arm, seen, label in arms:
        skipped.extend((path, keyword) for keyword in arm if keyword not in _COMPARED and keyword not in _UNCOMPARED and not keyword.startswith("x-"))
        at = f"{path}<{label}>" if union and "properties" in arm else path
        needed = {str(key) for key in openapi.array(arm.get("required"))}
        for key, inner in openapi.mapping(arm.get("properties")).items():
            yield from _flat(f"{at}.{key}", inner, document, required=key in needed, skipped=skipped, within=seen)
        for index, item in enumerate(openapi.array(arm.get("prefixItems"))):
            yield from _flat(f"{path}[{index}]", item, document, required=True, skipped=skipped, within=seen)
        if isinstance(items := arm.get("items"), dict):
            yield from _flat(f"{path}[]", items, document, required=True, skipped=skipped, within=seen)
        if isinstance(values_of := arm.get("additionalProperties"), dict):
            yield from _flat(f"{path}{{}}", values_of, document, required=True, skipped=skipped, within=seen)


def _arms(schema: JsonValue | None, document: Document, within: frozenset[str], label: str | None = None) -> list[tuple[Document, frozenset[str], str]]:
    """`schema` as the schemas any of whose values it holds, each resolved through its references, with the
    components walked to reach it, and its name: its component's, or its place among the union's."""
    if not isinstance(schema, dict):
        return []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        return [] if name in within else _arms(_resolved(schema, document), document, within | {name}, name)
    choices = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(choices, list):
        named = [None if isinstance(choice, dict) and "$ref" in choice else f"{label or 'arm'} {index + 1}" for index, choice in enumerate(choices)]
        return [found for choice, name in zip(choices, named, strict=True) for found in _arms(choice, document, within, name)]
    return [(schema, within, label or "model")]


def _kind(schema: Document) -> set[str]:
    kind = schema.get("type")
    kinds: set[str] = {str(found) for found in kind} if isinstance(kind, list) else {str(kind)} if kind is not None else set[str]()
    if "const" in schema or "enum" in schema:
        kinds |= {_type_of(value) for value in _enumerated(schema)}
    if (form := schema.get("format")) is not None:
        kinds = {f"{found}:{form}" if found == "string" else found for found in kinds}
    return kinds or {"any"}


def _values(schema: Document) -> list[str]:
    return [json.dumps(value) for value in _enumerated(schema)]


def _enumerated(schema: Document) -> list[JsonValue]:
    if "const" in schema:
        return [schema["const"]]
    return openapi.array(schema.get("enum"))


def _type_of(value: JsonValue) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int():
            return "integer"
        case float():
            return "number"
        case str():
            return "string"
        case _:
            return "any"


def _kinds(kinds: frozenset[str]) -> str:
    return " or ".join(sorted(kinds))


def _resolved(schema: JsonValue | None, document: Document) -> Document:
    found: JsonValue | None = schema
    seen: set[str] = set()
    while isinstance(found, dict) and isinstance(reference := found.get("$ref"), str) and reference.startswith("#/") and reference not in seen:
        seen.add(reference)
        target: JsonValue | None = document
        for part in reference[2:].split("/"):
            target = openapi.mapping(target).get(part.replace("~1", "/").replace("~0", "~"))
        found = target
    return openapi.mapping(found)


def _parsed(text: str, where: str) -> Document:
    try:
        found = json.loads(text)
    except json.JSONDecodeError as error:
        msg = f"{where} is not JSON ({error}): run `uv run basis sync`, which writes it"
        raise ContractError(msg) from None
    return openapi.mapping(found)
