"""The formatter against what parallax's Rust writes, and the display formats and values code shows through it.

`fixtures/display/format.json` is basis-ui's `packages/parallax/crates/parallax-engine/tests/fixtures/format.json`, at
0182278: each group a format, as parallax's `Format` serializes it, and each case a value with the text the Rust
formatter writes for it."""

import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict, Field

from basis.core import display
from basis.core.display import DisplayError, Format, Formatter, check, text

FIXTURE = Path(__file__).parent / "fixtures" / "display" / "format.json"


class Rules(BaseModel):
    """A group's format, as parallax serializes its `Format`."""

    model_config = ConfigDict(extra="forbid")
    abbreviate: Literal["K", "M", "B"] | None
    bps: bool
    decimals: int | None
    negative: Literal["minus", "parentheses"]
    percent: bool
    precision: Literal["seconds", "milliseconds", "microseconds"]
    suffixes: Literal["amount", "par"]
    thirty_seconds: Literal["thirty-seconds", "halves", "quarters", "eighths"] | None = Field(alias="thirtySeconds")
    thousands: bool | None
    zone: str | None

    def format(self) -> Format:
        return Format(**self.model_dump(by_alias=False))


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["float", "decimal", "integer", "date", "timestamp"]
    text: str
    value: float | str | None = None
    """A float's value, `NaN`, `inf` or `-inf` for those, or an integer's digits."""
    unscaled: str | None = None
    scale: int | None = None
    days: int | None = None
    micros: str | None = None
    zoned: bool | None = None

    def shown(self, formatter: Formatter) -> str:
        match self.kind:
            case "float":
                return formatter.float_text(float(self.value) if isinstance(self.value, str) else float(self.value or 0))
            case "integer":
                return formatter.integer_text(int(str(self.value)))
            case "decimal":
                return formatter.decimal_text(int(str(self.unscaled)), self.scale or 0)
            case "date":
                return formatter.date_text(self.days or 0)
            case "timestamp":
                return formatter.timestamp_text(int(str(self.micros)), zoned=bool(self.zoned))


class Group(BaseModel):
    format: Rules
    cases: tuple[Case, ...]


class Fixture(BaseModel):
    groups: tuple[Group, ...]


GROUPS = Fixture.model_validate_json(FIXTURE.read_bytes()).groups


def test_the_fixture_holds_every_kind_of_value_parallax_formats() -> None:
    cases = [case for group in GROUPS for case in group.cases]
    assert len(cases) == 4286
    assert {case.kind for case in cases} == {"float", "decimal", "integer", "date", "timestamp"}


@pytest.mark.parametrize(
    "group",
    GROUPS,
    ids=lambda group: ",".join(f"{key}={value}" for key, value in group.format.model_dump(by_alias=True).items() if value not in {None, False, "minus", "seconds", "amount"}) or "default",
)
def test_the_port_writes_what_parallax_s_rust_writes_for_every_case(group: Group) -> None:
    formatter = Formatter(group.format.format())
    differ = [(case.kind, case.value or case.unscaled or case.days or case.micros, case.text, shown) for case in group.cases if (shown := case.shown(formatter)) != case.text]
    assert differ == []


@pytest.mark.parametrize(
    ("given", "says"),
    [
        (Format(decimals=31), "a format shows 0 to 30 decimals, Excel's limit, not 31"),
        (Format(percent=True, abbreviate="M"), "a format cannot both show a percentage and abbreviate"),
        (Format(bps=True, thirty_seconds="halves"), "not basis points and 32nds"),
        (Format(zone="Mars/Olympus"), "there is no time zone 'Mars/Olympus'"),
    ],
)
def test_a_format_parallax_refuses_is_refused_with_the_fix(given: Format, says: str) -> None:
    with pytest.raises(DisplayError, match=r": \w") as raised:
        Formatter(given)
    assert says in str(raised.value)


@pytest.mark.parametrize(
    ("given", "value", "shown"),
    [
        ({"kind": "number"}, 1234.5, "1,234.50"),
        ({"kind": "number", "decimals": 0}, 2.5, "3"),
        ({"kind": "money", "currency": "USD"}, -1234.5, "-$1,234.50"),
        ({"kind": "money", "currency": "JPY"}, 1234.5, "\N{YEN SIGN}1,235"),  # its minor units, 0
        ({"kind": "money", "currency": "KWD"}, 1.5, "KWD\N{NO-BREAK SPACE}1.500"),  # 3, and no symbol of its own
        ({"kind": "money", "currency": "EUR", "decimals": 0}, 2.5, "\N{EURO SIGN}3"),
        ({"kind": "pct", "decimals": 2}, 0.0525, "5.25%"),
        ({"kind": "pct"}, -0.00001, "0.00%"),  # rounds to zero, so no minus sign
        ({"kind": "bps", "decimals": 1}, 0.0525, "525.0\N{NO-BREAK SPACE}bp"),
        ({"kind": "price", "decimals": 3}, 101.2345, "101.235"),
        ({"kind": "price32"}, 99.515625, "99-17"),  # 16 and a half 32nds, rounded half away from zero
        ({"kind": "price32", "step": "half"}, 99.515625, "99-16+"),
        ({"kind": "price32", "step": "eighth"}, 99.50390625, "99-161"),
        ({"kind": "money", "currency": "USD"}, math.inf, "$\N{INFINITY}"),
    ],
)
def test_a_display_format_shows_a_number_as_the_ui_does(given: dict[str, object], value: float, shown: str) -> None:
    assert text(value, given) == shown
    assert check(given).as_json() == given


@pytest.mark.parametrize(
    ("given", "says"),
    [
        ({"kind": "currency"}, "there is no display format 'currency': give one of number, money, pct, bps, price, price32"),
        ({"kind": "number", "places": 2}, "a display format has kind, decimals, currency and step, not places"),
        ({"kind": "money", "currency": "usd"}, "'usd' is no ISO 4217 currency with minor units: give an upper-case code"),
        ({"kind": "money", "currency": "XAU"}, "'XAU' is no ISO 4217 currency with minor units"),
        ({"kind": "money"}, "a money format needs a currency"),
        ({"kind": "pct", "currency": "USD"}, "a pct format takes no currency"),
        ({"kind": "number", "step": "half"}, "a number format takes no step"),
        ({"kind": "price32", "step": "sixteenth"}, "a price32 format's step is one of 32nd, half, quarter, eighth, not 'sixteenth'"),
        ({"kind": "number", "decimals": 16}, "a display format shows 0 to 15 decimals, not 16"),
        ({"kind": "number", "decimals": True}, "a display format's decimals is a whole number, not True"),
        ({"kind": "number", "decimals": 2.0}, "a display format's decimals is a whole number, not 2.0"),
    ],
)
def test_a_display_format_basis_does_not_know_is_refused_with_the_fix(given: dict[str, object], says: str) -> None:
    with pytest.raises(DisplayError, match=r": \w") as raised:
        check(given)
    assert says in str(raised.value)


NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (None, ""),
        (math.nan, ""),  # pandas' missing float, which the grid shows as missing
        (pd.NA, ""),
        (np.float64(2.675), "2.68"),
        (np.int64(-1234), "-1,234"),
        (2**70, "1,180,591,620,717,411,303,424"),  # an int is exact, at any size
        (0.1 + 0.2, "0.30"),
        (-0.0, "0.00"),
        (True, "TRUE"),
        ("as it is", "as it is"),
        (date(2026, 11, 7), "2026-11-07"),
        (datetime(2026, 11, 7, 14, 30, 5, 999_999, tzinfo=UTC), "2026-11-07 09:30:05"),  # New York's wall clock, cut to the second
        (datetime(2026, 11, 7, 14, 30, tzinfo=NEW_YORK), "2026-11-07 14:30:00"),
        (datetime(2026, 11, 7, 14, 30), "2026-11-07 14:30:00"),  # a wall-clock time, as it is
    ],
)
def test_a_value_without_a_format_takes_its_types_default(value: object, shown: str) -> None:
    assert text(value) == shown


def test_a_format_gives_the_formatter_s_other_choices() -> None:
    assert text(1_250_000.0, Format(abbreviate="M")) == "1.3M"
    assert text(5_000_000.0, Format(abbreviate="M", suffixes="par")) == "5.0MM"
    assert text(-1234.5, Format(negative="parentheses")) == "(1,234.50)"
    assert text(datetime(2026, 11, 7, 14, 30, 5, 123_456, tzinfo=UTC), Format(zone="Europe/London", precision="milliseconds")) == "2026-11-07 14:30:05.123"


@pytest.mark.parametrize("value", [np.bool_(1), [1.0], b"1"], ids=["a-numpy-bool", "a-list", "bytes"])
def test_a_value_it_cannot_show_is_refused_with_the_fix(value: object) -> None:
    with pytest.raises(DisplayError, match=r": \w"):
        text(value)


def test_the_kinds_and_currencies_are_the_contract_s() -> None:
    assert display.KINDS == ("number", "money", "pct", "bps", "price", "price32")
    assert (len(display.CURRENCIES), display.CURRENCIES["USD"], display.CURRENCIES["JPY"], display.CURRENCIES["BHD"], display.CURRENCIES["CLF"]) == (165, 2, 0, 3, 4)
    assert "XAU" not in display.CURRENCIES
