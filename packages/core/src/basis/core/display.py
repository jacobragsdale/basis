"""Text for values as people read them, in a Slack message or an email, written as the UI writes them: a Python port
of parallax's formatter (basis-ui's `packages/parallax/crates/parallax-engine/src/format.rs`), which follows Excel's
display rules, so a number reads the same in a message as in the grid.

- A float is first rounded to 15 significant digits, half away from zero, as Excel does: 2.675, whose double is
  2.67499999999999982..., becomes 2.67500000000000, and shows as 2.68; 0.1 + 0.2 shows as 0.30.
- Percent, basis-point and K/M/B scaling then move the decimal point exactly, and the result is rounded once to the
  shown decimals, half away from zero. A value that rounds to zero never shows a minus sign.
- A price in 32nds, `99-16+`, is read at a float's shortest digits, rounded to 8 decimals, then to its step.
- An instant shows in a zone, `America/New_York` unless the format names another; sub-second digits past the shown
  precision are cut, never rounded.

`Formatter(Format(...))` is the port, method for method, held to every case of basis-ui's
`packages/parallax/crates/parallax-engine/tests/fixtures/format.json`, which parallax's Rust writes. `text(value,
display)` is what code calls: it reads the contract's display format, the object a dataset's column declares as
`basis.format` (`docs/contract.md`, "Display formats"), as a `Display` or that JSON object, and shows a Python value:

| Kind | The object | Shows 1234.5 as |
|---|---|---|
| A number | `{"kind": "number", "decimals": 2}` | `1,234.50` |
| Money | `{"kind": "money", "currency": "USD"}`, `decimals` defaulting to the currency's ISO 4217 minor units | `$1,234.50` |
| A percentage of a fraction | `{"kind": "pct", "decimals": 2}` | `123,450.00%` |
| Basis points of a fraction | `{"kind": "bps", "decimals": 1}` | `12,345,000.0 bp` |
| A price | `{"kind": "price", "decimals": 3}` | `1,234.500` |
| A price in 32nds | `{"kind": "price32", "step": "32nd"}`, `step` one of `32nd`, `half`, `quarter`, `eighth` | `1234-16` |

Without a display format, a value takes the formatter's default for its type: a float 2 decimals, an int none, a
date `YYYY-MM-DD`, an aware datetime its wall clock in New York, a naive one as it is, a bool `TRUE` or `FALSE`, and
text as it is. None and NaN, which is how pandas marks a missing float, are the empty text, as the grid shows a
missing value. A `Format` gives the formatter's other choices, such as `Format(abbreviate="M")` for `1.2M`.

The module reads nothing and writes nothing, so job code may call it. It returns text and never changes a value: keep
the float for any arithmetic, and format it only where the text leaves for a person. Money is written as basis-ui's
`Money` component writes it, the sign first, then the symbol en-US gives the currency, as Node's `Intl.NumberFormat`
writes it (the table is ctzd's port's, `ctzd.display`), or else the code and a no-break space: `-$1,234.50`,
`(€1,234.50)`, `CHF 1,234.50`. The codes and their minor units are ISO 4217's list one, as its maintenance agency
published it on 2026-09-17; a code with no minor units, such as XAU, is no currency to show money in."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, assert_never, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from basis.core.errors import BasisError

type Abbreviation = Literal["K", "M", "B"]
"""A fixed scaling by a thousand, million or billion, whose suffix is always shown."""
type Suffixes = Literal["amount", "par"]
"""How an abbreviation is written: `K`, `M` and `B` for amounts, or `M`, `MM` and `BN` for par amounts."""
type ThirtySeconds = Literal["thirty-seconds", "halves", "quarters", "eighths"]
"""The finest step a price in 32nds shows: a 32nd, or a half, quarter or eighth of one."""
type Negative = Literal["minus", "parentheses"]
"""How a negative number is written: `-1,234.50` or `(1,234.50)`."""
type Precision = Literal["seconds", "milliseconds", "microseconds"]
"""The sub-second digits an instant shows."""

DEFAULT_ZONE: Final = "America/New_York"
MAX_DECIMALS: Final = 30
"""The most decimals a `Format` may show, Excel's limit."""
NULL_TEXT: Final = ""
UNDEFINED_TEXT: Final = "\N{EM DASH}"
"""A result that has no value, such as a float's NaN, or an infinite price in 32nds."""
OUT_OF_RANGE_TEXT: Final = "out of range"
"""A date or instant past what the calendar can show."""

_SHIFTS: Final[dict[Abbreviation, int]] = {"K": 3, "M": 6, "B": 9}
_SUFFIXES: Final[dict[Suffixes, dict[Abbreviation, str]]] = {"amount": {"K": "K", "M": "M", "B": "B"}, "par": {"K": "M", "M": "MM", "B": "BN"}}
_STEPS: Final[dict[ThirtySeconds, int]] = {"thirty-seconds": 1, "halves": 2, "quarters": 4, "eighths": 8}
_DIGITS: Final[dict[Precision, int]] = {"seconds": 0, "milliseconds": 3, "microseconds": 6}
_SIGNS: Final[dict[Negative, tuple[str, str]]] = {"minus": ("-", ""), "parentheses": ("(", ")")}
_BP_SUFFIX: Final = "\N{NO-BREAK SPACE}bp"
_SIGNIFICANT: Final = 15
_PRICE_DECIMALS: Final = 8
"""The decimals a float price is read at before it is rounded to 32nds: enough for any 256th of a point."""
_EIGHTHS_PER_POINT: Final = 256
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_DAY: Final = date(1970, 1, 1)


class DisplayError(BasisError, ValueError):
    """A format or a display format that cannot be used, or a value it cannot show; the message gives the fix."""


@dataclass(frozen=True, kw_only=True)
class Format:
    """How a value is shown, as parallax's `Format` has it; a field left unset takes the value's type's default."""

    decimals: int | None = None
    """Digits after the decimal point: by default, a decimal's scale, 0 for an int, 1 when abbreviating, else 2."""
    thousands: bool | None = None
    """Whether to group thousands with commas; by default, yes."""
    percent: bool = False
    """Multiply by 100 and append `%`."""
    abbreviate: Abbreviation | None = None
    suffixes: Suffixes = "amount"
    bps: bool = False
    """Show a fraction in basis points: multiply by 10,000 and append ` bp`, so 0.0025 is `25 bp`."""
    thirty_seconds: ThirtySeconds | None = None
    negative: Negative = "minus"
    zone: str | None = None
    """The zone an instant shows in: `America/New_York` by default."""
    precision: Precision = "seconds"


class Formatter:
    """A checked `Format`, which shows values as parallax's `Formatter` does."""

    def __init__(self, format_: Format | None = None) -> None:
        given = Format() if format_ is None else format_
        if given.decimals is not None and (isinstance(given.decimals, bool) or not 0 <= given.decimals <= MAX_DECIMALS):
            msg = f"a format shows 0 to {MAX_DECIMALS} decimals, Excel's limit, not {given.decimals!r}: give fewer"
            raise DisplayError(msg)
        if given.percent and given.abbreviate is not None:
            msg = "a format cannot both show a percentage and abbreviate: give one of them"
            raise DisplayError(msg)
        styles = [
            name for on, name in ((given.percent, "a percentage"), (given.abbreviate is not None, "an abbreviation"), (given.bps, "basis points"), (given.thirty_seconds is not None, "32nds")) if on
        ]
        if len(styles) > 1:
            msg = f"a format shows one of a percentage, an abbreviation, basis points or 32nds, not {styles[0]} and {styles[1]}: give one of them"
            raise DisplayError(msg)
        zone = given.zone if given.zone is not None else DEFAULT_ZONE
        try:
            self._zone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            msg = f"there is no time zone {zone!r}: name an IANA zone, such as America/New_York"
            raise DisplayError(msg) from error
        self.format = given
        self._thousands = given.thousands if given.thousands is not None else True

    def float_text(self, value: float) -> str:
        """A float: 15 significant digits, then the shown decimals."""
        if math.isnan(value):
            return UNDEFINED_TEXT
        negative = math.copysign(1.0, value) < 0
        if (step := self.format.thirty_seconds) is not None:
            if math.isinf(value):
                return UNDEFINED_TEXT
            digits, exponent = _shortest(abs(value))
            points = _round_half_away(digits, exponent, _PRICE_DECIMALS)
            return self._thirty_seconds(points, -_PRICE_DECIMALS, step, negative=negative)
        if math.isinf(value):
            return self._signed(f"\N{INFINITY}{_BP_SUFFIX if self.format.bps else ''}", negative=negative)
        digits, exponent = (0, 0) if value == 0 else _fifteen_digits(abs(value))
        return self._number(digits, exponent, self._decimals(2), negative=negative)

    def decimal_text(self, unscaled: int, scale: int) -> str:
        """An exact decimal, `unscaled * 10**-scale`."""
        if (step := self.format.thirty_seconds) is not None:
            return self._thirty_seconds(abs(unscaled), -scale, step, negative=unscaled < 0)
        natural = min(max(scale - (4 if self.format.bps else 0), 0), MAX_DECIMALS)
        return self._number(abs(unscaled), -scale, self._decimals(natural), negative=unscaled < 0)

    def integer_text(self, value: int) -> str:
        if (step := self.format.thirty_seconds) is not None:
            return self._thirty_seconds(abs(value), 0, step, negative=value < 0)
        return self._number(abs(value), 0, self._decimals(0), negative=value < 0)

    def date_text(self, days: int) -> str:
        """A date, `days` after 1970-01-01, as `YYYY-MM-DD`, from -9999 to 9999 as parallax's calendar has them; a
        date never passes through a zone."""
        if not _FIRST_DAY <= days <= _LAST_DAY:
            return OUT_OF_RANGE_TEXT
        year, month, day = _civil(days)
        return f"{year:04d}-{month:02d}-{day:02d}" if year >= 0 else f"{year:05d}-{month:02d}-{day:02d}"

    def timestamp_text(self, micros: int, *, zoned: bool) -> str:
        """An instant, `micros` after the epoch, in the display zone where `zoned`, or a wall-clock time as stored."""
        try:
            moment = (_EPOCH + timedelta(microseconds=micros)).astimezone(self._zone if zoned else UTC)
        except OverflowError:
            return OUT_OF_RANGE_TEXT
        text = f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} {moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        if digits := _DIGITS[self.format.precision]:
            text += f".{moment.microsecond:06d}"[: digits + 1]
        return text

    @staticmethod
    def boolean_text(value: bool) -> str:  # noqa: FBT001 -- the value it shows
        return "TRUE" if value else "FALSE"

    def _decimals(self, natural: int) -> int:
        if self.format.decimals is not None:
            return self.format.decimals
        if self.format.percent:
            return 2
        return 1 if self.format.abbreviate is not None else natural

    def _signed(self, body: str, *, negative: bool) -> str:
        if not negative:
            return body
        before, after = _SIGNS[self.format.negative]
        return f"{before}{body}{after}"

    def _number(self, digits: int, exponent: int, decimals: int, *, negative: bool) -> str:
        """`digits * 10**exponent`, rounded to `decimals` places."""
        given = self.format
        exponent += (2 if given.percent else 0) + (4 if given.bps else 0) - (_SHIFTS[given.abbreviate] if given.abbreviate is not None else 0)
        rounded = _round_half_away(digits, exponent, decimals)
        whole, fraction = divmod(rounded, _power(decimals))
        body = f"{whole:,}" if self._thousands else str(whole)
        if decimals > 0:
            body += f".{fraction:0{decimals}d}"
        if given.abbreviate is not None:
            body += _SUFFIXES[given.suffixes][given.abbreviate]
        body += ("%" if given.percent else "") + (_BP_SUFFIX if given.bps else "")
        return self._signed(body, negative=negative and rounded != 0)

    def _thirty_seconds(self, digits: int, exponent: int, step: ThirtySeconds, *, negative: bool) -> str:
        """The price `digits * 10**exponent` points in 32nds, rounded to `step`, half away from zero: `99-16+`."""
        per_point = 32 * _STEPS[step]
        eighths = _round_half_away(digits * per_point, exponent, 0) * (_EIGHTHS_PER_POINT // per_point)
        whole, rest = divmod(eighths, _EIGHTHS_PER_POINT)
        thirty_seconds, eighth = divmod(rest, 8)
        mark = "" if eighth == 0 else "+" if eighth == 4 else str(eighth)  # noqa: PLR2004 -- four eighths of a 32nd, its half, written +
        return self._signed(f"{whole}-{thirty_seconds:02d}{mark}", negative=negative and eighths != 0)


def _civil(days: int) -> tuple[int, int, int]:
    """The proleptic Gregorian year, month and day `days` after 1970-01-01 (Howard Hinnant's `civil_from_days`), for
    the years Python's `date` cannot hold."""
    shifted = days + 719_468
    era = shifted // 146_097
    of_era = shifted - era * 146_097
    year_of_era = (of_era - of_era // 1460 + of_era // 36_524 - of_era // 146_096) // 365
    day_of_year = of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    month_index = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * month_index + 2) // 5 + 1
    month = month_index + 3 if month_index < 10 else month_index - 9  # noqa: PLR2004 -- March-based months, January and February after December
    return year_of_era + era * 400 + (1 if month <= 2 else 0), month, day  # noqa: PLR2004 -- January and February belong to the next year


_FIRST_DAY: Final = -4_371_587
"""-9999-01-01, the first day parallax's calendar shows."""
_LAST_DAY: Final = 2_932_896
"""9999-12-31, its last."""


def _power(exponent: int, *, base: int = 10) -> int:
    """`base**exponent`, for an exponent of 0 or more."""
    return cast("int", base**exponent)  # typeshed types an int's power as Any, since a negative exponent makes a float


def _round_half_away(digits: int, exponent: int, decimals: int) -> int:
    """`digits * 10**exponent`, which is not negative, rounded to `decimals` places, half away from zero, and scaled
    by `10**decimals`."""
    shift = exponent + decimals
    if shift >= 0:
        return digits * _power(shift)
    divisor = _power(-shift)
    quotient, remainder = divmod(digits, divisor)
    return quotient + 1 if 2 * remainder >= divisor else quotient


def _fifteen_digits(magnitude: float) -> tuple[int, int]:
    """A positive finite float rounded to 15 significant digits, half away from zero, from its exact decimal value:
    the digits and their exponent."""
    numerator, denominator = magnitude.as_integer_ratio()
    k = denominator.bit_length() - 1  # the denominator is 2**k, and n / 2**k is n * 5**k * 10**-k
    exact = numerator * _power(k, base=5)
    if (dropped := len(str(exact)) - _SIGNIFICANT) <= 0:
        return exact, -k
    return _round_half_away(exact, -dropped, 0), dropped - k


def _shortest(magnitude: float) -> tuple[int, int]:
    """A positive finite float's shortest text that reads back as the same float, as `repr` and JavaScript's `String`
    write it: its digits and their exponent."""
    mantissa, _, exponent = repr(magnitude).partition("e")
    whole, _, fraction = mantissa.partition(".")
    return int(whole + fraction), int(exponent or "0") - len(fraction)


# The contract's display formats, which a dataset's column declares and a UI shows it by.

type Kind = Literal["number", "money", "pct", "bps", "price", "price32"]
type Step = Literal["32nd", "half", "quarter", "eighth"]
KINDS: Final[tuple[Kind, ...]] = ("number", "money", "pct", "bps", "price", "price32")
_STEP_NAMES: Final[dict[Step, ThirtySeconds]] = {"32nd": "thirty-seconds", "half": "halves", "quarter": "quarters", "eighth": "eighths"}
MAX_DISPLAY_DECIMALS: Final = 15
"""The most decimals a display format may show, as the contract has it."""


@dataclass(frozen=True, kw_only=True)
class Display:
    """A display format, the contract's object: `Display(kind="money", currency="USD")` is
    `{"kind": "money", "currency": "USD"}`. `Display.of` reads the object, and `as_json` writes it."""

    kind: Kind
    decimals: int | None = None
    """0 to 15; each kind's default where it is None: a money format's currency's minor units, and 2 otherwise."""
    currency: str | None = None
    """A money format's upper-case ISO 4217 code, and no other kind's."""
    step: Step | None = None
    """A price32 format's finest step, `32nd` by default, and no other kind's."""
    _formatter: Formatter = field(init=False, repr=False, compare=False)
    _symbol: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            msg = f"there is no display format {self.kind!r}: give one of {', '.join(KINDS)}"
            raise DisplayError(msg)
        if self.decimals is not None and (isinstance(self.decimals, bool) or not isinstance(self.decimals, int) or not 0 <= self.decimals <= MAX_DISPLAY_DECIMALS):
            msg = f"a display format shows 0 to {MAX_DISPLAY_DECIMALS} decimals, not {self.decimals!r}: give a whole number in that range"
            raise DisplayError(msg)
        if self.currency is None and self.kind == "money":
            msg = 'a money format needs a currency: give one, as currency="USD"'
            raise DisplayError(msg)
        if self.currency is not None and self.kind != "money":
            msg = f"a {self.kind} format takes no currency: leave it out, or make it a money format"
            raise DisplayError(msg)
        if self.step is not None and self.kind != "price32":
            msg = f"a {self.kind} format takes no step, which a price32 format has: leave it out"
            raise DisplayError(msg)
        if self.step is not None and self.step not in _STEP_NAMES:
            msg = f"a price32 format's step is one of {', '.join(_STEP_NAMES)}, not {self.step!r}: give one of them"
            raise DisplayError(msg)
        symbol, decimals = "", self.decimals
        if self.currency is not None:
            minor = CURRENCIES.get(self.currency)
            if minor is None:
                msg = f"{self.currency!r} is no ISO 4217 currency with minor units: give an upper-case code, such as USD"
                raise DisplayError(msg)
            symbol = SYMBOLS.get(self.currency, f"{self.currency}\N{NO-BREAK SPACE}")
            decimals = minor if decimals is None else decimals
        object.__setattr__(self, "_formatter", Formatter(self._format(decimals)))
        object.__setattr__(self, "_symbol", symbol)

    def _format(self, decimals: int | None) -> Format:
        match self.kind:
            case "number" | "price" | "money":
                return Format(decimals=decimals)
            case "pct":
                return Format(decimals=decimals, percent=True)
            case "bps":
                return Format(decimals=decimals, bps=True)
            case "price32":
                return Format(thirty_seconds=_STEP_NAMES[self.step or "32nd"])
            case _:
                assert_never(self.kind)

    @classmethod
    def of(cls, given: Mapping[str, object]) -> Display:
        """A display format from its JSON object, which must hold only the fields its kind has."""
        known = {"kind", "decimals", "currency", "step"}
        if unknown := sorted(set(given) - known):
            msg = f"a display format has kind, decimals, currency and step, not {', '.join(unknown)}: leave them out"
            raise DisplayError(msg)
        kind, decimals, currency, step = given.get("kind"), given.get("decimals"), given.get("currency"), given.get("step")
        found_kind = next((name for name in KINDS if name == kind), None)
        if found_kind is None:
            msg = f"there is no display format {kind!r}: give one of {', '.join(KINDS)}"
            raise DisplayError(msg)
        if decimals is not None and (isinstance(decimals, bool) or not isinstance(decimals, int)):
            msg = f"a display format's decimals is a whole number, not {decimals!r}: give one from 0 to {MAX_DISPLAY_DECIMALS}"
            raise DisplayError(msg)
        if currency is not None and not isinstance(currency, str):
            msg = f"a money format's currency is an ISO 4217 code, not {currency!r}: give one, such as USD"
            raise DisplayError(msg)
        found_step = next((name for name in _STEP_NAMES if name == step), None)
        if step is not None and found_step is None:
            msg = f"a price32 format's step is one of {', '.join(_STEP_NAMES)}, not {step!r}: give one of them"
            raise DisplayError(msg)
        return cls(kind=found_kind, decimals=decimals, currency=currency, step=found_step)

    def as_json(self) -> dict[str, object]:
        """The contract's object, with only the fields given."""
        return {"kind": self.kind} | {name: value for name, value in (("decimals", self.decimals), ("currency", self.currency), ("step", self.step)) if value is not None}

    def text(self, value: float) -> str:
        """A number's text in this format: a float's, or an int's, exactly."""
        shown = self._formatter.integer_text(value) if isinstance(value, int) else self._formatter.float_text(value)
        if not self._symbol or shown == UNDEFINED_TEXT:
            return shown
        sign = shown[0] if shown[0] in "-(" else ""
        return f"{sign}{self._symbol}{shown[len(sign) :]}"


def check(given: Mapping[str, object]) -> Display:
    """The display format `given`, the contract's JSON object, checked as basis checks one where a column declares it:
    an unknown kind or field, a currency that is not ISO 4217's, or decimals outside 0 to 15 raise `DisplayError`,
    whose message ends with the fix."""
    return Display.of(given)


_DEFAULT = Formatter()


def text(value: object, display: Display | Mapping[str, object] | Format | None = None) -> str:
    """`value` as people read it: a number in `display`, a display format or a `Format`, or any value in its type's
    default, as the module's docstring says."""
    formatter = Formatter(display) if isinstance(display, Format) else _DEFAULT
    match value:
        case None:
            shown = NULL_TEXT
        case _ if type(value).__name__ == "NaTType":  # pandas' missing time, which is a datetime that holds no time
            shown = NULL_TEXT
        case str():
            shown = value
        case bool():
            shown = Formatter.boolean_text(value)
        case datetime():
            shown = formatter.timestamp_text(_micros(value), zoned=value.tzinfo is not None)
        case date():
            shown = formatter.date_text((value - _EPOCH_DAY).days)
        case _:
            number = _number(value)
            if number is None:
                shown = NULL_TEXT
            elif display is None or isinstance(display, Format):
                shown = formatter.integer_text(number) if isinstance(number, int) else formatter.float_text(number)
            else:
                shown = (display if isinstance(display, Display) else Display.of(display)).text(number)
    return shown


def _number(value: object) -> float | int | None:
    """A number as a Python float or int, numpy's and pandas' scalars among them, or None for a missing one."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    kind = type(value).__module__.partition(".")[0]
    if kind == "numpy" and hasattr(value, "dtype") and hasattr(value, "item"):
        plain: object = value.item()  # ty: ignore[call-non-callable] -- a numpy scalar, which item() makes a Python one
        if isinstance(plain, bool):
            msg = f"{value!r} is a bool, not a number: show it as text"
            raise DisplayError(msg)
        return _number(plain)
    if kind == "pandas" and type(value).__name__ == "NAType":
        return None
    msg = f"basis.core.display shows numbers, dates, times, bools and text, not {type(value).__name__}: give one of them"
    raise DisplayError(msg)


def _micros(moment: datetime) -> int:
    """`moment`'s microseconds after the epoch: an aware one's instant, or a naive one's wall clock, as stored."""
    since = (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)) - _EPOCH
    return (since.days * 86_400 + since.seconds) * 1_000_000 + since.microseconds


CURRENCIES: Final[dict[str, int]] = {
    "AED": 2,
    "AFN": 2,
    "ALL": 2,
    "AMD": 2,
    "AOA": 2,
    "ARS": 2,
    "AUD": 2,
    "AWG": 2,
    "AZN": 2,
    "BAM": 2,
    "BBD": 2,
    "BDT": 2,
    "BHD": 3,
    "BIF": 0,
    "BMD": 2,
    "BND": 2,
    "BOB": 2,
    "BOV": 2,
    "BRL": 2,
    "BSD": 2,
    "BTN": 2,
    "BWP": 2,
    "BYN": 2,
    "BZD": 2,
    "CAD": 2,
    "CDF": 2,
    "CHE": 2,
    "CHF": 2,
    "CHW": 2,
    "CLF": 4,
    "CLP": 0,
    "CNY": 2,
    "COP": 2,
    "COU": 2,
    "CRC": 2,
    "CUP": 2,
    "CVE": 2,
    "CZK": 2,
    "DJF": 0,
    "DKK": 2,
    "DOP": 2,
    "DZD": 2,
    "EGP": 2,
    "ERN": 2,
    "ETB": 2,
    "EUR": 2,
    "FJD": 2,
    "FKP": 2,
    "GBP": 2,
    "GEL": 2,
    "GHS": 2,
    "GIP": 2,
    "GMD": 2,
    "GNF": 0,
    "GTQ": 2,
    "GYD": 2,
    "HKD": 2,
    "HNL": 2,
    "HTG": 2,
    "HUF": 2,
    "IDR": 2,
    "ILS": 2,
    "INR": 2,
    "IQD": 3,
    "IRR": 2,
    "ISK": 0,
    "JMD": 2,
    "JOD": 3,
    "JPY": 0,
    "KES": 2,
    "KGS": 2,
    "KHR": 2,
    "KMF": 0,
    "KPW": 2,
    "KRW": 0,
    "KWD": 3,
    "KYD": 2,
    "KZT": 2,
    "LAK": 2,
    "LBP": 2,
    "LKR": 2,
    "LRD": 2,
    "LSL": 2,
    "LYD": 3,
    "MAD": 2,
    "MDL": 2,
    "MGA": 2,
    "MKD": 2,
    "MMK": 2,
    "MNT": 2,
    "MOP": 2,
    "MRU": 2,
    "MUR": 2,
    "MVR": 2,
    "MWK": 2,
    "MXN": 2,
    "MXV": 2,
    "MYR": 2,
    "MZN": 2,
    "NAD": 2,
    "NGN": 2,
    "NIO": 2,
    "NOK": 2,
    "NPR": 2,
    "NZD": 2,
    "OMR": 3,
    "PAB": 2,
    "PEN": 2,
    "PGK": 2,
    "PHP": 2,
    "PKR": 2,
    "PLN": 2,
    "PYG": 0,
    "QAR": 2,
    "RON": 2,
    "RSD": 2,
    "RUB": 2,
    "RWF": 0,
    "SAR": 2,
    "SBD": 2,
    "SCR": 2,
    "SDG": 2,
    "SEK": 2,
    "SGD": 2,
    "SHP": 2,
    "SLE": 2,
    "SOS": 2,
    "SRD": 2,
    "SSP": 2,
    "STN": 2,
    "SVC": 2,
    "SYP": 2,
    "SZL": 2,
    "THB": 2,
    "TJS": 2,
    "TMT": 2,
    "TND": 3,
    "TOP": 2,
    "TRY": 2,
    "TTD": 2,
    "TWD": 2,
    "TZS": 2,
    "UAH": 2,
    "UGX": 0,
    "USD": 2,
    "USN": 2,
    "UYI": 0,
    "UYU": 2,
    "UYW": 4,
    "UZS": 2,
    "VED": 2,
    "VES": 2,
    "VND": 0,
    "VUV": 0,
    "WST": 2,
    "XAD": 2,
    "XAF": 0,
    "XCD": 2,
    "XCG": 2,
    "XOF": 0,
    "XPF": 0,
    "YER": 2,
    "ZAR": 2,
    "ZMW": 2,
    "ZWG": 2,
}
"""Each ISO 4217 currency's minor units, as list one has them, published 2026-09-17; codes with none are left out."""

SYMBOLS: Final[dict[str, str]] = {
    "AUD": "A$", "BRL": "R$", "CAD": "CA$", "CNY": "CN\N{YEN SIGN}", "EUR": "\N{EURO SIGN}", "GBP": "\N{POUND SIGN}",
    "HKD": "HK$", "ILS": "\N{NEW SHEQEL SIGN}", "INR": "\N{INDIAN RUPEE SIGN}", "JPY": "\N{YEN SIGN}", "KRW": "\N{WON SIGN}",
    "MXN": "MX$", "NZD": "NZ$", "PHP": "\N{PESO SIGN}", "TWD": "NT$", "USD": "$", "VND": "\N{DONG SIGN}", "XAF": "FCFA\N{NO-BREAK SPACE}",
    "XCD": "EC$", "XCG": "Cg.", "XOF": "F\N{NARROW NO-BREAK SPACE}CFA\N{NO-BREAK SPACE}", "XPF": "CFPF\N{NO-BREAK SPACE}",
}  # fmt: skip
"""The symbol en-US writes before an amount in each currency that has one other than its code, as Node 26's
`Intl.NumberFormat` (ICU 78, CLDR 48) writes them, from ctzd's port; a symbol that ends in a letter takes a no-break
space after it, as every other code does."""
