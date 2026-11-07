from datetime import date
from decimal import Decimal

from etl_framework.testing import frame, inputs

from jobs.security_master import job
from jobs.security_master.rows import Security

SECURITY = {"security_id": "XS0001", "isin": "XS0000000011", "name": "Alpha", "currency": "USD", "coupon": Decimal("4.25"), "maturity_date": date(2031, 6, 15), "active": True}


def test_only_active_securities_are_written() -> None:
    securities = frame(Security, SECURITY, SECURITY | {"security_id": "XS0002", "isin": "XS0000000029", "active": False})
    outputs = job.transform(inputs(job.Inputs, securities=securities), job.Params())
    assert outputs.master.get_column("security_id").to_list() == ["XS0001"]
    assert "active" not in outputs.master.columns


def test_two_active_securities_with_one_isin_stop_the_run() -> None:
    given = inputs(job.Inputs, securities=frame(Security, SECURITY, SECURITY | {"security_id": "XS0002"}))
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == ["active securities share the ISINs ['XS0000000011']"]


def test_an_inactive_security_may_share_an_isin() -> None:
    given = inputs(job.Inputs, securities=frame(Security, SECURITY, SECURITY | {"security_id": "XS0002", "active": False}))
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == []
