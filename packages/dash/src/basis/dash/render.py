"""The agent's eyes: what a page shows, and what a dataset holds, as JSON, with no browser, on the app's fakes unless an
environment is named.

`basis render PAGE [--set ID.PROP=VALUE]...` builds the app, gives the page's layout, with each `--set` value in place
of the layout's, and calls every callback a load of the page calls, in the browser's order, as `basis check` does
(`basis.dash.driving`). It prints each output as a person would see it:

| Output | Summary |
|---|---|
| A grid's `rowData` | `{"kind": "grid", "rows": N, "columns": [HEADER], "first": [{HEADER: TEXT}]}`: its first rows as the grid shows them, each number as `bd.text` writes it |
| A grid's totals, a `Patch` of its `dashGridOptions` | `{"kind": "totals", "row": {HEADER: TEXT}}` |
| A graph's `figure` | `{"kind": "figure", "title": TEXT, "traces": [{"type", "name", "points"}], "axes": {"xaxis": {"title", "type"}}}` |
| Components, or a text | `{"kind": "text", "text": TEXT}`: every text the components hold, in order |
| Anything else | `{"kind": "value", "value": VALUE}` |

and with them each dataset the page loaded, from its `loaded DATASET` record, the notifications a callback sent, and
each callback that failed, with why. A callback that failed makes the command fail too, after the JSON.

`basis load DATASET [--param NAME=VALUE]...` loads one dataset of the app's, twice, and prints its rows, its columns,
its first rows as text, and each load's milliseconds and how the cache answered, so the second shows a hit."""

import json
import logging
import time
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import pandas as pd

from basis.core.errors import BasisError
from basis.core.rows import Row
from basis.dash import declarations, ids
from basis.dash import format as formats
from basis.dash.app import App
from basis.dash.driving import Drive, Json, Node, driving, short
from basis.dash.grids import Grid
from basis.dash.loads import load as load_dataset
from basis.dash.pages import Layout, Page
from basis.dash.settings import Settings
from basis.dash.texts import text
from dash.development.base_component import Component

FIRST: Final = 10
"""How many rows a grid's or a dataset's summary shows."""


class RenderError(BasisError, LookupError):
    """A page, a dataset or a value the command cannot find or read; the message ends with the fix."""


@dataclass(frozen=True)
class Rendered:
    shown: dict[str, Json]
    """What `basis render` prints."""
    failed: tuple[tuple[str, str], ...]
    """Each callback that failed, by its output, and why."""


def render(app: App, page: str, sets: Mapping[str, Json], *, settings: Settings | None = None, environment: str | None = None) -> Rendered:
    """The page `page`, by its path or its title, with `sets` by `ID.PROP` in place of its layout's values, as the
    module's docstring says, on the app's fakes, or with `settings`, `environment`'s."""
    found = _page(app, page)
    grids = _grids(found.layout)
    with driving(app, settings=settings, environment=environment) as drive:
        components = drive.page(found.path)
        kinds = {str(props["id"]): node for node in components if isinstance((props := node.get("props") or {}).get("id"), str)}
        if unknown := sorted(key for key in sets if key.rpartition(".")[0] not in kinds):
            listed = ", ".join(sorted(kinds)[:20])
            msg = f"page {found.path} has no component {unknown[0].rpartition('.')[0]!r}: --set ID.PROP=VALUE names a component of the page's, as {listed}"
            raise RenderError(msg)
        values = drive.first(found.path, components) | dict(sets)
        outputs: dict[str, Json] = {}
        loads: list[dict[str, Json]] = []
        notifications: list[object] = []
        failed: list[tuple[str, str]] = []
        for entry in drive.calls(found.path, components):
            called = drive.call(entry, values)
            loads.extend(_loads(drive))
            notifications.extend(_notifications(called.side))
            if called.failed:
                failed.append((short(called.output), called.failures[0] if called.failures else f"the app answered {called.status}"))
                continue
            for component, props in called.response.items():
                for prop, value in props.items():
                    outputs[f"{component}.{prop}"] = summary(kinds.get(component), prop, value, grids.get(component))
    shown: dict[str, Json] = {"app": app.name, "page": found.path, "title": found.title, "env": environment, "outputs": outputs, "loads": loads, "notifications": notifications}
    shown["failed"] = [{"callback": output, "error": why} for output, why in failed]
    return Rendered(shown, tuple(failed))


def load(app: App, name: str, given: Mapping[str, str], *, settings: Settings | None = None, environment: str | None = None) -> dict[str, Json]:
    """Dataset `name` of `app`'s, with `given` params as text, loaded twice, as the module's docstring says."""
    datasets = {dataset.name: dataset for dataset in declarations.datasets(app.resources)}
    if (dataset := datasets.get(name)) is None:
        msg = f"app {app.name} has no dataset {name!r}: name one of {', '.join(sorted(datasets)) or 'none'}"
        raise RenderError(msg)
    params = {param: _typed(name, param, dataset.params.get(param), text) for param, text in given.items()}
    with driving(app, settings=settings, environment=environment) as drive:
        portal = drive.client.http.portal
        if portal is None:  # the client is open, so its portal is
            msg = "the app's test client has no portal: run it again"
            raise RenderError(msg)
        timings: list[dict[str, Json]] = []
        frame = pd.DataFrame()
        for _ in range(2):
            del drive.records[:]
            started = time.perf_counter()
            frame = portal.call(_load, dataset, params)
            ms = round((time.perf_counter() - started) * 1000, 3)
            cache = next((getattr(record, "cache", None) for record in drive.records if record.getMessage().startswith("loaded ")), None)
            timings.append({"ms": ms, "cache": cache})
    return {
        "app": app.name,
        "dataset": name,
        "kind": dataset.kind,
        "env": environment,
        "params": {param: value.isoformat() if isinstance(value, date) else value for param, value in params.items()},
        "rows": len(frame),
        "columns": list(frame.columns),
        "first": _texts(frame.head(FIRST), dataset.row),
        "loads": timings,
    }


async def _load(dataset: declarations.Dataset, params: Mapping[str, Hashable]) -> pd.DataFrame:
    return await load_dataset(dataset, **params)


def summary(node: Node | None, prop: str, value: Json, grid: Grid | None = None) -> dict[str, Json]:
    """What a person sees of `value`, given to `node`'s `prop`."""
    kind = f"{node.get('namespace')}.{node.get('type')}" if node is not None else None
    if _patch(value):
        assigned = {".".join(str(part) for part in operation.get("location") or []): (operation.get("params") or {}).get("value") for operation in value.get("operations") or []}
        if node is not None and kind == "dash_ag_grid.AgGrid" and isinstance(rows := assigned.get("pinnedBottomRowData"), list) and rows:
            return {"kind": "totals", "row": _shown(node, rows[:1], grid)[0]}
        return {"kind": "patch", "set": assigned}
    if node is not None and kind == "dash_ag_grid.AgGrid" and prop == "rowData" and isinstance(value, list):
        return {"kind": "grid", "rows": len(value), "columns": [header for _, header in _columns(node)], "first": _shown(node, value[:FIRST], grid)}
    if prop == "figure" and isinstance(value, dict):
        return _figure(value)
    if prop == "children" or isinstance(value, str | int | float):
        return {"kind": "text", "text": " ".join(_strings(value))}
    return {"kind": "value", "value": value}


def _page(app: App, given: str) -> Page:
    for page in app.pages:
        if given in {page.path, page.path.lstrip("/")} or given.casefold() == page.title.casefold():
            return page
    listed = ", ".join(f"{page.path} ({page.title})" for page in app.pages)
    msg = f"app {app.name} has no page {given!r}: name one by its path or title, of {listed}"
    raise RenderError(msg)


def _grids(layout: Layout) -> dict[str, Grid]:
    """Each `bd.grid` of a layout, by its table's id, with what it was declared with."""
    made = layout if isinstance(layout, Component) else layout()
    found: dict[str, Grid] = {}
    for component in [made, *made._traverse()]:  # noqa: SLF001 -- Dash's walk of a layout's components
        if isinstance(component, Grid):
            for inner in component._traverse():  # noqa: SLF001
                if getattr(inner, "_type", None) == "AgGrid" and isinstance(identity := getattr(inner, "id", None), str):
                    found[identity] = component
    return found


def _columns(node: Node) -> list[tuple[str, str]]:
    """Each column a grid shows, its hidden ones left out, by its field and its header."""
    defined = (node.get("props") or {}).get("columnDefs") or []
    return [(str(column["field"]), str(column.get("headerName") or column["field"])) for column in defined if isinstance(column, dict) and "field" in column and not column.get("hide")]


def _shown(node: Node, rows: Sequence[Json], grid: Grid | None) -> list[dict[str, Json]]:
    """`rows` as the grid shows them: by header, each number through its column's display format where the grid is
    basis's."""
    displays = {column.name: column.display or (formats.number() if column.kind == "float" else None) for column in grid.declared.row.columns} if grid is not None else {}
    shown: list[dict[str, Json]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        texts: dict[str, Json] = {}
        for field, header in _columns(node):
            value = row.get(field)
            texts[header] = text(value, displays[field]) if field in displays else value
        shown.append(texts)
    return shown


def _texts(frame: pd.DataFrame, row: type[Row]) -> list[dict[str, str]]:
    displays = {column.name: column.display for column in row.columns}
    return [{str(name): text(value, displays.get(str(name))) for name, value in record.items()} for record in frame.to_dict("records")]


def _figure(figure: Mapping[str, Json]) -> dict[str, Json]:
    given = figure.get("layout")
    layout: dict[str, Json] = given if isinstance(given, dict) else {}
    traces: list[dict[str, Json]] = []
    for trace in figure.get("data") or []:
        if not isinstance(trace, dict):
            continue
        points = next((len(trace[axis]) for axis in ("x", "y", "values", "z", "labels") if isinstance(trace.get(axis), list)), 0)
        traces.append({"type": trace.get("type", "scatter"), "name": trace.get("name"), "points": points})
    axes = {name: {"title": _title(axis.get("title")), "type": axis.get("type")} for name, axis in sorted(layout.items()) if name.startswith(("xaxis", "yaxis")) and isinstance(axis, dict)}
    return {"kind": "figure", "title": _title(layout.get("title")), "traces": traces, "axes": axes}


def _title(given: Json) -> str | None:
    if isinstance(given, dict):
        given = given.get("text")
    return given if isinstance(given, str) else None


def _strings(value: Json) -> list[str]:
    """Every text `value` holds, components' children among them, in order."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float):
        return [str(value)]
    if isinstance(value, list):
        return [found for inner in value for found in _strings(inner)]
    if isinstance(value, dict) and isinstance(props := value.get("props"), dict):
        return _strings(props.get("children"))
    return []


def _patch(value: Json) -> bool:
    return isinstance(value, dict) and "__dash_patch_update" in value


def _loads(drive: Drive) -> list[dict[str, Json]]:
    return [
        {
            "dataset": getattr(record, "dataset", None),
            "params": getattr(record, "params", None),
            "rows": getattr(record, "rows", None),
            "ms": getattr(record, "ms", None),
            "cache": getattr(record, "cache", None),
        }
        for record in drive.records
        if record.levelno >= logging.INFO and record.getMessage().startswith("loaded ")
    ]


def _notifications(side: Mapping[str, Mapping[str, Json]]) -> list[object]:
    sent = side.get(ids.NOTIFICATIONS, {}).get("sendNotifications")
    return [{"title": note.get("title"), "message": note.get("message")} for note in sent if isinstance(note, dict)] if isinstance(sent, list) else []


def _typed(dataset: str, param: str, kind: type | None, text: str) -> Hashable:
    """`text`, a param of `dataset`'s, as its declared type."""
    if kind is None:
        return text  # bd.load names the params it takes
    refused = RenderError(f"--param {param}={text} is not a {kind.__name__}, which {dataset}'s {param} is: give one, as {_example(kind)}")
    if kind is bool:
        if text.lower() not in {"true", "false"}:
            raise refused
        return text.lower() == "true"
    try:
        if kind is date:
            return date.fromisoformat(text)
        if kind is int:
            return int(text)
        if kind is float:
            return float(text)
    except ValueError:
        raise refused from None
    return text


def _example(kind: type) -> str:
    return {date: "2026-11-13", bool: "true", int: "3", float: "0.25"}.get(kind, "text")


def parsed(given: Sequence[str], flag: str) -> dict[str, Json]:
    """Each `NAME=VALUE` of `given`, by NAME, its VALUE as JSON reads it where it can, else as text."""
    found: dict[str, Json] = {}
    for one in given:
        name, equals, value = one.partition("=")
        if not equals or not name:
            msg = f"{flag} {one} names no value: give {flag} NAME=VALUE"
            raise RenderError(msg)
        try:
            found[name] = json.loads(value)
        except json.JSONDecodeError:
            found[name] = value
    return found
