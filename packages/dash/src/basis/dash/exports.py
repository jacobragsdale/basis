"""Exports, which carry their provenance: a grid's rows as an Excel workbook, each column in its display format as
Excel's own number format, its second sheet saying what was asked, as of when, and when the data was read; or as CSV,
guarded against formula injection. A grid declared with `exports=True` has the buttons and the download, and a
callback of the page's answers them:

    POSITIONS = bd.grid("positions", Position, key="cusip", exports=True)

    @callback(Output(POSITIONS.download, "data"), Input(POSITIONS.export, "n_clicks"), State("as-of", "value"), prevent_initial_call=True)
    async def export(_: list[int | None], as_of: str) -> dict[str, object]:
        day = bd.day(as_of)
        frame = await bd.load(POSITIONS_DATA, as_of=day)
        return POSITIONS.exported(frame, asked={"As of": as_of}, loaded_at=bd.loaded(POSITIONS_DATA, as_of=day))

- **Excel** goes through basis-core's workbook writer (`basis.core.folders.workbook`, `basis-core[excel]`): every text a
  cell's text, never a formula. A number keeps its value, so a sum in Excel is the sum, and shows as the grid shows it:
  `"$"#,##0` for dollars, `0.00%` for a percentage, `# ??/32` for a price in 32nds. Basis points, which Excel has no
  format for, are written as basis points, the column's header saying so ("OAS (bp)"), rather than shown wrong.
- **CSV** is UTF-8 with a byte-order mark, which Excel reads as UTF-8; numbers at full precision, dates as ISO text,
  missing values empty; and a text that would start a formula (`=`, `+`, `-`, `@`, a tab or a return) starts with an
  apostrophe instead, which a spreadsheet shows as text.

Pure but for `exported`, which reads which button was pressed from Dash's callback context."""

import csv as csv_module
import io
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from basis.core.display import CURRENCIES, DEFAULT_ZONE, SYMBOLS, Display
from basis.core.rows import Row
from basis.core.tables import Column
from basis.dash.errors import DeclarationError

type Kind = Literal["csv", "xlsx"]
KINDS: Final[tuple[Kind, ...]] = ("csv", "xlsx")
EXPORT: Final = "basis-export"
"""The type of a grid's export buttons' ids, `{"type": EXPORT, "grid": ID, "kind": "csv" | "xlsx"}`."""
DOWNLOAD: Final = "basis-download"
"""The type of a grid's download's id, `{"type": DOWNLOAD, "grid": ID}`."""
SHEET: Final = "Data"
ABOUT: Final = "About"
"""The provenance sheet: what was asked, as of when, when it was read."""
FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")
"""What a cell's text may not start with in a CSV, since a spreadsheet would run it (OWASP's CSV injection)."""
BASIS_POINTS: Final = 10_000
_STEPS: Final = {"32nd": 32, "half": 64, "quarter": 128, "eighth": 256}
_MEDIA: Final[dict[Kind, str]] = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def number_format(display: Display | None) -> str:
    """Excel's own number format for `display`, which shows a value as the grid does: `#,##0.00` where none is given."""
    if display is None:
        return "#,##0.00"
    decimals = display.decimals
    match display.kind:
        case "money":
            currency = str(display.currency)
            places = decimals if decimals is not None else CURRENCIES[currency]
            symbol = SYMBOLS.get(currency, f"{currency}\N{NO-BREAK SPACE}").replace('"', "")
            shown = f'"{symbol}"{_digits(places)}'
            return f"{shown};-{shown}"
        case "pct":
            return f"0{_fraction(2 if decimals is None else decimals)}%"
        case "bps":
            return _digits(2 if decimals is None else decimals)  # of the value in basis points, which `excel` writes
        case "price32":
            return f"# ??/{_STEPS[display.step or '32nd']}"
        case _:
            return _digits(2 if decimals is None else decimals)


def excel(
    frame: pd.DataFrame,
    row: type[Row],
    *,
    headers: Mapping[str, str] | None = None,
    title: str,
    asked: Mapping[str, object] | None = None,
    loaded_at: datetime | None = None,
    now: datetime | None = None,
) -> bytes:
    """`frame`, of `row`'s columns, as a workbook: its data in each column's format, under `headers` or the columns'
    names, and an About sheet naming `title`, what was `asked`, when it was exported and when it was read."""
    columns = _columns(frame, row)
    data: dict[str, pd.Series] = {}
    formats: dict[str, str] = {}
    zones: dict[str, str] = {}
    for found in columns:
        named = (headers or {}).get(found.name, found.name)
        values = frame[found.name]
        if found.kind == "float" and found.display is not None and found.display.kind == "bps":
            named, values = f"{named} (bp)", values.astype("float64") * BASIS_POINTS
        if found.kind == "float":
            formats[named] = number_format(found.display)
        elif found.kind == "date":
            formats[named] = "yyyy-mm-dd"
        elif found.kind == "datetime":
            formats[named] = "yyyy-mm-dd hh:mm:ss"
            zones[named] = found.zone or "UTC"
        data[named] = values
    from basis.core.folders import workbook  # noqa: PLC0415 -- the writer, and the tracing it brings, load when an export runs, never when an app is declared

    sheets = {SHEET: pd.DataFrame(data), ABOUT: about(title=title, asked=asked or {}, rows=len(frame), loaded_at=loaded_at, now=now)}
    return workbook(sheets, zones=zones, formats={SHEET: formats}, what=f"the export of {title}")


def about(*, title: str, asked: Mapping[str, object], rows: int, loaded_at: datetime | None, now: datetime | None = None) -> pd.DataFrame:
    """An export's provenance, as its About sheet holds it: two columns of text."""
    zone = ZoneInfo(DEFAULT_ZONE)
    exported = (now or datetime.now(UTC)).astimezone(zone)
    lines: list[tuple[str, str]] = [("View", title), ("Exported", _when(exported)), ("Rows", f"{rows:,}")]
    lines.append(("Read from its source", _when(loaded_at.astimezone(zone)) if loaded_at is not None else "not known"))
    lines += [(f"Asked: {name}", _asked(value)) for name, value in asked.items()]
    return pd.DataFrame(lines, columns=["Field", "Value"])


def csv(frame: pd.DataFrame, row: type[Row], *, headers: Mapping[str, str] | None = None) -> bytes:
    """`frame`, of `row`'s columns, as CSV, as the module's docstring says."""
    columns = _columns(frame, row)
    out = io.StringIO()
    writer = csv_module.writer(out, lineterminator="\r\n")
    writer.writerow([_guarded((headers or {}).get(found.name, found.name)) for found in columns])
    for values in zip(*(frame[found.name].tolist() for found in columns), strict=True):
        writer.writerow([_cell(value) for value in values])
    return ("\N{BYTE ORDER MARK}" + out.getvalue()).encode("utf-8")


def downloaded(kind: Kind, content: bytes, name: str) -> dict[str, object]:
    """What a `dcc.Download` takes for `content`, a file of `kind` named `name` and its kind's suffix."""
    import base64  # noqa: PLC0415 -- only a download needs it

    return {"content": base64.b64encode(content).decode("ascii"), "filename": f"{name}.{kind}", "type": _MEDIA[kind], "base64": True}


def _columns(frame: pd.DataFrame, row: type[Row]) -> Sequence[Column]:
    if missing := [found.name for found in row.columns if found.name not in frame]:
        msg = f"the frame to export lacks {', '.join(missing)}, which {row.__qualname__} declares: export a frame of its row"
        raise DeclarationError(msg)
    return row.columns


def _cell(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA or type(value).__name__ == "NaTType":
        return ""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return _guarded(str(value))


def _guarded(text: str) -> str:
    return f"'{text}" if text.startswith(FORMULA) else text


def _asked(value: object) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(str(part) for part in value) if value else "all"
    return "all" if value in {None, ""} else str(value)


def _when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z")


def _digits(places: int) -> str:
    return f"#,##0{_fraction(places)}"


def _fraction(places: int) -> str:
    return f".{'0' * places}" if places > 0 else ""
