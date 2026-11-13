"""A number's display format, which a row's column declares once, `Annotated[float, bd.format.money("USD", 0)]`, so the
grid, a chart's axes and hover text, a stat tile and an export all read it, by one rounding rule. Each is basis-core's
`display.Display`, the object basis-ui reads too, checked where it is declared: an unknown currency, decimals outside 0
to 15, or a field its kind does not have, raise `DisplayError`, whose message ends with the fix. Rates and spreads are
fractions: 0.0525 is 5.25% and 525 bp."""

from basis.core.display import Display, Step

__all__ = ["Display", "bps", "money", "number", "pct", "price", "price32"]


def number(decimals: int | None = None) -> Display:
    """A plain number, with `decimals` shown, or the formatter's default."""
    return Display(kind="number", decimals=decimals)


def money(currency: str, decimals: int | None = None) -> Display:
    """An amount of `currency`, ISO 4217's upper-case code, with its minor units shown unless `decimals` says."""
    return Display(kind="money", currency=currency, decimals=decimals)


def pct(decimals: int | None = None) -> Display:
    """A fraction, shown as a percentage: 0.0525 is 5.25%."""
    return Display(kind="pct", decimals=decimals)


def bps(decimals: int | None = None) -> Display:
    """A fraction, shown in basis points, times 10,000: 0.0525 is 525 bp."""
    return Display(kind="bps", decimals=decimals)


def price(decimals: int | None = None) -> Display:
    """A price, with `decimals` shown."""
    return Display(kind="price", decimals=decimals)


def price32(step: Step | None = None) -> Display:
    """A price in 32nds, to its finest `step`: `32nd`, the default, `half`, `quarter` or `eighth` of one."""
    return Display(kind="price32", step=step)
