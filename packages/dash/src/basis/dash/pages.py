"""A page of a Dash app, which `bd.App(pages=[...])` lists, and whose module's callbacks are the app's. basis builds a
page the same way in every app, from what it shows:

    page = bd.page(
        "/",
        "Overview",
        filters=[bd.as_of("as-of", calendar="sifma-us")],
        body=[
            bd.card("By sector", bd.graph("by-sector"), span=6),
            bd.card("By rating", bd.graph("by-rating"), span=6),
            bd.card("Positions", bd.grid("positions", Position, key="cusip"), flush=True),
        ],
    )

Its title, with the freshness line beside it, then the filter bar, then the body: cards on a grid of twelve columns,
each card as wide as its `span`, and every card the page's width once the page is narrower than a laptop's screen. A
page that needs a layout of its own gives `layout=` instead of a body, and keeps the shell around it."""

import inspect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from basis.dash.errors import DeclarationError
from basis.dash.filters import Filter, filter_of
from dash import html
from dash.development.base_component import Component

type Layout = Component | Callable[..., Component]
"""A page's layout: a component, or a function that makes one, which Dash calls with the URL's query parameters."""

_PATH = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")
COLUMNS: Final = 12
"""The columns of a page's body, which a card's `span` counts."""
FRESHNESS: Final = "basis-freshness"
"""The type of a page's freshness line's id, `{"type": FRESHNESS, "page": PATH, "datasets": NAMES}`, which the
shell's one callback writes from the status store: NAMES the datasets it reports, by commas, or empty for all."""


@dataclass(frozen=True)
class Page:
    path: str
    """Where the page is, under the app's prefix: `/` for the first, `/positions` for another."""
    title: str
    """The page's title, in the browser's tab, the navigation and the page's own heading."""
    layout: Layout
    module: str = field(default="__main__", compare=False)
    filters: tuple[Filter, ...] = field(default=(), compare=False)
    """The page's filters, whose values the URL keeps: the shell writes each back to the query string as it changes."""
    """The module that declared the page, whose callbacks are the page's: a page's module holds its layout and its
    callbacks."""


def page(
    path: str,
    title: str,
    *,
    body: Sequence[Component] | None = None,
    filters: Sequence[Filter | Component] = (),
    description: str | None = None,
    datasets: Sequence[object] = (),
    layout: Layout | None = None,
) -> Page:
    """A page at `path`, titled `title`, showing `body` under its `filters`, or `layout` where the page lays itself out,
    declared in the module that calls it. Its freshness line reports `datasets`, each a dataset or its name, or every
    dataset the page's callbacks loaded where it names none."""
    if not _PATH.match(path):
        msg = f"a page's path is / or /NAME, each part letters, digits, dots, dashes, underscores or tildes, with no slash at its end, not {path!r}: give one, as /positions"
        raise DeclarationError(msg)
    if not title.strip():
        msg = f"the page at {path} has no title: give it one, as it shows in the browser's tab"
        raise DeclarationError(msg)
    if body is not None and layout is not None:
        msg = f"the page at {path} gives both a body and a layout: give its body, a list of bd.card(...), or a layout of its own"
        raise DeclarationError(msg)
    if layout is not None and not (isinstance(layout, Component) or callable(layout)):
        msg = f"the page at {path}'s layout is a {type(layout).__qualname__}: give a Dash component, or a function that returns one"
        raise DeclarationError(msg)
    if layout is not None and (filters or description is not None or datasets):
        msg = f"the page at {path} has a layout of its own, so it takes no filters, description or datasets: put them in its layout, or give it a body"
        raise DeclarationError(msg)
    if layout is None:
        if body is None:
            msg = f"the page at {path} gives neither a body nor a layout: give its body, a list of bd.card(...), or a layout of its own"
            raise DeclarationError(msg)
        layout = _composed(path, title, list(body), list(filters), description=description, datasets=_names(path, datasets))
    frame = inspect.currentframe()
    caller = frame.f_back.f_globals.get("__name__") if frame is not None and frame.f_back is not None else None
    declared = tuple(found for found in map(filter_of, filters) if found is not None)
    return Page(path, title, layout, caller if isinstance(caller, str) else "__main__", declared)


def _names(path: str, datasets: Sequence[object]) -> str:
    """The datasets a page's freshness line reports, by name, joined by commas."""
    names = []
    for given in datasets:
        name = given if isinstance(given, str) else getattr(given, "name", None)
        if not isinstance(name, str) or not name or "," in name:
            msg = f"the page at {path} reports {given!r}: give a dataset, or its name"
            raise DeclarationError(msg)
        names.append(name)
    return ",".join(names)


def _composed(path: str, title: str, body: list[Component], filters: list[Filter | Component], *, description: str | None, datasets: str) -> Callable[..., Component]:
    for index, part in enumerate([*body, *filters]):
        if not isinstance(part, Component) and filter_of(part) is None:
            msg = f"the page at {path}'s body and filters are Dash components, not a {type(part).__qualname__} (item {index + 1}): give bd.card(...), a filter, or a component"
            raise DeclarationError(msg)

    def layout(**query: object) -> Component:
        head: list[Component] = [html.H1(title, className="basis-page-title")]
        if description is not None:
            head.append(html.P(description, className="basis-page-description"))
        head.append(html.Div(id={"type": FRESHNESS, "page": path, "datasets": datasets}, className="basis-freshness", role="status"))
        shown: list[Component] = [html.Div(head, className="basis-page-head")]
        if filters:
            parts: list[Component] = []
            for part in filters:
                made = filter_of(part)
                if made is not None:
                    parts.append(made.component(query))
                elif isinstance(part, Component):
                    parts.append(part)
            shown.append(html.Div(parts, className="basis-filters", role="search", **{"aria-label": "Filters"}))
        shown.append(html.Div(body, className="basis-body"))
        return html.Div(shown, className="basis-page")

    return layout


def card(title: str | None, *content: Component | str, span: int = COLUMNS, description: str | None = None, actions: Sequence[Component] = (), flush: bool = False) -> Component:
    """A card of a page's body: `title` over `content`, `span` of the body's twelve columns wide, with `actions`, such
    as an export, at the title's right. `flush` sets the content against the card's edges, as a grid sits."""
    if isinstance(span, bool) or not isinstance(span, int) or not 1 <= span <= COLUMNS:
        msg = f"a card spans 1 to {COLUMNS} columns, not {span!r}: give a whole number in that range"
        raise DeclarationError(msg)
    if title is not None and not title.strip():
        msg = "a card's title is text, or None for a card with none: give one"
        raise DeclarationError(msg)
    parts: list[Component] = []
    if title is not None or actions:
        heading: list[Component] = []
        if title is not None:
            heading.append(html.H2(title, className="basis-card-title"))
        if description is not None:
            heading.append(html.Span(description, className="basis-card-description"))
        if actions:
            heading.append(html.Div(list(actions), className="basis-card-actions"))
        parts.append(html.Div(heading, className="basis-card-head"))
    parts.append(html.Div(list(content), className="basis-card-body basis-card-flush" if flush else "basis-card-body"))
    style = {"gridColumn": f"span {span}"}
    if title is None:
        return html.Section(parts, className="basis-card", style=style)
    return html.Section(parts, className="basis-card", style=style, **{"aria-label": title})
