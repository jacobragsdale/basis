"""An app built in the process and driven through FastAPI's `TestClient`, with no browser and no socket, as every test
of an app and of basis-dash does:

    with bd.testing.client(app) as client:  # runs the lifespan, as `basis serve` does
        assert client.http.get("/health/ready").status_code == 200
        shown = client.page("/")  # the page's layout, as Dash Pages answers it for that path
        answered = client.fire("total.children", {"as-of.value": "2026-11-13"})  # a callback, through Dash's own route

`client` builds the app with `Settings.given(...)`, so the developer's environment never changes what a test checks.
It runs on the fakes the app declares (`basis.dash.fixtures`), or a test's own; phase 17 builds `basis check`'s and
`basis render`'s drives on this one."""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from basis.dash import fixtures, internals
from basis.dash.app import App
from basis.dash.building import Built, build
from basis.dash.settings import Settings

ROUTER = "_pages_content.children"
"""What Dash Pages' router callback outputs, among its outputs: the page the URL names."""


@dataclass(frozen=True)
class Client:
    built: Built
    http: TestClient
    """FastAPI's test client, for any request; its paths are the server's, under the prefix where the proxy keeps it."""

    @property
    def base(self) -> str:
        """The path Dash's own routes are under on the server: `/`, or the prefix where the proxy keeps it."""
        return str(self.built.dash.config.routes_pathname_prefix)

    def index(self) -> str:
        """The index page's HTML, as a browser first gets it."""
        return self._ok(self.http.get(self.base)).text

    def layout(self) -> dict[str, object]:
        """The layout every page shows inside, as Dash sends it: basis's chrome, with Dash Pages' container."""
        found: object = self._ok(self.http.get(f"{self.base}{internals.LAYOUT}")).json()
        return _object(found, "the layout")

    def dependencies(self) -> list[dict[str, object]]:
        """Each callback the page has, as Dash sends it: its `output`, `inputs`, `state` and the rest."""
        found: object = self._ok(self.http.get(f"{self.base}{internals.DEPENDENCIES}")).json()
        if not isinstance(found, list):
            msg = f"Dash's dependencies are a list, not {type(found).__qualname__}"
            raise TypeError(msg)
        return [_object(entry, "a dependency") for entry in found]

    def fire(self, output: str, inputs: Mapping[str, object], *, state: Mapping[str, object] | None = None, changed: list[str] | None = None) -> dict[str, dict[str, object]]:
        """Fire the callback whose output is `output`, as `_dash-dependencies` writes it, with each input's and
        state's value by `id.prop`, and `changed` the inputs that changed, by default all of them; what it answers, by
        component and property, its outputs and what it set with `set_props` alike, or nothing where it prevents the
        update."""
        body = internals.request(
            output, [_value(key, value) for key, value in inputs.items()], state=[_value(key, value) for key, value in (state or {}).items()], changed=list(inputs) if changed is None else changed
        )
        answered = self.http.post(f"{self.base}{internals.UPDATE}", json=body)
        if answered.status_code == HTTPStatus.NO_CONTENT:
            return {}
        found = _object(self._ok(answered).json(), "a callback's answer")
        answered_props: dict[str, dict[str, object]] = {}
        for part in ("response", "sideUpdate"):
            for component, props in _object(found.get(part, {}), f"a callback's {part}").items():
                answered_props.setdefault(component, {}).update(_object(props, "a component's props"))
        return answered_props

    def page(self, path: str) -> dict[str, object]:
        """The layout of the page at `path`, under the app's prefix, as Dash Pages' router answers it for a browser
        at that URL."""
        router = next(entry for entry in self.dependencies() if ROUTER in str(entry.get("output")))
        pathname = f"{str(self.built.dash.config.requests_pathname_prefix).rstrip('/')}{path}"
        values = {"pathname": pathname, "search": "", "hash": "", "href": f"http://testserver{pathname}"}
        inputs = {_key(given): values.get(str(_object(given, "an input")["property"])) for given in _list(router.get("inputs"))}
        state = dict.fromkeys((_key(given) for given in _list(router.get("state"))), None)
        answered = self.fire(str(router["output"]), inputs, state=state)
        content = answered.get(ROUTER.partition(".")[0], {})
        return _object(content.get("children"), "the page's layout")

    @staticmethod
    def _ok(response: Response) -> Response:
        if response.status_code != HTTPStatus.OK:
            msg = f"the app answered {response.status_code}: {response.text[:500]}"
            raise AssertionError(msg)
        return response


@contextmanager
def client(app: App, *, settings: Settings | None = None, fakes: Mapping[Any, object] | None = None) -> Generator[Client]:
    """`app`, built with `settings`, or its settings' defaults alone, on `fakes`, by resource or its name, or the fakes
    the app declares (`basis.dash.fixtures.of`), and started, as a `Client`."""
    if fakes is None and app.fakes is not None:
        fakes = dict(fixtures.of(app))
    built = build(app, settings=settings if settings is not None else app.settings.given(), fakes=fakes)
    with TestClient(built.asgi) as http:
        yield Client(built, http)


def _value(key: str, value: object) -> dict[str, object]:
    component, _, prop = key.rpartition(".")
    return {"id": component, "property": prop, "value": value}


def _key(given: object) -> str:
    entry = _object(given, "an input or a state")
    return f"{entry['id']}.{entry['property']}"


def _object(found: object, what: str) -> dict[str, object]:
    if not isinstance(found, dict):
        msg = f"{what} is an object, not {type(found).__qualname__}: {found!r}"[:500]
        raise TypeError(msg)
    return {str(key): value for key, value in found.items()}


def _list(found: object) -> list[object]:
    return list(found) if isinstance(found, list) else []
