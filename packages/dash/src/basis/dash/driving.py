"""An app driven as a browser drives it, in the process, with no browser and no socket: what `basis check` (`checks`) and
`basis render` (`render`) share. `driving(app)` builds the app on its fakes, or on an environment's settings, starts it
through `bd.testing`'s client, keeps every log record for the caller rather than writing it, and watches how long the
app's event loop is held. Its `Drive`:

- `page(PATH)` gives the page's components, inside basis's chrome, as Dash sends them, `{"type", "namespace", "props"}`;
- `first(PATH, nodes)` gives each component's properties by `ID.PROP`, as the layout gives them, and the location at
  the page's URL;
- `calls(PATH, nodes)` gives the callbacks a load of the page calls, those of the app's and its pages' modules, in the
  order the browser would: a server callback, called on load, with no pattern-matching id, its ids on the page;
- `call(entry, values)` calls one with those values, as the browser's first call does, no input marked changed, and
  keeps what it answered in `values`, so the callbacks after it read it."""

import logging
import time
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from http import HTTPStatus
from typing import Any, Final, override

import anyio
import orjson
from fastapi.testclient import TestClient

from basis.dash import internals
from basis.dash.app import App
from basis.dash.building import build
from basis.dash.settings import Settings
from basis.dash.testing import Client, client

type Json = Any
"""A value as orjson parses a layout or an answer, whose shape each function tests with `isinstance` before it relies
on it."""
type Node = dict[str, Json]
"""A component, as Dash sends it: `{"type", "namespace", "props"}`."""

ROUTER: Final = ".._pages_content"
"""The start of Dash Pages' router's output, which `page` stands in for."""
TICK_S: Final = 0.005


class PageError(RuntimeError):
    """A page that does not build; the message says why, as the app logged it."""


@dataclass
class Watch:
    """The longest the event loop has been held, in seconds, since `worst` was last set to 0."""

    worst: float = 0.0


@dataclass(frozen=True)
class Call:
    """What one callback answered."""

    output: str
    """Its output, as `_dash-dependencies` writes it."""
    status: int
    content: bytes
    response: Mapping[str, Mapping[str, Json]]
    """Its outputs, by component and property."""
    side: Mapping[str, Mapping[str, Json]]
    """What it set with `set_props`, by component and property: the notifications and the status store among them."""
    failures: tuple[str, ...]
    """Each failure the app's `on_error` logged, as its record says it."""
    held_ms: float
    """The longest it held the event loop."""

    @property
    def failed(self) -> bool:
        return bool(self.failures) or self.status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}


@dataclass
class Drive:
    app: App
    client: Client
    records: list[logging.LogRecord]
    watch: Watch = field(default_factory=Watch)

    @cached_property
    def chrome(self) -> Node:
        return self.client.layout()

    @cached_property
    def dependencies(self) -> list[dict[str, Json]]:
        """Each callback the app sends the page, but Dash Pages' router, which `page` stands in for."""
        return [entry for entry in self.client.dependencies() if not str(entry.get("output", "")).startswith(ROUTER)]

    @cached_property
    def functions(self) -> dict[str, object]:
        """Each callback's output, with its function as its module wrote it, or None for a clientside one."""
        found: dict[str, object] = {}
        for output, entry in self.client.built.dash.callback_map.items():
            function = entry.get("callback")
            found[str(output)] = getattr(function, "__wrapped__", function)
        return found

    def module(self, output: str) -> str | None:
        """The module of the callback whose output is `output`, or None for a clientside one."""
        module = getattr(self.functions.get(output), "__module__", None)
        return module if isinstance(module, str) else None

    def page(self, path: str) -> list[Node]:
        """The components of the page at `path`, inside basis's chrome, outermost first."""
        del self.records[:]
        try:
            content = self.client.page(path)
        except (AssertionError, TypeError, KeyError, StopIteration) as error:
            said = next((record.getMessage() for record in self.records if record.levelno >= logging.ERROR), f"{type(error).__name__}: {' '.join(str(error).split())}")
            raise PageError(said) from None
        return list(nodes(replaced(self.chrome, content)))

    def first(self, path: str, components: Sequence[Node]) -> dict[str, Json]:
        """Each property of each component with an id of its own, by `ID.PROP`, as the layout gives it, and the
        location at the page's URL."""
        values: dict[str, Json] = {}
        for node in components:
            props = node.get("props") or {}
            if isinstance(component := props.get("id"), str):
                values |= {f"{component}.{prop}": value for prop, value in props.items()}
        pathname = f"{str(self.client.built.dash.config.requests_pathname_prefix).rstrip('/')}{path}"
        return values | {"_pages_location.pathname": pathname, "_pages_location.search": "", "_pages_location.hash": "", "_pages_location.href": f"http://testserver{pathname}"}

    def calls(self, path: str, components: Sequence[Node], *, skip: frozenset[int] = frozenset()) -> list[dict[str, Json]]:
        """The callbacks a load of the page at `path` calls, in the browser's order, but those whose functions' ids
        `skip` holds."""
        page_modules = {page.module for page in self.app.pages if page.path == path}
        on = {str(props["id"]) for node in components if isinstance((props := node.get("props") or {}).get("id"), str)}
        found = [
            entry
            for entry in self.dependencies
            if self.module(str(entry["output"])) in {self.app.module, *page_modules} and id(self.functions.get(str(entry["output"]))) not in skip and called_on_load(entry, on)
        ]
        return ordered(found)

    def call(self, entry: Mapping[str, Json], values: dict[str, Json], *, again_past_ms: float | None = None) -> Call:
        """Call the callback `entry` with `values`, keep what it answers in them, and give what it answered; made once
        more where it held the loop past `again_past_ms`, as Python's first import of a module does."""
        output = str(entry["output"])
        inputs = [{"id": one["id"], "property": one["property"], "value": values.get(f"{one['id']}.{one['property']}")} for one in entry.get("inputs") or []]
        state = [{"id": one["id"], "property": one["property"], "value": values.get(f"{one['id']}.{one['property']}")} for one in entry.get("state") or []]
        body = internals.request(output, inputs, state=state, changed=[])
        for attempt in range(2):
            del self.records[:]
            self.watch.worst = 0.0
            answered = self.client.http.post(f"{self.client.base}{internals.UPDATE}", json=body)
            if (portal := self.client.http.portal) is not None:
                portal.call(anyio.sleep, 4 * TICK_S)  # the watch's last tick
            held_ms = self.watch.worst * 1000
            if again_past_ms is None or held_ms <= again_past_ms or attempt:
                break
        failures = tuple(record.getMessage() for record in self.records if record.name == "basis.dash.errors" and record.levelno >= logging.ERROR)
        decoded: Json = orjson.loads(answered.content) if answered.status_code == HTTPStatus.OK and answered.content else {}
        response = _props(decoded.get("response")) if isinstance(decoded, dict) else {}
        side = _props(decoded.get("sideUpdate")) if isinstance(decoded, dict) else {}
        for component, props in response.items():
            values |= {f"{component}.{prop}": value for prop, value in props.items()}
        return Call(output, answered.status_code, answered.content, response, side, failures, held_ms)


@contextmanager
def driving(app: App, *, settings: Settings | None = None, environment: str | None = None, fakes: Mapping[Any, object] | None = None) -> Generator[Drive]:
    """`app`, built and started, driven: on its fakes, or with `settings`, an environment's, named `environment`, on
    that environment's resources, or `fakes`, a test's. Every log record is kept on the `Drive`, not written."""
    with _captured() as records:
        if settings is None:
            with client(app, fakes=fakes) as opened:
                yield from _watched(app, opened, records)
        else:
            built = build(app, settings=settings, environment=environment, fakes=fakes)
            with TestClient(built.asgi) as http:
                yield from _watched(app, Client(built, http), records)


def _watched(app: App, opened: Client, records: list[logging.LogRecord]) -> Iterator[Drive]:
    drive = Drive(app, opened, records)
    portal = opened.http.portal
    watching = portal.start_task_soon(_watching, drive.watch) if portal is not None else None
    try:
        yield drive
    finally:
        if watching is not None:
            watching.cancel()  # else the client's portal, closing, waits for it


async def _watching(watch: Watch) -> None:
    """Keep `watch.worst` the longest the loop was held, from how late each short sleep wakes."""
    while True:
        before = time.perf_counter()
        await anyio.sleep(TICK_S)
        watch.worst = max(watch.worst, time.perf_counter() - before - TICK_S)


@contextmanager
def _captured() -> Generator[list[logging.LogRecord]]:
    """Every log record meanwhile, kept rather than written: one per request would be hundreds of lines."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        @override
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    kept: list[logging.Handler] = [Keep()]
    root.handlers, root.level = kept, logging.INFO
    try:
        yield records
    finally:
        root.handlers, root.level = handlers, level


def _props(found: Json) -> dict[str, dict[str, Json]]:
    if not isinstance(found, dict):
        return {}
    return {str(component): {str(prop): value for prop, value in props.items()} for component, props in found.items() if isinstance(props, dict)}


def nodes(tree: Json) -> Iterator[Node]:
    """Each component of a layout as Dash sends it, at any depth, outermost first."""
    if isinstance(tree, dict):
        if "type" in tree and "namespace" in tree and isinstance(tree.get("props"), dict):
            yield tree
            for value in tree["props"].values():
                yield from nodes(value)
        else:
            for value in tree.values():
                yield from nodes(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from nodes(value)


def replaced(chrome: Node, content: Json) -> Node:
    """The chrome, with the page's content where Dash Pages puts it."""

    def walk(tree: Json) -> Json:
        if isinstance(tree, dict):
            props = tree.get("props")
            if isinstance(props, dict) and props.get("id") == "_pages_content":
                return {**tree, "props": {**props, "children": content}}
            return {key: walk(value) for key, value in tree.items()}
        if isinstance(tree, list):
            return [walk(value) for value in tree]
        return tree

    found = walk(chrome)
    return found if isinstance(found, dict) else chrome


def outputs(output: str) -> list[dict[str, str]]:
    """Each output of a callback whose output `_dash-dependencies` writes as `output`, by `id` and `property`."""
    ones = output[2:-2].split("...") if output.startswith("..") and output.endswith("..") else [output]
    found: list[dict[str, str]] = []
    for one in ones:
        component, _, prop = one.rpartition(".")
        found.append({"id": component, "property": prop.partition("@")[0]})
    return found


def short(output: str) -> str:
    """A callback's output, as a person reads it: its first output's id and property, and how many more."""
    given = outputs(output)
    first = given[0] if given else {"id": output, "property": ""}
    more = f" (and {len(given) - 1} more)" if len(given) > 1 else ""
    return f"{first['id']}.{first['property']}{more}"


def called_on_load(entry: Mapping[str, Json], on: set[str]) -> bool:
    """Whether a page's load calls the callback with values its layout gives: a server callback, called on load, with
    no pattern-matching id, whose ids, `on`, are on the page."""
    if entry.get("clientside_function") or entry.get("prevent_initial_call"):
        return False
    given = [*outputs(str(entry["output"])), *(entry.get("inputs") or []), *(entry.get("state") or [])]
    if any(str(one["id"]).startswith("{") for one in given):
        return False
    return all(str(one["id"]) in on or str(one["id"]).startswith("_pages_") for one in given)


def ordered(entries: list[dict[str, Json]]) -> list[dict[str, Json]]:
    """`entries` in the order the browser calls them: one whose input another answers after that one."""
    produced = {f"{one['id']}.{one['property']}": index for index, entry in enumerate(entries) for one in outputs(str(entry["output"]))}
    waits = {index: {produced[key] for one in entry.get("inputs") or [] if (key := f"{one['id']}.{one['property']}") in produced and produced[key] != index} for index, entry in enumerate(entries)}
    done: list[int] = []
    while len(done) < len(entries):
        ready = [index for index in range(len(entries)) if index not in done and waits[index] <= set(done)]
        done.extend(ready or [next(index for index in range(len(entries)) if index not in done)])  # a cycle: in the order declared
    return [entries[index] for index in done]
