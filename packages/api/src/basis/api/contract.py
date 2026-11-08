"""The API's breaking-change check: its `openapi.json`, compared with the one at its last release, which fails when a
change would break a UI built against that release. `basis check --full` runs it as basis-api's `Part.contract`, and
`basis review` prints what it found.

The baseline is declared, never guessed. The repo names the tags that mark its releases, as a glob:

    [tool.basis.api]
    releases = "v*"

and the last release is the nearest tag that matches it among the checked commit's ancestors, the commit itself
included, as `git describe --tags --abbrev=0 --match GLOB HEAD` finds it. With no such tag, or none whose commit has an
`openapi.json`, there is no earlier release to break, and the check passes, saying so. A clone that may lack the
release it descends from fails instead, with the fix: a shallow one, such as a CI checkout's default, or one that
fetches no tags (`remote.NAME.tagOpt` is `--no-tags`, as `git clone --no-tags` sets it). A full clone whose tags were
never fetched looks, from inside it, like a repo with no release; the remote is not asked, since that takes the
network and its credentials.

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
decimal string apart from any other; an error's problem is basis's, the same in every API, and constraints such as a
pattern or a bound are not compared."""

import json
import os
import re
import subprocess
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
_GIT_NONE = ("no names found", "no tags can describe", "cannot describe")
"""What `git describe` says when no tag matches, in lower case."""


class ContractError(BasisError, ValueError):
    """An `openapi.json` that breaks its contract with the last release's, or a repo that declares no baseline."""


class Declared(Model):
    """`[tool.basis.api]`, what an API repo declares to basis-api."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    releases: str
    """The glob of the tags that mark the API's releases, such as `v*`, which `git describe --match` reads."""


def declared(repo: Path) -> Declared:
    """The repo's `[tool.basis.api]`, read strictly: a key basis-api does not read is refused, with the fix."""
    found = table(repo, PART)
    where = f"{repo / 'pyproject.toml'}'s [tool.basis.{PART}]"
    example = 'releases = "v*", the glob of the tags that mark its releases, which the breaking-change check compares openapi.json with'
    try:
        return Declared.model_validate(found)
    except ValidationError as error:
        problems = error.errors(include_url=False, include_input=False)
        unknown = [str(entry["loc"][0]) for entry in problems if entry["type"] == "extra_forbidden"]
        if unknown:
            msg = f"{where} has {', '.join(unknown)}, which basis-api does not read: remove it; it reads {example}"
        elif "releases" not in found:
            msg = f"{where} declares no releases: add {example}"
        else:
            msg = f"{where} releases is not a glob: give one string, as {example}"
        raise ContractError(msg) from None


def contract(repo: Path) -> str | None:
    """basis-api's `Part.contract`: how the repo's `openapi.json` differs from its last release's, as Markdown whose
    first line sums it up, or None when it does not; a `ContractError` when it breaks a UI built against it."""
    return check(repo, openapi.FILE, declared(repo).releases)


def check(repo: Path, document: str, releases: str) -> str | None:
    """How the document at `document`, relative to `repo`, differs from the one at the last tag `releases` matches,
    as `contract` gives it."""
    current = repo / document
    if not current.is_file():
        msg = f"{current} is missing, so its contract cannot be checked: run `uv run basis sync`, which writes it"
        raise ContractError(msg)
    after = _parsed(current.read_text(encoding="utf-8"), str(current))
    tag = _last_release(repo, releases)
    if tag is None:
        return f"No earlier release to compare with: no tag matches `{releases}` among this commit's ancestors."
    if not _at(repo, tag, document):
        return f"No earlier document to compare with: release `{tag}` has no `{document}`."
    shown = _git(repo, "show", f"{tag}:./{document}")
    if shown.returncode != 0:
        said = " ".join(shown.stderr.split())
        msg = f"git cannot read {document} at release {tag}: {said}: check the clone's history and tags, as `git fetch --tags` fetches them"
        raise ContractError(msg)
    found = compare(_parsed(shown.stdout, f"{tag}:{document}"), after)
    if found.breaking:
        listed = "; ".join(found.breaking)
        fix = "keep each field a UI reads or sends as it was, adding new ones beside it, or add the changed operation beside the old one, under a path of its own"
        msg = f"{document} breaks its contract with release {tag}'s: {listed}: {fix}"
        raise ContractError(msg)
    if not found.compatible:
        return None
    return "\n".join([f"Changed since release `{tag}`, with nothing a UI built against it relies on broken.", "", *(f"- {change}" for change in found.compatible)])


@dataclass
class Changes:
    breaking: list[str] = field(default_factory=list[str])
    compatible: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Shape:
    """What one field of a request or a response holds."""

    kinds: frozenset[str]
    """Its JSON types, a string's format among them, as `string:date`, and `null` where it may be null."""
    required: bool
    values: frozenset[str] | None
    """An enum's values, as JSON writes each; None for a field of any value."""


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
    requests = _fields(_parameters(old, before), before), _fields(_parameters(new, after), after)
    if old_body is not None and new_body is not None:
        requests[0].update(_flat("body", old_body[0], before, required=old_body[1]))
        requests[1].update(_flat("body", new_body[0], after, required=new_body[1]))
    _sent(name, *requests, found)
    old_answer, new_answer = _answer(old, old_status), _answer(new, new_status)
    _read(name, dict(_flat("the response", old_answer, before, required=True)), dict(_flat("the response", new_answer, after, required=True)), found)
    old_arrow, new_arrow = _arrow(old, old_status), _arrow(new, new_status)
    if old_arrow and not new_arrow:
        found.breaking.append(f"{name} no longer answers Arrow")
    elif new_arrow and not old_arrow:
        found.compatible.append(f"{name} answers Arrow too")


def _sent(name: str, old: dict[str, Shape], new: dict[str, Shape], found: Changes) -> None:
    """What a UI sends, which the API reads contravariantly: each field the old operation took that the new one no
    longer takes as it did, a type it narrows, an enum's value it drops, or a field it now requires."""
    for path, shape in old.items():
        if (now := new.get(path)) is None:
            found.breaking.append(f"{name} no longer takes {path}")
            continue
        if refused := frozenset(kind for kind in shape.kinds if not _covers(now.kinds, kind)):
            found.breaking.append(f"{name} no longer takes {path} as {_kinds(refused)}")
        if shape.values is not None and now.values is not None and (gone := shape.values - now.values):
            found.breaking.append(f"{name} no longer takes {path} as {_kinds(gone)}")
        if now.required and not shape.required:
            found.breaking.append(f"{name} requires {path}")
        widened = frozenset(kind for kind in now.kinds if not _covers(shape.kinds, kind))
        if shape.values is not None:
            widened |= (now.values - shape.values) if now.values is not None else frozenset({"any value"})
        if widened:
            found.compatible.append(f"{name} takes {path} as {_kinds(widened)} too")
        if shape.required and not now.required:
            found.compatible.append(f"{name} no longer requires {path}")
    for path, shape in new.items():
        if path not in old:
            if shape.required:
                found.breaking.append(f"{name} requires {path}, which it did not take")
            else:
                found.compatible.append(f"{name} takes {path}")


def _read(name: str, old: dict[str, Shape], new: dict[str, Shape], found: Changes) -> None:
    """What a UI reads, which it reads covariantly: each field the old response gave that the new one no longer gives
    as it did, a type or an enum's value it adds, or a field that may be absent."""
    for path, shape in old.items():
        if (now := new.get(path)) is None:
            found.breaking.append(f"{name}: {path} is removed, or renamed")
            continue
        if added := frozenset(kind for kind in now.kinds if not _covers(shape.kinds, kind)):
            found.breaking.append(f"{name}: {path} may be {_kinds(added)}")
        if shape.values is not None and (new_values := (now.values or frozenset({"any value"})) - shape.values):
            found.breaking.append(f"{name}: {path} may be {_kinds(new_values)}")
        if shape.required and not now.required:
            found.breaking.append(f"{name}: {path} may be absent")
    found.compatible.extend(f"{name}: {path} is added" for path in new if path not in old)


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


def _fields(parameters: Iterator[tuple[str, JsonValue | None, bool]], document: Document) -> dict[str, Shape]:
    found: dict[str, Shape] = {}
    for place, schema, required in parameters:
        found.update(_flat(place, schema, document, required=required))
    return found


def _flat(path: str, schema: JsonValue | None, document: Document, *, required: bool, within: frozenset[str] = frozenset()) -> Iterator[tuple[str, Shape]]:
    """`schema`'s shape at `path`, and every field's within it, by its path: `.key` for a property, `[]` for an array's
    items, `{}` for a map's values. A component already being walked is not walked again."""
    arms = _arms(schema, document, within)
    kinds = frozenset(kind for arm, _ in arms for kind in _kind(arm))
    values = [value for arm, _ in arms for value in _values(arm)]
    enumerated = all(_values(arm) for arm, _ in arms if _kind(arm) != {"null"})
    yield path, Shape(kinds, required, frozenset(values) if values and enumerated else None)
    alone = sum(1 for arm, _ in arms if "properties" in arm) == 1  # a field one model of a union requires may be absent
    for arm, seen in arms:
        needed = {str(key) for key in openapi.array(arm.get("required"))}
        for key, inner in openapi.mapping(arm.get("properties")).items():
            yield from _flat(f"{path}.{key}", inner, document, required=alone and key in needed, within=seen)
        if isinstance(items := arm.get("items"), dict):
            yield from _flat(f"{path}[]", items, document, required=True, within=seen)
        if isinstance(values_of := arm.get("additionalProperties"), dict):
            yield from _flat(f"{path}{{}}", values_of, document, required=True, within=seen)


def _arms(schema: JsonValue | None, document: Document, within: frozenset[str]) -> list[tuple[Document, frozenset[str]]]:
    """`schema` as the schemas any of whose values it holds, each resolved through its references, with the
    components walked to reach it."""
    if not isinstance(schema, dict):
        return []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        return [] if name in within else _arms(_resolved(schema, document), document, within | {name})
    choices = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(choices, list):
        return [found for choice in choices for found in _arms(choice, document, within)]
    return [(schema, within)]


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


def _last_release(repo: Path, releases: str) -> str | None:
    """The nearest tag that matches `releases` among HEAD's ancestors, HEAD included, or None; a clone that may lack it
    is refused."""
    shallow = _git(repo, "rev-parse", "--is-shallow-repository")
    if shallow.returncode == 0 and shallow.stdout.strip() == "true":
        msg = (
            f"the clone of {repo} is shallow, so the release it descends from may be missing from it, and its contract cannot be checked:"
            " fetch its whole history and its tags, as `git fetch --unshallow --tags` does, or check it out whole, as fetch-depth: 0 does"
        )
        raise ContractError(msg)
    options = _git(repo, "config", "--get-regexp", r"^remote\..*\.tagopt$")
    if remotes := [line.split()[0] for line in options.stdout.splitlines() if line.split()[-1:] == ["--no-tags"]]:
        remote = remotes[0].removeprefix("remote.").removesuffix(".tagopt")
        msg = (
            f"the clone of {repo} fetches no tags from {remote}, so the release it descends from may be missing from it, and its contract cannot be checked:"
            f" fetch them, and let it fetch them from now on, with `git config --unset remote.{remote}.tagOpt && git fetch --tags {remote}`"
        )
        raise ContractError(msg)
    found = _git(repo, "describe", "--tags", "--abbrev=0", "--match", releases, "HEAD")
    if found.returncode == 0:
        return found.stdout.strip()
    if any(said in found.stderr.lower() for said in _GIT_NONE):
        return None
    said = " ".join(found.stderr.split())
    msg = f"git cannot find the last release in {repo}: {said}: check the repo out as a clone, with its tags, as `git fetch --tags` fetches them"
    raise ContractError(msg)


def _at(repo: Path, tag: str, document: str) -> bool:
    """Whether `tag` holds `document`, relative to `repo`: a release made before the API had its document holds none."""
    listed = _git(repo, "ls-tree", "--name-only", tag, "--", document)
    if listed.returncode != 0:
        said = " ".join(listed.stderr.split())
        msg = f"git cannot read release {tag} in {repo}: {said}: check the clone's history and tags, as `git fetch --tags` fetches them"
        raise ContractError(msg)
    return bool(listed.stdout.strip())


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """`git ARGV...` in `repo`, its messages in English whatever the machine's locale, since a missing tag is known by
    what git says."""
    environment = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    try:
        return subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False, env=environment)  # noqa: S607 -- git from the PATH, as every basis command runs it
    except FileNotFoundError as error:
        msg = "git is not on the PATH, and the breaking-change check reads the last release through it: install it, as `basis doctor` says"
        raise ContractError(msg) from error
