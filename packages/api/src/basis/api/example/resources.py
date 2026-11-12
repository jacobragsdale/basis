"""The example's declarations of what it reaches and how it is set up: its settings, and each resource its handlers
ask for, by name, with its deadline, which basis opens, bounds, traces and fakes."""

from typing import Annotated

from basis import api
from pydantic import Field

from basis.api.example import compute
from basis.api.example.domain import Bond


class Settings(api.Settings):
    max_days: Annotated[int, Field(ge=1)] = 3660
    """The longest period a request may accrue over, in calendar days: about ten years, past which a date is more
    likely a typo than a trade."""


WAREHOUSE = api.database(
    "warehouse",
    deadline_s=5,
    max_concurrent=8,
    outbox=api.outbox("accruals_outbox", max_age_s=86_400, backoff_s=60, max_backoff_s=3_600),
    idempotency=api.idempotency("accruals_requests", keep_s=86_400),
)
"""Where each bond's terms are, where settlements are recorded, and where their bookings wait for the ledger: a day, at
most, retried from a minute to an hour apart."""
PRICING = api.http("pricing", deadline_s=2)
"""The pricing service, which quotes each bond's clean price."""
LEDGER = api.http("ledger", deadline_s=2)
"""The ledger service, which books each settlement, and drops a booking whose `Idempotency-Key` it has seen."""
TERMS = api.cache("bond_terms", Bond, ttl_s=300, max_entries=10_000, deadline_s=5)
"""Bonds' terms, which change far less often than every five minutes."""
CLOCK = api.clock("clock")
IDS = api.ids("ids")
SOLVE = api.compute(compute.yield_to_maturity)
"""The yield solver, which runs in the service's process pool."""
