"""States, one look on every grid, graph and stat tile, which no page writes: loading, empty, failed and stale.

- **Loading** shows only once an answer has taken `DELAY` ms, so a cached one never flickers: a thin bar along the
  component's top, its content dimmed in place rather than hidden, so nothing moves (`dcc.Loading`, watching only the
  component's data props).
- **Empty** is the component's own: a grid's "No rows for this view", a figure's sentence (`bd.charts.empty`), a
  tile's dash.
- **Failed**: when a callback that answers a component fails, the app's `on_error` tells the person in the
  notifications, the component keeps what it showed, and its mark, "Not updated", says so on it until its next answer
  arrives. `on_error(handler)` wraps the app's handler to do it, from the failed callback's outputs.
- **Stale**: a component that names its dataset (`dataset=`) shows "Stale" while the status store marks that dataset
  stale, a last good value a failed reload left.

A component's marks have ids of their own, `{"type": FAILED, "of": ID, "prop": PROP}` and `{"type": STALE, "of": ID,
"dataset": NAME}`, which the shell's callbacks write in the browser."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

import dash
from basis.dash import internals
from dash import dcc, html
from dash.development.base_component import Component

FAILED: Final = "basis-failed"
"""The type of a component's failed mark's id."""
STALE: Final = "basis-stale"
"""The type of a component's stale mark's id."""
DELAY: Final = 400
"""How long an answer may take, in ms, before its component shows that it is loading."""
NOT_UPDATED: Final = "Not updated"
NOT_UPDATED_TITLE: Final = "Its last load failed: it shows what it showed before. The notification says why."
STALE_TEXT: Final = "Stale"
HIDDEN: Final = "basis-mark basis-mark-hidden"

_marked: dict[str, str] = {}
"""Each component with a failed mark, by id: the property its callbacks answer."""


def name_of(dataset: object) -> str | None:
    """A dataset's name, given it or its name."""
    if dataset is None:
        return None
    name = dataset if isinstance(dataset, str) else getattr(dataset, "name", None)
    if not isinstance(name, str) or not name:
        msg = f"{dataset!r} is no dataset: give one, or its name"
        raise TypeError(msg)
    return name


def marks(of: str, prop: str, dataset: object = None) -> list[Component]:
    """The marks of component `of`, whose callbacks answer `prop`: its failed mark, and its stale one where it names
    `dataset`."""
    _marked[of] = prop
    shown: list[Component] = [html.Span(NOT_UPDATED, id={"type": FAILED, "of": of, "prop": prop}, className=HIDDEN, title=NOT_UPDATED_TITLE)]
    if (name := name_of(dataset)) is not None:
        shown.insert(0, html.Span(STALE_TEXT, id={"type": STALE, "of": of, "dataset": name}, className=HIDDEN, title=f"{name} is stale: a reload failed, so this is its last good value."))
    return shown


def loading(child: Component, of: str, props: Sequence[str]) -> dcc.Loading:
    """`child`, which shows that it is loading once an answer to `of`'s `props` takes `DELAY` ms."""
    return dcc.Loading(
        child,
        delay_show=DELAY,
        delay_hide=0,
        show_initially=False,
        overlay_style={"visibility": "visible", "opacity": 0.55, "transition": "opacity 120ms"},
        custom_spinner=html.Div(className="basis-loading-bar", role="progressbar", **{"aria-label": "Loading"}),
        target_components={of: list(props)},
        parent_className="basis-loading",
    )


def on_error(handler: Callable[[Exception], None]) -> Callable[[Exception], None]:
    """`handler`, the app's `on_error`, and then each output of the failed callback that has a failed mark marked."""

    def handled(error: Exception) -> None:
        handler(error)
        if not internals.in_callback():
            return
        for component in _outputs():
            if (prop := _marked.get(component)) is not None:
                dash.set_props({"type": FAILED, "of": component, "prop": prop}, {"className": "basis-mark basis-mark-failed"})

    return handled


def found_marks(layout: object) -> Iterator[Mapping[str, str]]:
    """Each failed mark's id in `layout`, a component tree."""
    if isinstance(layout, Component):
        found = getattr(layout, "id", None)
        if isinstance(found, dict) and found.get("type") == FAILED:
            yield {str(key): str(value) for key, value in found.items()}
        children = getattr(layout, "children", None)
        yield from found_marks(children)
    elif isinstance(layout, list | tuple):
        for part in layout:
            yield from found_marks(part)


def _outputs() -> list[str]:
    """The ids of the components the running callback answers."""
    listed = dash.callback_context.outputs_list  # a callback of one output has one entry, of several a list of them
    parts = [listed] if isinstance(listed, dict) else list(listed)
    flat = [entry for part in parts for entry in (part if isinstance(part, list) else [part])]
    return [str(entry["id"]) for entry in flat if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
