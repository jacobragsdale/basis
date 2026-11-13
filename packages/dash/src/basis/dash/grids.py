"""The grid, which reads its row: `bd.grid(id, Row)` is an AG Grid, Community features only, whose every column comes
from the row's, so a page never restates what the row declares:

    POSITIONS = bd.grid("positions", Position, key="cusip", totals={"par": "sum", "ytw": bd.weighted("par")}, columns={"pnl": bd.column(signed=True)})

    @callback(Output("positions", "rowData"), Output("positions", "dashGridOptions"), Input("as-of", "value"))
    async def show(as_of: str) -> tuple[bd.Rows, Patch]:
        positions = await bd.load(POSITIONS_DATA, as_of=bd.day(as_of))
        return bd.rows(positions), POSITIONS.totals(positions)

- **Each column** has its header (the name in words, `yield_to_worst` as "Yield to worst", an acronym in capitals),
  its alignment (a number right, in tabular figures), its format (the column's display format, through basis's
  browser formatter, `basisFormat`, which rounds as the server does), a filter of its kind (number, text or date),
  sorting and resizing.
- **Rows keep their ids** (`key`, a text column), so a refresh changes the rows that changed, and keeps the
  selection and the scroll.
- **Totals are of the rows shown**, a pinned row under them: `grid.totals(frame)`, a change to the grid's options,
  sums, counts, takes the least or most, or a mean weighted by a declared column (`bd.weighted("par")`), skipping
  missing values, as each column's `totals` entry says, and labels the row in the first text column without one. While
  a filter is on, the quick filter's or a column's, basis's script works the same totals out over the rows it keeps,
  and the server's come back when it is cleared.
- **A count** beside the quick filter: "300 rows", or "50 of 300 rows" while a filter is on.
- **A quick filter** over every column, above the grid, which filters in the browser alone; text a person can select
  and copy; a height of its own, so a large table scrolls inside it and draws only the rows shown.
- **A page overrides a column** where it must, with `bd.column(header=, width=, pin=, hide=, signed=, bar=, heat=)`:
  a signed number in the gain and loss colours, a bar in the cell for a magnitude, or a heat scale behind it.

The grid is `dag.AgGrid` itself, with its declaration kept beside it, so a page lays it out as any component."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

import dash_ag_grid as dag
import dash_mantine_components as dmc
import orjson
import pandas as pd
import pyarrow as pa

import dash
from basis.core import frames
from basis.core.display import Display
from basis.core.rows import Row
from basis.core.tables import Column
from basis.dash import exports as exports_module
from basis.dash import internals, states, texts, theme
from basis.dash.declarations import Dataset
from basis.dash.errors import DeclarationError
from basis.dash.exports import DOWNLOAD, EXPORT
from basis.dash.icons import icon
from dash import ALL, Patch, dcc, html
from dash.development.base_component import Component

type Total = Literal["sum", "mean", "min", "max", "count"] | Weighted
type Pin = Literal["left", "right"]

COUNT: Final = "basis-grid-count"
"""The type of a grid's count's id, `{"type": COUNT, "grid": ID}`, which basis's script writes: "50 of 300 rows"."""
SEARCH: Final = "basis-grid-search"
"""The type of a grid's quick filter's id, `{"type": SEARCH, "grid": ID}`, which the shell's one callback reads."""
EMPTY: Final = "No rows for this view"
"""What a grid with no rows says."""
HEIGHT: Final = 520
"""A grid's height in pixels, unless the page gives another: a large table scrolls inside it."""
ACRONYMS: Final = frozenset(
    {"id", "cusip", "isin", "sedol", "figi", "ticker", "oas", "dv01", "cs01", "pv01", "ytm", "ytw", "pnl", "usd", "eur", "gbp", "jpy", "fx", "nav", "mtd", "qtd", "ytd", "bp", "bps"}
)
"""Words a header writes in capitals."""
_KINDS: Final = {"float": "number", "str": "text", "bool": "boolean", "date": "dateString", "datetime": "dateTimeString"}
_FILTERS: Final = {"float": "agNumberColumnFilter", "str": "agTextColumnFilter", "bool": "agTextColumnFilter", "date": "agDateColumnFilter", "datetime": "agDateColumnFilter"}
_TOTALS: Final = ("sum", "mean", "min", "max", "count")


@dataclass(frozen=True)
class Weighted:
    """A mean weighted by another column of the row: `bd.weighted("par")`."""

    by: str


def weighted(by: str) -> Weighted:
    """A total that is the mean of a column weighted by column `by`, over the rows where both are given."""
    return Weighted(by)


@dataclass(frozen=True, kw_only=True)
class ColumnShown:
    """How a page shows one column, where it must differ from what its row declares."""

    header: str | None = None
    width: int | None = None
    """In pixels; by default the grid shares its width between its columns."""
    pin: Pin | None = None
    hide: bool = False
    signed: bool = False
    """A number in the gain colour above zero and the loss colour below."""
    bar: bool = False
    """A bar behind a number, as long as its magnitude against the column's largest."""
    heat: bool = False
    """A colour behind a number, from the diverging scale, by its value against the column's largest magnitude."""


def column(*, header: str | None = None, width: int | None = None, pin: Pin | None = None, hide: bool = False, signed: bool = False, bar: bool = False, heat: bool = False) -> ColumnShown:
    """A column shown otherwise than its row declares it."""
    return ColumnShown(header=header, width=width, pin=pin, hide=hide, signed=signed, bar=bar, heat=heat)


@dataclass(frozen=True)
class Declared:
    """What a grid was declared with, kept beside it for its totals."""

    row: type[Row]
    key: str | None
    totals: Mapping[str, Total]
    label: str | None
    """The text column the totals row's label sits in."""
    id: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    """Each column's header, as the grid shows it, which an export writes too."""


class Grid(html.Div):
    """A grid declared from a row, under its quick filter: `bd.grid(...)` makes one, and a page lays it out as any
    component."""

    declared: Declared

    def totals(self, frame: pd.DataFrame) -> Patch:
        """`frame`'s totals, the row pinned under the rows, as a change to the grid's `dashGridOptions`, which a callback
        outputs: `Output(ID, "dashGridOptions")`. Only the pinned row changes; the grid's other options stay."""
        patched = Patch()
        patched["pinnedBottomRowData"] = totals(frame, self.declared)
        return patched

    @property
    def download(self) -> dict[str, str]:
        """The id of the grid's download, which an export callback outputs `data` to."""
        return {"type": DOWNLOAD, "grid": self.declared.id}

    @property
    def export(self) -> dict[str, object]:
        """The ids of the grid's export buttons, which an export callback takes `n_clicks` from."""
        return {"type": EXPORT, "grid": self.declared.id, "kind": ALL}

    async def load_totals(self, dataset: object, /, **params: object) -> Patch:
        """`dataset`'s totals for `params`, as `totals` gives them, made in a worker thread once per value and kept with
        it in the cache, so another caller pays nothing for them: await it in an `async` callback."""
        from basis.dash import loads  # noqa: PLC0415 -- loads declares datasets, and the grid is declared before them

        if not isinstance(dataset, Dataset):
            msg = f"grid {self.declared.id}'s totals load a dataset, not {dataset!r}: give one bd.query, bd.file, bd.fetch or bd.view declares"
            raise DeclarationError(msg)
        declared = self.declared

        def made(table: pa.Table) -> tuple[object, int]:
            found = totals(frames.from_arrow(table), declared)
            return found, len(orjson.dumps(found))

        patched = Patch()
        patched["pinnedBottomRowData"] = await loads.derived(dataset, params, f"totals {declared.id}", made)
        return patched

    def exported(self, frame: pd.DataFrame, *, asked: Mapping[str, object] | None = None, loaded_at: datetime | None = None, name: str | None = None, title: str | None = None) -> dict[str, object]:
        """`frame` as the file the pressed button asks for, Excel or CSV, for the grid's download: named `name`, by
        default the grid's id and the day, with `title`, `asked` and `loaded_at` in an Excel export's About sheet."""
        pressed = dash.ctx.triggered_id if internals.in_callback() else None
        kind = pressed.get("kind") if isinstance(pressed, Mapping) else None
        if kind not in exports_module.KINDS:
            msg = f"grid {self.declared.id} exports from its own buttons, so call exported in a callback whose input is its export, not {pressed!r}"
            raise DeclarationError(msg)
        named = name or f"{self.declared.id}-{datetime.now(UTC).astimezone().date().isoformat()}"
        if kind == "csv":
            return exports_module.downloaded("csv", exports_module.csv(frame, self.declared.row, headers=self.declared.headers), named)
        made = exports_module.excel(frame, self.declared.row, headers=self.declared.headers, title=title or self.declared.id, asked=asked, loaded_at=loaded_at)
        return exports_module.downloaded("xlsx", made, named)


def grid(
    id: str,  # noqa: A002 -- a component's id, as Dash names it
    row: type[Row],
    *,
    key: str | None = None,
    totals: Mapping[str, Total] | None = None,
    columns: Mapping[str, ColumnShown] | None = None,
    height: int | None = HEIGHT,
    search: bool = True,
    noun: str = "rows",
    dataset: object = None,
    exports: bool = False,
) -> Grid:
    """A grid of `row`'s columns at `id`, its rows known by `key`, with `totals` and `columns`' overrides, `height`
    pixels tall, or as tall as its rows where None, for a short table, and a quick filter unless `search` is False. Its
    count names its rows `noun`: "300 holdings", or "50 of 300 holdings" while a filter is on; its marks say when it is
    stale, by `dataset`, or not updated (`basis.dash.states`). What a page lays out: the grid, under its quick filter,
    its count and its marks."""
    if not (isinstance(row, type) and issubclass(row, Row)):
        msg = f"grid {id}'s row is {row!r}: give a bd.Row subclass, whose columns the grid shows"
        raise DeclarationError(msg)
    by_name = {found.name: found for found in row.columns}
    given_totals = dict(totals or {})
    given_columns = dict(columns or {})
    _check(id, row, by_name, key, given_totals=given_totals, given_columns=given_columns)
    shown_columns = [found for found in row.columns if not given_columns.get(found.name, ColumnShown()).hide]
    label = next((found.name for found in shown_columns if found.kind == "str" and found.name not in given_totals), None)  # a hidden column would hold it out of sight
    options: dict[str, object] = {
        "theme": theme.GRID,
        "enableCellTextSelection": True,
        "ensureDomOrder": True,
        "animateRows": False,
        "suppressDragLeaveHidesColumns": True,
        "autoSizeStrategy": {"type": "fitGridWidth", "defaultMinWidth": 96},
        "localeText": {"noRowsToShow": EMPTY},  # text, never HTML, which dash-ag-grid rightly refuses to evaluate
        "loading": False,  # basis's own loading state, shown only once an answer is slow
        "suppressNoRowsOverlay": True,  # until the first answer, which basis's script then lets say "No rows"
    }
    if height is None:
        options["domLayout"] = "autoHeight"
    count = {"type": COUNT, "grid": id}
    options["context"] = {"basis": {"totals": {name: _total_json(total) for name, total in given_totals.items()}, "label": label, "key": key, "count": count, "noun": noun}}
    table = dag.AgGrid(
        id=id,
        columnDefs=[_definition(found, given_columns.get(found.name, ColumnShown())) for found in row.columns],
        defaultColDef={"sortable": True, "resizable": True, "filter": True},
        dashGridOptions=options,
        getRowId=f"params.data.{key}" if key is not None else None,
        eventListeners={event: ["basisTotals(params)"] for event in ("firstDataRendered", "rowDataUpdated", "modelUpdated", "pinnedRowDataChanged")},
        className="basis-grid-table",
        style={"height": f"{height}px" if height is not None else None, "width": "100%"},
    )
    tools: list[Component] = []
    if search:
        box = dcc.Input(id={"type": SEARCH, "grid": id}, type="search", placeholder="Filter rows", debounce=False, className="basis-grid-search-input", autoComplete="off")
        named = html.Span("Filter the rows", className="basis-visually-hidden")  # the box's name, which the label gives it
        tools.append(html.Label([icon("search"), named, box], className="basis-grid-search"))
    tools.append(html.Span(id=count, className="basis-grid-count", role="status"))
    tools.append(html.Span(className="basis-grid-gap"))
    tools.append(html.Span(states.marks(id, "rowData", dataset), className="basis-marks"))
    if exports:
        tools.append(html.Span([_export_button(id, kind) for kind in ("csv", "xlsx")], className="basis-grid-exports"))
        tools.append(dcc.Download(id={"type": DOWNLOAD, "grid": id}))
    body = html.Div([states.loading(table, id, ["rowData", "dashGridOptions"]), states.failed_body(id, "rowData")], className="basis-state-frame")
    shown = Grid([html.Div(tools, className="basis-grid-tools"), body], className="basis-grid")
    headers = {found.name: given_columns.get(found.name, ColumnShown()).header or header(found.name) for found in row.columns}
    object.__setattr__(shown, "declared", Declared(row, key, given_totals, label, id, headers))  # beside the component, which Dash never sends
    return shown


def _export_button(id: str, kind: str) -> Component:  # noqa: A002 -- the grid's id
    return dmc.Button(
        "Excel" if kind == "xlsx" else "CSV",
        id={"type": EXPORT, "grid": id, "kind": kind},
        leftSection=icon("download"),
        variant="default",
        size="compact-xs",
        className="basis-export",
        **{"aria-label": f"Export the rows as {'an Excel workbook' if kind == 'xlsx' else 'CSV'}"},
    )


def _total_json(total: Total) -> object:
    """A total as basis's script reads it: its kind, or `{"weighted": COLUMN}`."""
    return {"weighted": total.by} if isinstance(total, Weighted) else total


def header(name: str) -> str:
    """A column's name in words, as its header shows it: `yield_to_worst` as "Yield to worst", `oas_bps` as "OAS
    BPS"."""
    words = [word.upper() if word in ACRONYMS else word for word in name.split("_") if word]
    if not words:
        return name
    first = words[0]
    words[0] = first if first.isupper() else first[:1].upper() + first[1:]
    return " ".join(words)


def totals(frame: pd.DataFrame, declared: Declared) -> list[dict[str, object]]:
    """The totals row of `frame`, by `declared`'s totals, with NaN as None; none where it declares none."""
    if not declared.totals:
        return []
    found: dict[str, object] = {}
    for name, total in declared.totals.items():
        values = pd.to_numeric(frame[name], errors="coerce").astype("float64") if name in frame else pd.Series(dtype="float64")
        found[name] = _total(values, frame, total)
    if declared.label is not None:
        found[declared.label] = "Total"
    if declared.key is not None and declared.key not in found:
        found[declared.key] = ""
    return [found]


def _total(values: pd.Series, frame: pd.DataFrame, total: Total) -> float | None:
    given = values.dropna()
    if isinstance(total, Weighted):
        weights = pd.to_numeric(frame[total.by], errors="coerce").astype("float64")
        both = values.notna() & weights.notna()
        weight = float(weights[both].sum())
        shown = float((values[both] * weights[both]).sum()) / weight if weight != 0 else math.nan
    elif total == "count":
        shown = float(len(given))
    elif given.empty:
        shown = math.nan
    elif total == "sum":
        shown = math.fsum(given.tolist())
    elif total == "mean":
        shown = math.fsum(given.tolist()) / len(given)
    elif total == "min":
        shown = float(given.min())
    else:
        shown = float(given.max())
    return None if math.isnan(shown) else shown


def _definition(found: Column, shown: ColumnShown) -> dict[str, object]:
    defined: dict[str, object] = {"field": found.name, "headerName": shown.header or header(found.name), "cellDataType": _KINDS[found.kind], "filter": _FILTERS[found.kind]}
    if found.kind == "bool":
        defined |= {"cellDataType": False, "valueFormatter": {"function": "basisCheck(params.value)"}, "cellClass": "basis-check"}  # a mark, never AG Grid's checkbox, which reads as editable
    if found.kind == "float":
        display = found.display if found.display is not None else Display(kind="number")
        defined |= {
            "type": "rightAligned",
            "valueFormatter": texts.formatter(display),
            "cellClass": ["ag-right-aligned-cell", "basis-num"],
        }  # the type's class too, which a cellClass of its own replaces
        rules = {}
        if shown.signed:
            rules |= {"basis-gain": "params.value > 0", "basis-loss": "params.value < 0"}
        if rules:
            defined["cellClassRules"] = rules
        if shown.bar or shown.heat:
            defined["cellStyle"] = {"function": f"basisScale(params, {'"bar"' if shown.bar else '"heat"'})"}
    defined["minWidth"] = _least_width(found, defined["headerName"])
    if shown.width is not None:
        defined |= {"width": shown.width, "minWidth": min(shown.width, int(defined["minWidth"])), "suppressSizeToFit": True}
    if shown.pin is not None:
        defined["pinned"] = shown.pin
    if shown.hide:
        defined["hide"] = True
    return defined


_HEADER: Final = re.compile(r"\S")
_WIDTHS: Final = {"str": 112, "bool": 84, "date": 104, "datetime": 164}
"""The least width of a column of each kind, in pixels, so a value is never cut, at the grid's 13 px."""
_DIGIT: Final = 7.4
"""A tabular figure's width at 13 px of Inter, and about a letter's at the header's 12 px, in pixels."""
_PADDING: Final = 40
_SAMPLES: Final = {"pct": -0.9999, "bps": -0.9999, "price": -199.999, "price32": -199.99}
"""A large value of each kind of display format, whose text sets a column's least width; an amount's is a billion."""
"""A cell's padding, and the room a header keeps for its sort and filter marks."""


def _least_width(found: Column, header_text: object) -> int:
    """The least width that shows `found`'s values and its header whole: a number's by the text its format writes for
    a large value of its kind, an amount of a billion, a total's size, or a rate of -99.99%."""
    by_header = round(len(str(header_text)) * _DIGIT + _PADDING)
    if found.kind != "float":
        return max(_WIDTHS[found.kind], by_header)
    display = found.display or Display(kind="number")
    widest = texts.text(_SAMPLES.get(display.kind, -1_234_567_890.123), display)
    return max(round(len(widest) * _DIGIT + _PADDING - 16), by_header, 84)


def _check(id: str, row: type[Row], by_name: Mapping[str, Column], key: str | None, *, given_totals: Mapping[str, Total], given_columns: Mapping[str, ColumnShown]) -> None:  # noqa: A002 -- the grid's id
    where = f"grid {id}"
    if key is not None and (key not in by_name or by_name[key].kind != "str" or by_name[key].nullable):
        msg = f"{where}'s key is {key!r}: name a text column of {row.__qualname__} that is never null, as each row's id"
        raise DeclarationError(msg)
    for name, total in given_totals.items():
        if name not in by_name or by_name[name].kind != "float":
            msg = f"{where} totals {name!r}, which is no number column of {row.__qualname__}: total one of {', '.join(sorted(n for n, c in by_name.items() if c.kind == 'float'))}"
            raise DeclarationError(msg)
        if isinstance(total, Weighted):
            if total.by not in by_name or by_name[total.by].kind != "float":
                msg = f"{where} weights {name!r} by {total.by!r}, which is no number column of {row.__qualname__}: weight by one"
                raise DeclarationError(msg)
        elif total not in _TOTALS:
            msg = f"{where}'s total of {name!r} is {total!r}: give one of {', '.join(_TOTALS)}, or bd.weighted(COLUMN)"
            raise DeclarationError(msg)
    for name, shown in given_columns.items():
        if name not in by_name:
            msg = f"{where} shows {name!r}, which {row.__qualname__} does not declare: override one of its columns, {', '.join(by_name)}"
            raise DeclarationError(msg)
        if not isinstance(shown, ColumnShown):
            msg = f"{where}'s column {name!r} is {shown!r}: give bd.column(...)"
            raise DeclarationError(msg)
        if (shown.signed or shown.bar or shown.heat) and by_name[name].kind != "float":
            msg = f"{where}'s column {name!r} is no number, so it takes no signed colours, bar or heat"
            raise DeclarationError(msg)
        if shown.bar and shown.heat:
            msg = f"{where}'s column {name!r} has a bar and a heat scale: give one"
            raise DeclarationError(msg)
        if shown.header is not None and not _HEADER.search(shown.header):
            msg = f"{where}'s column {name!r} has an empty header: give it words, or leave it out"
            raise DeclarationError(msg)
