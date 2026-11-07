"""Expected returns from the signals API, one table that every portfolio shares."""

from datetime import timedelta

from optim import Dataset, Row
from optim.schema import Id


class Signal(Row):
    security_id: Id
    expected_return: float
    """Expected annual return on market value, as a fraction: 0.05 is 5%."""


SIGNALS = Dataset("signals", Signal, source="rest", query="/v1/expected-returns", key=("security_id",), cache=timedelta(minutes=5))
