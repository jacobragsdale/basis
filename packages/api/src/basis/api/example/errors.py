"""The example's errors: the HTTP meaning of each of its domain's exceptions, which `domain.py` raises as plain
exceptions, as a job would, and the errors of its own requests, which its handlers raise. Its routes declare them."""

from http import HTTPStatus

from basis import api

from basis.api.example import domain


class PeriodReversed(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The period ends before it starts"


class PeriodTooLong(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The period is longer than this service accrues over"


UNKNOWN_BOND = api.error(domain.UnknownBond, status=HTTPStatus.NOT_FOUND, title="No bond has that ISIN")
UNPRICED = api.error(domain.Unpriced, status=HTTPStatus.NOT_FOUND, title="The pricing service has no price for the bond")
MATURED = api.error(domain.Matured, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="The bond matures on or before the settlement date")
NOT_ISSUED = api.error(domain.NotIssued, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="The bond is issued after the settlement date")
NO_YIELD = api.error(domain.NoYield, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="No yield gives the bond's price")

OUTSIDE_ITS_LIFE = [MATURED, NOT_ISSUED]
"""How a settlement date can fall outside a bond's life."""
