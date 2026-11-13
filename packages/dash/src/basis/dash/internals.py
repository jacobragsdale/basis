"""Every name of Dash's that basis touches and Dash does not document, in one module, so an upgrade of Dash that moves
one fails `tests/test_internals.py`, and nowhere else: spike 14.1, question 8. Nothing else in basis imports them.

- `choose_react`: React 19, through `dash._dash_renderer._set_react_version`, which Dash reads each time it renders
  the index, so the choice holds for every app in the process.
- `own_callbacks`: what `dash.callback` registers, which Dash keeps in `dash._callback.GLOBAL_CALLBACK_MAP` and
  `GLOBAL_CALLBACK_LIST` until the first app's `_setup_server` takes them all. basis keeps every one it has seen, and
  gives each app built the callbacks of its own modules, its declaration's and its pages', so a second app built in one process, as tests build them,
  still has its callbacks, and an app never has another's.
- `set_up`: `Dash._setup_server`, which Dash otherwise runs on the first request, and which raises the errors of a
  callback's setup, so building the app raises them instead.
- `forget_pages`: `dash._pages.PAGE_REGISTRY`, Dash Pages' registry, which is the process's; building an app makes it
  hold that app's pages alone.
- The wire format of `POST _dash-update-component`, which `basis.dash.testing` drives: `request` and `UPDATE`,
  `DEPENDENCIES` and `LAYOUT`.
- `FASTAPI_CONFIG`: the variable from which Dash's FastAPI backend turns its dev tools on, on its first request, with
  no argument to stop it, which `basis serve` refuses."""

from collections.abc import Callable, Iterable, Mapping
from typing import Final

import dash
from dash import _callback, _dash_renderer, _pages

REACT: Final = "19.2.4"
"""The React Dash 4.5 opts into, which Mantine 9 needs."""
FASTAPI_CONFIG: Final = "_DASH_FASTAPI_CONFIG"
"""`dash.backends._fastapi._ENV_CONFIG`, written out, since importing that module loads uvicorn."""
UPDATE: Final = "_dash-update-component"
DEPENDENCIES: Final = "_dash-dependencies"
LAYOUT: Final = "_dash-layout"

_seen: dict[str, dict[str, object]] = {}
_seen_list: list[dict[str, object]] = []


def choose_react() -> None:
    """React 19 for every app in the process, a test's among them."""
    _dash_renderer._set_react_version(REACT, REACT)  # noqa: SLF001 -- Dash's opt-in has no public name


def own_callbacks(app: dash.Dash, modules: Iterable[str]) -> None:
    """Give `app` every callback `dash.callback` registered from one of `modules`, and no other."""
    global_map, global_list = _callback.GLOBAL_CALLBACK_MAP, _callback.GLOBAL_CALLBACK_LIST
    _seen.update(global_map)
    known = {id(entry) for entry in _seen_list}
    _seen_list.extend(entry for entry in global_list if id(entry) not in known)
    global_map.clear()
    global_list.clear()
    owned = frozenset(modules)
    for key, entry in _seen.items():
        if _module(entry) in owned:
            app.callback_map[key] = entry
    outputs = set(app.callback_map)
    app._callback_list.extend(entry for entry in _seen_list if entry.get("output") in outputs)  # noqa: SLF001 -- Dash's list of the callbacks it sends the page


def _module(entry: Mapping[str, object]) -> str | None:
    function = entry.get("callback")
    module = getattr(getattr(function, "__wrapped__", function), "__module__", None)
    return module if isinstance(module, str) else None


def server_of(made: object) -> Callable[[], object]:
    """A FastAPI server, as `Dash(server=...)` takes it, which Dash's annotation, `bool | Callable[[], Any]`, leaves out,
    though its backends take an instance."""
    return made  # ty: ignore[invalid-return-type] -- the instance Dash's FastAPI backend takes, which its annotation omits


def set_up(app: dash.Dash) -> None:
    """Set `app`'s server up now, as Dash would on its first request."""
    app._setup_server()  # noqa: SLF001 -- where Dash raises a callback's setup errors


def forget_pages() -> None:
    """Empty Dash Pages' registry, the process's, for the app being built to register its own."""
    _pages.PAGE_REGISTRY.clear()


def request(output: str, inputs: Iterable[Mapping[str, object]], *, state: Iterable[Mapping[str, object]] = (), changed: Iterable[str] = ()) -> dict[str, object]:
    """The body of `POST _dash-update-component` that fires the callback whose output is `output`, as
    `_dash-dependencies` writes it (`id.prop`, or `..a.b...c.d..` for several), with each input and state as
    `{"id", "property", "value"}`, and `changed` the inputs that changed, as `id.prop`."""
    return {"output": output, "outputs": _outputs(output), "inputs": list(inputs), "state": list(state), "changedPropIds": list(changed)}


def _outputs(output: str) -> object:
    if output.startswith("..") and output.endswith(".."):
        return [_output(one) for one in output[2:-2].split("...")]
    return _output(output)


def _output(one: str) -> dict[str, str]:
    component, _, prop = one.rpartition(".")
    return {"id": component, "property": prop}
