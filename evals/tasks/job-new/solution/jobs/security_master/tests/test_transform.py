from datetime import date

from basis.job.testing import frame, inputs

from jobs.security_master import job
from jobs.security_master.rows import Security

ONE: dict[str, object] = {"security_id": "S1", "isin": "US0000000001", "name": "One", "currency": "USD", "coupon": 0.05, "maturity_date": date(2030, 1, 15), "active": True}


def run(*rows: dict[str, object]) -> list[str]:
    given = inputs(job.Inputs, securities=frame(Security, *rows))
    return job.check(given, job.transform(given, job.Params()), job.Params())


def test_only_active_securities_are_written() -> None:
    given = inputs(job.Inputs, securities=frame(Security, ONE, ONE | {"security_id": "S2", "active": False}))
    assert job.transform(given, job.Params()).master["security_id"].tolist() == ["S1"]


def test_two_active_securities_with_one_isin_are_refused() -> None:
    assert run(ONE, ONE | {"security_id": "S2"}) == ["active securities share the ISINs ['US0000000001']"]


def test_an_inactive_one_may_share_it() -> None:
    assert run(ONE, ONE | {"security_id": "S2", "active": False}) == []
