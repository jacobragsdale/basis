"""Shared folders: a path inside one never leaves it, a sheet reads as text for its row to type, and a workbook is
written whole, through a temporary file renamed into place, so a reader never sees half of one."""

import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest
from pydantic import ValidationError

from basis.core.folders import TEMPORARY, Folder, FolderError, cell_text, folder
from basis.core.settings import GivenSettings, SettingsError


@pytest.fixture
def shared(tmp_path: Path) -> Folder:
    (tmp_path / "pm").mkdir()
    return Folder("pm", tmp_path / "pm")


def arrow(values: list[object], kind: pa.DataType) -> pd.Series:
    return pd.Series(values, dtype=pd.ArrowDtype(kind))


def positions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "account": arrow(["001234", None], pa.string()),
            "quantity": [100.0, float("nan")],
            "price": [101.25, 2.675],
            "day": arrow([date(2026, 11, 5), None], pa.date32()),
            "booked_at": arrow([datetime(2026, 11, 5, 14, 30, tzinfo=UTC), None], pa.timestamp("us", "UTC")),
            "open": arrow([True, None], pa.bool_()),
        }
    )


def test_a_workbook_written_reads_back_as_the_text_its_row_types(shared: Folder) -> None:
    written = shared.write_workbook("reports/positions.xlsx", {"Positions": positions()}, zones={"booked_at": "America/New_York"})
    assert written.path == shared.root / "reports" / "positions.xlsx"
    read = shared.read_sheet("reports/positions.xlsx", "Positions")
    assert read.to_dict("records") == [
        {"account": "001234", "quantity": "100", "price": "101.25", "day": "2026-11-05", "booked_at": "2026-11-05T09:30:00", "open": "true"},  # New York's wall clock
        {"account": None, "quantity": None, "price": "2.675", "day": None, "booked_at": None, "open": None},
    ]
    assert str(read.dtypes["price"]) == "string"


def test_a_workbook_is_written_whole_and_leaves_no_temporary_file(shared: Folder) -> None:
    first = shared.write_workbook("positions.xlsx", {"Positions": positions()})
    second = shared.write_workbook("positions.xlsx", {"Positions": positions().iloc[:1]})
    assert first.sha256 != second.sha256
    assert __import__("hashlib").sha256((shared.root / "positions.xlsx").read_bytes()).hexdigest() == second.sha256
    assert [path.name for path in shared.root.iterdir()] == ["positions.xlsx"]


def test_a_write_that_fails_leaves_what_was_there_and_no_temporary_file(shared: Folder) -> None:
    (shared.root / "positions.xlsx").mkdir()  # a folder where the workbook goes, so the rename into place fails
    with pytest.raises(OSError, match=r"positions\.xlsx"):
        shared.write_bytes("positions.xlsx", b"workbook")
    assert (shared.root / "positions.xlsx").is_dir()
    assert not [path for path in shared.root.iterdir() if path.name.endswith(TEMPORARY)]


@pytest.mark.parametrize("relative", ["../elsewhere.xlsx", "/etc/passwd", "C:\\\\Users\\\\x.xlsx", "a/../../b.xlsx", ""])
def test_a_path_that_leaves_the_folder_is_refused(shared: Folder, relative: str) -> None:
    with pytest.raises(FolderError, match="no path inside the folder pm"):
        shared.at(relative)


@pytest.mark.parametrize(
    ("call", "says"),
    [
        (lambda shared: shared.read_sheet("missing.xlsx", "Positions"), "pm has no missing.xlsx"),
        (lambda shared: shared.read_sheet("positions.csv", "Positions"), "positions.csv in pm is no workbook"),
        (lambda shared: shared.write_workbook("positions.xls", {"P": positions()}), "basis writes workbooks as .xlsx"),
        (lambda shared: Folder("pm", shared.root / "gone").write_bytes("x.xlsx", b""), "the folder pm, at"),
    ],
)
def test_a_workbook_that_cannot_be_read_or_written_is_refused_with_the_fix(shared: Folder, call: Callable[[Folder], object], says: str) -> None:
    with pytest.raises(FolderError, match=says):
        call(shared)


def test_a_sheet_the_workbook_lacks_is_refused_naming_it(shared: Folder) -> None:
    shared.write_workbook("positions.xlsx", {"Positions": positions()})
    with pytest.raises(FolderError, match="has no sheet 'Trades'"):
        shared.read_sheet("positions.xlsx", "Trades")


def test_a_folder_is_named_by_the_settings_and_is_absolute() -> None:
    settings = GivenSettings.model_validate({"folders": {"pm": {"path": "/mnt/pm"}}})
    assert folder("pm", settings).root == Path("/mnt/pm")
    with pytest.raises(SettingsError, match=r"the folder risk is not configured.*set BASIS_FOLDERS__RISK__PATH"):
        folder("risk", settings)
    with pytest.raises(ValidationError, match="a folder's path is absolute on this machine"):
        GivenSettings.model_validate({"folders": {"pm": {"path": "pm"}}})


@pytest.mark.parametrize(
    ("value", "text"),
    [(None, None), ("", None), ("as is", "as is"), (True, "true"), (100.0, "100"), (2.675, "2.675"), (1e15, "1000000000000000"), (1e16, "1e+16"), (2.0**60, "1.152921504606847e+18"), (7, "7")],
)
def test_a_cell_reads_as_the_text_a_csv_would_hold(value: object, text: str | None) -> None:
    assert cell_text(value) == text


def test_text_that_looks_like_a_formula_or_a_link_is_written_as_text(shared: Folder) -> None:
    """A workbook a portfolio manager opens must not run a formula a source's text holds."""
    frame = pd.DataFrame({"note": arrow(['=HYPERLINK("https://example.com","x")', "https://example.com/a", "+1-2"], pa.string())})
    shared.write_workbook("notes.xlsx", {"Notes": frame})
    with zipfile.ZipFile(shared.root / "notes.xlsx") as workbook:
        sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
        names = workbook.namelist()
    assert "<f>" not in sheet  # no cell holds a formula
    assert "<hyperlinks>" not in sheet
    assert not any(name.startswith("xl/worksheets/_rels/") for name in names)
    assert shared.read_sheet("notes.xlsx", "Notes")["note"].tolist() == ['=HYPERLINK("https://example.com","x")', "https://example.com/a", "+1-2"]


def test_text_longer_than_a_cell_holds_is_refused_rather_than_cut(shared: Folder) -> None:
    frame = pd.DataFrame({"note": arrow(["x" * 32_768], pa.string())})
    with pytest.raises(FolderError, match=r"notes.xlsx: Notes's note holds text of 32768 characters, and an Excel cell holds 32767: shorten it in the transform"):
        shared.write_workbook("notes.xlsx", {"Notes": frame})
    assert not (shared.root / "notes.xlsx").exists()
