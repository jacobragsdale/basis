"""Accrued interest on a fixed-rate bond: the coupon interest earned between two dates under the bond's day count
convention, which a buyer pays the seller at settlement. Given a bond's terms, the service accrues over any period;
for a bond in its warehouse, it accrues since the last coupon, prices the bond with the pricing service's clean price,
streams its clean price to a UI that shows it live, solves for its yield to maturity in the service's process pool,
lists its coupons, a dataset that a UI's grid can take
as Arrow, and records settlements for a signed-in user, booking each with the ledger service through the warehouse's
outbox.

This file is the service, the declaration basis builds its app from, in which each route declares its deadline, its
response and its errors. Its code sits in layers, each a module basis knows by its name, and each importing only those
below it: `handlers.py`, one function per route, which reach the warehouse, the pricing service, the cache, the clock
and new ids only through what `api.given` gives them, and pass each on to `repository.py`, all the SQL and every call
to another service; and `domain.py`, the pure types and rules, which a job could run as they stand. Beside them,
`resources.py` declares the settings and the resources, and `errors.py` the errors, the meaning of each of the
domain's exceptions among them. The yield solver is `compute.py`, and the app around it all, with its logs, traces,
errors, health, transactions, process pool and shutdown, is basis's. `dev.py` makes its fakes for `basis dev`."""

from http import HTTPStatus

from basis import api

from basis.api.example import handlers
from basis.api.example.domain import Accrual, Bond, DayCount, DirtyPrice, Quote, Settlement, Yield
from basis.api.example.errors import NO_YIELD, OUTSIDE_ITS_LIFE, UNKNOWN_BOND, UNPRICED, PeriodReversed, PeriodTooLong
from basis.api.example.resources import CLOCK, IDS, LEDGER, PRICING, TERMS, WAREHOUSE, Settings

service = api.Service(
    name="accruals",
    settings=Settings,
    resources=[WAREHOUSE, PRICING, LEDGER, TERMS, CLOCK, IDS],
    user=api.User,
    routes=[
        api.get("/accrued-interest", handlers.accrued_interest, deadline_s=1, response=Accrual, errors=[PeriodReversed, PeriodTooLong]),
        api.get("/conventions", handlers.conventions, deadline_s=1, response=list[DayCount], errors=[]),
        api.get("/bonds/{security}", handlers.bond, deadline_s=5, response=Bond, errors=[UNKNOWN_BOND]),
        api.get("/bonds/{security}/dirty-price", handlers.dirty_price, deadline_s=5, response=DirtyPrice, errors=[UNKNOWN_BOND, UNPRICED, *OUTSIDE_ITS_LIFE]),
        api.stream("/bonds/{security}/quote", handlers.quote, deadline_s=5, every_s=2, response=Quote, errors=[UNPRICED]),
        api.get("/bonds/{security}/yield", handlers.bond_yield, deadline_s=5, response=Yield, errors=[UNKNOWN_BOND, UNPRICED, NO_YIELD, *OUTSIDE_ITS_LIFE]),
        api.get("/bonds/{security}/coupons", handlers.coupons, deadline_s=5, response=api.Table[handlers.CouponRow], errors=[UNKNOWN_BOND]),
        api.post("/settlements", handlers.record_settlement, status=HTTPStatus.CREATED, deadline_s=5, response=Settlement, errors=[UNKNOWN_BOND, *OUTSIDE_ITS_LIFE]),
    ],
    fakes="basis.api.example.dev:fakes",
)
