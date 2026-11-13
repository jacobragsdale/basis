"""Every name of Dash's that basis touches and Dash does not document, in one module, so an upgrade of Dash that moves
one fails `tests/test_internals.py`, and nowhere else: spike 14.1, question 8. Nothing else in basis imports them.

- `choose_react`: React 19, through `dash._dash_renderer._set_react_version`, which Dash reads each time it renders
  the index, so the choice holds for every app in the process.
- `own_callbacks` and `functions_of`: what `dash.callback` registers, which Dash keeps in `dash._callback.GLOBAL_CALLBACK_MAP` and
  `GLOBAL_CALLBACK_LIST` until the first app's `_setup_server` takes them all. basis keeps every one it has seen, and
  gives each app built the callbacks of its own modules, its declaration's and its pages', so a second app built in one process, as tests build them,
  still has its callbacks, and an app never has another's.
- `set_up`: `Dash._setup_server`, which Dash otherwise runs on the first request, and which raises the errors of a
  callback's setup, so building the app raises them instead.
- `forget_pages`: `dash._pages.PAGE_REGISTRY`, Dash Pages' registry, which is the process's; building an app makes it
  hold that app's pages alone.
- The wire format of `POST _dash-update-component`, which `basis.dash.testing` drives: `request` and `UPDATE`,
  `DEPENDENCIES` and `LAYOUT`.
- `in_callback` and `updated`: Dash's callback context, `dash._callback_context.context_value`, which holds what the
  callback running sets with `set_props` (`updated_props`), so a load adds its dataset to the status store's `Patch`
  rather than replace another load's.
- `FASTAPI_CONFIG`: the variable from which Dash's FastAPI backend turns its dev tools on, on its first request, with
  no argument to stop it, which `basis serve` refuses.
- `write_answers_fast` and `answer_json`: Dash writes a callback's answer with `dash._callback.to_json`, Plotly's
  `to_json_plotly`, which tries orjson and, where orjson refuses one value, a `Patch` among them, walks every value
  in Python first: 160 ms of the 228 a 10,000-row grid's answer held the loop (step 17.6). basis's writer is orjson
  with each object's `to_plotly_json` as its default and Plotly's own escapes, the same bytes, and Plotly's writer
  for anything orjson still refuses."""

from collections.abc import Callable, Iterable, Mapping
from typing import Final

import orjson
from plotly.io import _json as plotly_json

import dash
from dash import _callback, _callback_context, _dash_renderer, _pages, _utils

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
    for entry in global_list:
        if id(entry) not in known:  # a module imported again, as a check imports a repo's, registers its callbacks again
            _seen_list[:] = [seen for seen in _seen_list if seen.get("output") != entry.get("output")]
            _seen_list.append(entry)
    global_map.clear()
    global_list.clear()
    owned = frozenset(modules)
    for key, entry in _seen.items():
        if _module(entry) in owned:
            app.callback_map[key] = entry
    outputs = set(app.callback_map)
    app._callback_list.extend(entry for entry in _seen_list if entry.get("output") in outputs)  # noqa: SLF001 -- Dash's list of the callbacks it sends the page


def functions_of(modules: Iterable[str]) -> list[Callable[..., object]]:
    """The function of every server callback `dash.callback` registered from one of `modules`, as the module wrote it,
    whether or not an app has been built since."""
    owned = frozenset(modules)
    entries = {**_seen, **_callback.GLOBAL_CALLBACK_MAP}
    found: list[Callable[..., object]] = []
    for entry in entries.values():
        function = entry.get("callback")
        function = getattr(function, "__wrapped__", function)
        if callable(function) and _module(entry) in owned:
            found.append(function)
    return found


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


def in_callback() -> bool:
    """Whether a callback is running in this context."""
    return bool(_callback_context.context_value.get({}))


def updated(component: str, prop: str) -> object:
    """What the running callback has set `component`'s `prop` to with `set_props`, or None."""
    found = getattr(_callback_context.context_value.get({}), "updated_props", None)
    return found.get(component, {}).get(prop) if isinstance(found, dict) else None


_OPTIONS: Final = orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY
"""orjson's options as Plotly's writer gives them."""


def _plotly_default(found: object) -> object:
    """What orjson writes of a value it has no type for: its Plotly JSON, as a `Patch`'s, a component's or a figure's."""
    made = getattr(found, "to_plotly_json", None)
    if made is None:
        msg = f"{type(found).__qualname__} is no value orjson writes"
        raise TypeError(msg)
    return made()


def answer_json(value: object) -> str:
    """`value`, a callback's answer, as Dash's writer writes it, by orjson in one pass."""
    try:
        written = orjson.dumps(value, default=_plotly_default, option=_OPTIONS).decode("utf8")
    except TypeError:
        return str(_utils.to_json(value))  # Plotly's writer, which cleans what orjson refuses
    return str(plotly_json._safe(written, plotly_json._swap_orjson))  # noqa: SLF001 -- Plotly's escapes for JSON in a page


def write_answers_fast() -> None:
    """Write every callback's answer with `answer_json`, for every app in the process."""
    _callback.to_json = answer_json  # ty: ignore[invalid-assignment] -- Dash's writer, whose signature its annotation leaves bare
