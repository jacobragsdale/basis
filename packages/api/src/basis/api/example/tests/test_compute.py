"""The example's yield solver, in this process: a price it can solve, and the bonds it cannot."""

from decimal import Decimal

import pytest

from basis.api.example.compute import price, yield_to_maturity


def test_the_yield_solved_for_prices_the_bond_at_its_dirty_price() -> None:
    annual = yield_to_maturity(Decimal("100.6375"), Decimal("2.25"), 2, 17, Decimal(9) / 180)
    assert abs(price(annual, Decimal("2.25"), 2, 17, Decimal(9) / 180) - Decimal("100.6375")) < Decimal("1e-6")


@pytest.mark.parametrize(("periods", "first"), [(0, Decimal("0.5")), (17, Decimal("1.5")), (17, Decimal(-1))], ids=["no-coupons-left", "past-a-period", "before-now"])
def test_a_bond_with_no_coupon_ahead_within_a_period_has_no_yield(periods: int, first: Decimal) -> None:
    with pytest.raises(ValueError, match="has no yield: give it one coupon or more, within a period"):
        yield_to_maturity(Decimal(100), Decimal("2.25"), 2, periods, first)
