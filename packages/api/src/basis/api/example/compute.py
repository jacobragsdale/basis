"""The example's compute: a bond's yield to maturity from its dirty price, solved by bisection. It is CPU-bound, and
grows with the coupons left to pay, so the service runs it in its process pool, off the event loop.

This module is pure, as the import contract keeps every compute module: it takes values, returns one, and reaches
nothing, not even basis-api. Its numbers are floats, as every number basis gives or takes is, and it rounds none: the
UI rounds once, as it shows them."""

import math

LOWEST = -0.99
HIGHEST = 10.0
"""The yields it searches between: -99% to 1000% a year."""
TOLERANCE = 1e-12
HUNDRED = 100.0


def yield_to_maturity(dirty: float, coupon: float, frequency: int, periods: int, first: float) -> float:
    """The annual yield, compounded `frequency` times a year, at which a bond's cash flows are worth `dirty`: `periods`
    coupons of `coupon` each, per 100 of face, the first of them `first` of a period away and each other a period after
    the one before, and 100 with the last. Within 1e-12, far below a hundredth of a basis point. It raises ValueError
    when no yield from -99% to 1000% a year gives that price."""
    if periods < 1 or not 0 <= first <= 1:
        msg = f"a bond with {periods} coupons left, the first {first} of a period away, has no yield: give it one coupon or more, within a period"
        raise ValueError(msg)
    low, high = LOWEST, HIGHEST
    if not price(high, coupon, frequency, periods, first) <= dirty <= price(low, coupon, frequency, periods, first):
        msg = f"no yield from {LOWEST:.0%} to {HIGHEST:.0%} a year gives a dirty price of {dirty}"
        raise ValueError(msg)
    while high - low > TOLERANCE:  # the price falls as the yield rises
        middle = (low + high) / 2
        if price(middle, coupon, frequency, periods, first) > dirty:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def price(annual: float, coupon: float, frequency: int, periods: int, first: float) -> float:
    """What the cash flows are worth at the yield `annual`, per 100 of face: each discounted by the yield per period,
    over the periods until it is paid."""
    discount = 1 / (1 + annual / frequency)
    factor = math.pow(discount, first)  # math.pow is a float, where ** of floats may be complex
    worth = 0.0
    for _ in range(periods):
        worth += coupon * factor
        factor *= discount
    return worth + HUNDRED * factor / discount
