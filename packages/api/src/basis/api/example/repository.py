"""The example's repository: all its SQL and every call it makes to another service, each function taking the resource
it needs and returning the domain's types. A failure that means something to the domain becomes its exception, as a
price the pricing service does not have is `Unpriced`; any other answers its own problem, so no handler needs a `try`.
The integration tier tests it against a real database."""

from datetime import datetime
from uuid import UUID

from basis import api
from pydantic import BaseModel, ConfigDict

from basis.api.example.domain import Bond, Settlement, UnknownBond, Unpriced

BOND = "SELECT security_id, coupon_rate, convention, frequency, issued, maturity FROM bonds WHERE security_id = %(security)s"
RECORD = "INSERT INTO settlements (settlement_id, security_id, face, settle, accrued, recorded_at) VALUES (%(settlement_id)s, %(security)s, %(face)s, %(settle)s, %(accrued)s, %(recorded_at)s)"


class CleanPrice(BaseModel):
    """The pricing service's quote. Fields it adds later are ignored, as basis-ui ignores an API's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    security_id: str
    clean: float
    """Per 100 of face."""


async def bond(warehouse: api.Reader, security: str) -> Bond:
    """The bond's terms, from the warehouse."""
    if (found := await warehouse.fetch_one(Bond, BOND, security=security)) is None:
        msg = f"the warehouse has no bond {security}"
        raise UnknownBond(msg)
    return found


async def cached_bond(warehouse: api.Reader, terms: api.Cache[Bond], security: str) -> Bond:
    """The bond's terms, from the cache while they are in it, and from the warehouse when they are not."""
    return await terms.get(security, lambda: bond(warehouse, security))


async def clean_price(pricing: api.Client, security: str) -> float:
    """The pricing service's clean price of the bond, per 100 of face."""
    try:
        return (await pricing.get("/prices/{security}", CleanPrice, segments={"security": security})).clean
    except api.NotFound as missing:  # every other kind of failure answers its own problem
        msg = f"the pricing service has no price for {security}"
        raise Unpriced(msg) from missing


def stamp(ids: api.Ids, clock: api.Clock) -> tuple[UUID, datetime]:
    """A new settlement's id, and the time it is recorded."""
    return ids.new(), clock.now()


async def record(warehouse: api.Writer, ledger: api.Client, settlement: Settlement) -> None:
    """Record the settlement in the warehouse, and book it with the ledger, by its id: the booking waits in the
    warehouse's outbox until the row commits."""
    await warehouse.execute(
        RECORD,
        settlement_id=settlement.settlement_id, security=settlement.security_id, face=settlement.face,
        settle=settlement.settle, accrued=settlement.accrued, recorded_at=settlement.recorded_at,
    )  # fmt: skip
    await ledger.send("POST", "/bookings", settlement, key=str(settlement.settlement_id))
