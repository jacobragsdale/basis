"""One number, one text: a value as people read it, on the server and in the browser alike, by basis-core's formatter
(`basis.core.display`), which follows Excel's rounding: 15 significant digits, half away from zero, then the decimals
shown.

- `bd.text(value, display)` is a value's text on the server: a stat tile's, a chart's hover text, a title's.
- `spec(display)` is what the browser's formatter takes for a column, every default resolved here, such as a money
  format's currency's minor units and symbol: `basisFormat(params.value, SPEC)` in a column's `valueFormatter`
  (`formatter(display)` writes it), the port of the same formatter in `static/basis.js`, held to basis-core's cases.

Reads and writes nothing."""

import json
from collections.abc import Mapping
from typing import Final

from basis.core.display import CURRENCIES, SYMBOLS, Display, Format
from basis.core.display import text as core_text

FUNCTION: Final = "basisFormat"
"""basis's formatter, in `window.dashAgGridFunctions`, under a name of basis's own."""
_STEPS: Final = {"32nd": "thirty-seconds", "half": "halves", "quarter": "quarters", "eighth": "eighths"}
_DEFAULT_DECIMALS: Final = 2


def text(value: object, display: Display | Mapping[str, object] | Format | None = None) -> str:
    """`value` as people read it: a number in its display format, or any value in its type's default; a missing one,
    None or NaN, is the empty text, as the grid shows it."""
    return core_text(value, display)


def spec(display: Display | Format | None) -> dict[str, object]:
    """The browser formatter's spec for `display`: the `Format`'s fields, and a money format's symbol."""
    if display is None:
        return {}
    if isinstance(display, Format):
        return _of_format(display)
    if display.kind == "price32":
        return _given(thirtySeconds=_STEPS[display.step or "32nd"])
    if display.kind == "money":
        currency = str(display.currency)
        decimals = display.decimals if display.decimals is not None else CURRENCIES[currency]
        return _given(decimals=decimals, symbol=SYMBOLS.get(currency, f"{currency}\N{NO-BREAK SPACE}"))
    decimals = display.decimals if display.decimals is not None else _DEFAULT_DECIMALS
    return _given(decimals=decimals, percent=display.kind == "pct", bps=display.kind == "bps")


def formatter(display: Display | Format | None) -> dict[str, str]:
    """A column's `valueFormatter` for dash-ag-grid: basis's formatter, with `display`'s spec."""
    return {"function": f"{FUNCTION}(params.value, {json.dumps(spec(display), separators=(',', ':'), ensure_ascii=False)})"}


def _of_format(given: Format) -> dict[str, object]:
    return _given(
        decimals=given.decimals,
        thousands=False if given.thousands is False else None,
        percent=given.percent,
        abbreviate=given.abbreviate,
        suffixes=given.suffixes if given.abbreviate is not None else None,
        bps=given.bps,
        thirtySeconds=given.thirty_seconds,
        negative=given.negative if given.negative != "minus" else None,
    )


def _given(**fields: object) -> dict[str, object]:
    return {name: value for name, value in fields.items() if value is not None and (value is not False or name == "thousands")}
