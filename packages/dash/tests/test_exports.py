"""Exports: each display format as Excel's own number format, a workbook whose values stay values and whose About
sheet says what was asked and when it was read, CSV guarded against formula injection, and a grid's buttons answered
by a callback of the page's."""

import base64
import io
import json
import math
import zipfile
from datetime import UTC, date, datetime
from typing import Annotated, Any

import pandas as pd
import pytest
from dash import Input, Output, State, callback, dcc

import basis.dash as bd
from basis.core.display import Display
from basis.dash import exports, internals, testing
from basis.dash.errors import DeclarationError


class Settings(bd.Settings):
    pass


class Position(bd.Row):
    cusip: str
    issuer: str
    maturity: date
    par: Annotated[float, bd.format.money("USD", 0)]
    price: Annotated[float, bd.format.price32()]
    ytw: Annotated[float, bd.format.pct(3)]
    oas: Annotated[float, bd.format.bps(1)] | None


FRAME = pd.DataFrame({
    "cusip": ["912828X1", "=HYPERLINK(1)"],
    "issuer": ["-Contoso", "@Fabrikam"],
    "maturity": [date(2030, 1, 15), date(2035, 6, 1)],
    "par": [1_000_000.0, 2_500_000.0],
    "price": [99.5, 101.015625],
    "ytw": [0.0525, 0.04875],
    "oas": [0.0123, math.nan],
})  # fmt: skip
GRID = bd.grid("export-grid", Position, key="cusip", exports=True, columns={"ytw": bd.column(header="Yield")})


@callback(Output(GRID.download, "data"), Input(GRID.export, "n_clicks"), State("export-as-of", "data"), prevent_initial_call=True)
async def export(_: list[int | None], as_of: str) -> dict[str, object]:
    return GRID.exported(FRAME, asked={"As of": as_of, "Books": ["Rates", "Credit"]}, loaded_at=datetime(2026, 11, 13, 19, 5, 3, tzinfo=UTC), name="positions")


APP = bd.App(name="exported", title="Exported", settings=Settings, pages=[bd.page("/", "Positions", body=[bd.card("Positions", GRID), dcc.Store(id="export-as-of", data="2026-11-12")])])


@pytest.mark.parametrize(
    ("display", "shown"),
    [
        (None, "#,##0.00"),
        (Display(kind="number", decimals=0), "#,##0"),
        (Display(kind="money", currency="USD", decimals=0), '"$"#,##0;-"$"#,##0'),
        (Display(kind="money", currency="EUR"), '"\N{EURO SIGN}"#,##0.00;-"\N{EURO SIGN}"#,##0.00'),
        (Display(kind="money", currency="CHF"), '"CHF\N{NO-BREAK SPACE}"#,##0.00;-"CHF\N{NO-BREAK SPACE}"#,##0.00'),
        (Display(kind="pct", decimals=3), "0.000%"),
        (Display(kind="bps", decimals=1), "#,##0.0"),
        (Display(kind="price", decimals=3), "#,##0.000"),
        (Display(kind="price32"), "# ??/32"),
        (Display(kind="price32", step="quarter"), "# ??/128"),
    ],
)
def test_each_display_format_is_excel_s_own(display: Display | None, shown: str) -> None:
    assert exports.number_format(display) == shown


def _book(content: bytes) -> dict[str, pd.DataFrame]:
    return pd.read_excel(io.BytesIO(content), sheet_name=None, engine="calamine")


def test_a_workbook_keeps_values_shows_formats_and_says_where_it_came_from() -> None:
    now = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    made = exports.excel(FRAME, Position, headers={"ytw": "Yield"}, title="positions", asked={"As of": "2026-11-12", "Book": None}, loaded_at=datetime(2026, 11, 13, 19, 5, 3, tzinfo=UTC), now=now)
    book = _book(made)
    assert list(book) == [exports.SHEET, exports.ABOUT]
    data = book[exports.SHEET]
    assert list(data.columns) == ["cusip", "issuer", "maturity", "par", "price", "Yield", "oas (bp)"]
    assert data["par"].tolist() == [1_000_000.0, 2_500_000.0]  # values, so a sum in Excel is the sum
    assert data["Yield"].tolist() == [0.0525, 0.04875]
    assert data["oas (bp)"].iloc[0] == pytest.approx(123.0)  # basis points, as its header says
    assert data["cusip"].iloc[1] == "=HYPERLINK(1)"  # a text, never a formula
    styles = zipfile.ZipFile(io.BytesIO(made)).read("xl/styles.xml").decode()
    for shown in ('"$"#,##0', "0.000%", "# ??/32", "yyyy-mm-dd"):
        assert f'formatCode="{shown.replace(chr(34), "&quot;")}' in styles
    about = dict(book[exports.ABOUT].itertuples(index=False, name=None))
    assert about == {"View": "positions", "Exported": "2026-11-13 15:00:00 EST", "Rows": "2", "Read from its source": "2026-11-13 14:05:03 EST", "Asked: As of": "2026-11-12", "Asked: Book": "all"}


def test_csv_is_guarded_against_formula_injection_and_keeps_full_precision() -> None:
    made = exports.csv(FRAME, Position, headers={"ytw": "Yield"})
    assert made.startswith("\N{BYTE ORDER MARK}".encode())
    lines = made.decode("utf-8").removeprefix("\N{BYTE ORDER MARK}").splitlines()
    assert lines[0] == "cusip,issuer,maturity,par,price,Yield,oas"
    assert lines[1] == "912828X1,'-Contoso,2030-01-15,1000000.0,99.5,0.0525,0.0123"
    assert lines[2] == "'=HYPERLINK(1),'@Fabrikam,2035-06-01,2500000.0,101.015625,0.04875,"


def test_a_frame_that_lacks_a_column_of_its_row_is_refused() -> None:
    with pytest.raises(DeclarationError, match="lacks oas"):
        exports.csv(FRAME.drop(columns=["oas"]), Position)


def test_a_grid_with_exports_has_its_buttons_and_its_download() -> None:
    sent = json.dumps(GRID.to_plotly_json(), default=lambda found: found.to_plotly_json())
    for kind in ("csv", "xlsx"):
        assert json.dumps({"type": exports.EXPORT, "grid": "export-grid", "kind": kind}) in sent
    assert json.dumps({"type": exports.DOWNLOAD, "grid": "export-grid"}) in sent
    assert GRID.download == {"type": exports.DOWNLOAD, "grid": "export-grid"}


@pytest.mark.parametrize("kind", ["xlsx", "csv"])
def test_a_button_pressed_downloads_its_file(kind: str) -> None:
    pressed = {"type": exports.EXPORT, "grid": "export-grid", "kind": kind}
    clicks = [{"id": {"type": exports.EXPORT, "grid": "export-grid", "kind": each}, "property": "n_clicks", "value": 1 if each == kind else None} for each in ("csv", "xlsx")]
    output = json.dumps({"grid": "export-grid", "type": exports.DOWNLOAD}, separators=(",", ":"))
    body = {
        "output": f"{output}.data",
        "outputs": {"id": {"type": exports.DOWNLOAD, "grid": "export-grid"}, "property": "data"},
        "inputs": [clicks],
        "state": [{"id": "export-as-of", "property": "data", "value": "2026-11-12"}],
        "changedPropIds": [f"{json.dumps(pressed, separators=(',', ':'), sort_keys=True)}.n_clicks"],
    }
    with testing.client(APP) as client:
        answered: Any = client.http.post(f"{client.base}{internals.UPDATE}", json=body).json()
    [download] = [props["data"] for props in answered["response"].values()]
    assert download["filename"] == f"positions.{kind}"
    content = base64.b64decode(download["content"])
    if kind == "xlsx":
        about = dict(_book(content)[exports.ABOUT].itertuples(index=False, name=None))
        assert (about["Asked: As of"], about["Asked: Books"], about["Read from its source"]) == ("2026-11-12", "Rates, Credit", "2026-11-13 14:05:03 EST")
        assert "Yield" in _book(content)[exports.SHEET].columns  # the grid's own header
    else:
        assert content.decode("utf-8").splitlines()[0].endswith("Yield,OAS")


def test_exported_outside_an_export_callback_is_refused() -> None:
    with pytest.raises(DeclarationError, match="whose input is its export"):
        GRID.exported(FRAME)
