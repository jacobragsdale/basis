"""30/360, fixed in the domain: an end on the 31st counts as the 30th only when the start is on the 30th or 31st."""

from datetime import date

import pytest

from quotes.domain import day_count


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        (date(2026, 3, 15), date(2026, 5, 31), 76),
        (date(2026, 3, 1), date(2026, 3, 31), 30),
        (date(2026, 3, 30), date(2026, 5, 31), 60),
        (date(2026, 3, 31), date(2026, 5, 31), 60),
        (date(2026, 1, 15), date(2026, 7, 15), 180),
    ],
)
def test_thirty_three_sixty(start: date, end: date, days: int) -> None:
    assert day_count(start, end, "30/360") == (days, 360)
