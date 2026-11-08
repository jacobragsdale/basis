"""A number's display format, which a field declares, `Annotated[float, api.format.money("USD")]`, so a UI formats
each column by it and its pages never choose decimals (design.md, "What a UI can rely on"; contract.md, "Display
formats"). basis writes the agreed object into the OpenAPI document as `x-basis-format` on the field's schema, and,
for a dataset, into its Arrow field's metadata as `basis.format`. Each format is basis-core's `display.Display`, which
checks it when the field is declared: an unknown currency, decimals outside 0 to 15, or a field its kind does not
have, raise `DisplayError`, whose message ends with the fix. Rates and spreads are fractions: 0.0525 is 5.25% and
525 bp."""

from dataclasses import dataclass
from typing import Any

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from basis.core.display import Display, Step

EXTENSION = "x-basis-format"
"""The key of a field's display format in the OpenAPI document."""
METADATA = "basis.format"
"""The key of a column's display format in an Arrow field's metadata, its value the object as JSON."""


@dataclass(frozen=True)
class Formatted:
    """A field's display format, as `Annotated` metadata: pydantic gives it to the field's JSON schema."""

    display: Display

    def __get_pydantic_json_schema__(self, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        found: dict[str, Any] = dict(handler(core_schema))
        found[EXTENSION] = self.display.as_json()
        return found


def number(decimals: int | None = None) -> Formatted:
    """A plain number, with `decimals` shown, or the formatter's default."""
    return Formatted(Display(kind="number", decimals=decimals))


def money(currency: str, decimals: int | None = None) -> Formatted:
    """An amount of `currency`, ISO 4217's upper-case code, with its minor units shown unless `decimals` says."""
    return Formatted(Display(kind="money", currency=currency, decimals=decimals))


def pct(decimals: int | None = None) -> Formatted:
    """A fraction, shown as a percentage: 0.0525 is 5.25%."""
    return Formatted(Display(kind="pct", decimals=decimals))


def bps(decimals: int | None = None) -> Formatted:
    """A fraction, shown in basis points, times 10,000: 0.0525 is 525 bp."""
    return Formatted(Display(kind="bps", decimals=decimals))


def price(decimals: int | None = None) -> Formatted:
    """A price, with `decimals` shown."""
    return Formatted(Display(kind="price", decimals=decimals))


def price32(step: Step | None = None) -> Formatted:
    """A price in 32nds, to its finest `step`: `32nd`, the default, `half`, `quarter` or `eighth` of one."""
    return Formatted(Display(kind="price32", step=step))
