"""The example's yield solver, in this process: a price it can solve, and the bonds it cannot."""

import pytest

from basis.api.example.compute import price, yield_to_maturity


def test_the_yield_solved_for_prices_the_bond_at_its_dirty_price() -> None:
    annual = yield_to_maturity(100.6375, 2.25, 2, 17, 9.0 / 180)
    assert abs(price(annual, 2.25, 2, 17, 9.0 / 180) - 100.6375) < 1e-6


@pytest.mark.parametrize(("periods", "first"), [(0, 0.5), (17, 1.5), (17, -1.0)], ids=["no-coupons-left", "past-a-period", "before-now"])
def test_a_bond_with_no_coupon_ahead_within_a_period_has_no_yield(periods: int, first: float) -> None:
    with pytest.raises(ValueError, match="has no yield: give it one coupon or more, within a period"):
        yield_to_maturity(100.0, 2.25, 2, periods, first)
