"""The holdings' rules on frames, with no app, no fakes and no basis: what a job could run as it stands."""

import math
from datetime import date

import pandas as pd
import pytest

from basis.dash.example import domain


def _holdings() -> pd.DataFrame:
    columns = {
        "book": ["Credit", "Credit", "Liquidity"],
        "cusip": ["AAA111AA1", "BBB222BB2", "CCC333CC3"],
        "issuer": ["Contoso Financial", "Northwind Energy", "US Treasury"],
        "sector": ["Corporate", "Corporate", "Treasury"],
        "par": [2_000_000.0, 1_000_000.0, 5_000_000.0],
        "price": [101.0, 98.0, 100.0],
        "ytw": [0.05, 0.06, 0.04],
        "duration": [6.0, 4.0, 1.0],
    }
    return pd.DataFrame(columns)


def test_a_positions_market_value_weight_and_dv01_follow_from_its_par_price_and_duration() -> None:
    found = domain.positions(_moving(), date(2026, 11, 12))
    assert found["market_value"].tolist() == [2_020_000.0, 980_000.0, 5_000_000.0]
    assert found["dv01"].tolist() == pytest.approx([1_212.0, 392.0, 500.0])
    assert found["weight"].sum() == pytest.approx(1.0)


def test_a_book_and_a_search_keep_their_positions_and_weigh_them_again() -> None:
    positions = domain.positions(_moving(), date(2026, 11, 12))
    credit = domain.shown(positions, "Credit", "")
    assert credit["cusip"].tolist() == ["AAA111AA1", "BBB222BB2"]
    assert credit["weight"].tolist() == pytest.approx([2_020_000 / 3_000_000, 980_000 / 3_000_000])
    assert domain.shown(positions, "", "northwind")["cusip"].tolist() == ["BBB222BB2"]  # an issuer, ignoring case
    assert domain.shown(positions, "", "ccc333")["cusip"].tolist() == ["CCC333CC3"]  # a CUSIP
    assert domain.shown(positions, "Municipals", "").empty


def test_sectors_and_the_summary_weigh_yield_and_duration_by_market_value() -> None:
    positions = domain.positions(_moving(), date(2026, 11, 12))
    sectors = domain.by_sector(positions)
    assert sectors["sector"].tolist() == ["Treasury", "Corporate"]  # the largest first
    corporate = sectors.set_index("sector").loc["Corporate"]
    assert corporate["ytw"] == pytest.approx((0.05 * 2_020_000 + 0.06 * 980_000) / 3_000_000)
    [summary] = domain.summary(positions).to_dict("records")
    assert summary["market_value"] == sectors["market_value"].sum()  # the tiles and the chart add up to one total
    assert summary["dv01"] == pytest.approx(sectors["dv01"].sum())
    assert summary["duration"] == pytest.approx((6 * 2_020_000 + 4 * 980_000 + 1 * 5_000_000) / 8_000_000)


def test_no_positions_sum_to_nothing_with_no_yield() -> None:
    [summary] = domain.summary(domain.shown(domain.positions(_moving(), date(2026, 11, 12)), "Municipals", "")).to_dict("records")
    assert (summary["holdings"], summary["market_value"], math.isnan(summary["ytw"])) == (0.0, 0.0, True)


def _moving() -> pd.DataFrame:
    holdings = _holdings()
    return holdings.assign(
        rating=["A", "BBB", "AAA"],
        coupon=[0.05, 0.06, 0.04],
        maturity=["2032-11-12", "2028-11-12", "2027-05-12"],
        prior_price=[100.5, 98.2, 100.0],
        prior_ytw=[0.0508, 0.0596, 0.0401],
        curve_change=[-0.0002, 0.0001, 0.0001],
    )


def test_a_days_pnl_is_its_carry_rates_spread_and_residual_exactly() -> None:
    found = domain.positions(_moving(), date(2026, 11, 12))
    assert found["pnl"].tolist() == pytest.approx((found["carry"] + found["rates"] + found["spread"] + found["residual"]).tolist())
    first = found.iloc[0]
    assert first["carry"] == pytest.approx(2_000_000 * 0.05 / 365)
    assert first["rates"] == pytest.approx(-6.0 * -0.0002 * 2_010_000)  # the curve fell 2 bp, so a holding gained
    assert first["years"] == pytest.approx(6.0, abs=0.01)
    [explained] = [domain.explained(domain.summary(found))]
    assert explained["factor"].tolist() == ["Carry", "Rates", "Spread", "Residual"]
    assert explained["pnl"].sum() == pytest.approx(found["pnl"].sum())


def test_ratings_and_maturities_keep_the_desks_order() -> None:
    found = domain.positions(_moving(), date(2026, 11, 12))
    assert domain.by_rating(found)["rating"].tolist() == ["AAA", "A", "BBB"]
    assert domain.by_maturity(found)["bucket"].tolist() == ["0-2y", "2-5y", "5-10y"]
    issuers = domain.by_issuer(found)
    assert issuers["issuer"].tolist() == ["US Treasury", "Contoso Financial", "Northwind Energy"]
    assert issuers["sector"].tolist() == ["Treasury", "Corporate", "Corporate"]


def test_the_largest_moves_are_by_size_gain_or_loss() -> None:
    found = domain.positions(_moving(), date(2026, 11, 12))
    moved = domain.movers(found)
    assert moved["pnl"].abs().tolist() == sorted(found["pnl"].abs().tolist(), reverse=True)


def test_a_curves_moves_are_its_close_less_each_earlier_one() -> None:
    curve = pd.DataFrame(
        {"close": ["As of", "Prior day", "One week before", "One month before"] * 2, "tenor": ["2Y"] * 4 + ["10Y"] * 4, "ytw": [0.0355, 0.0353, 0.0350, 0.0340, 0.0408, 0.0410, 0.0412, 0.0405]}
    )
    moves = domain.curve_moves(curve)
    assert moves["tenor"].tolist() == ["2Y", "10Y"]
    assert moves["day"].tolist() == pytest.approx([0.0002, -0.0002])
    assert moves["month"].tolist() == pytest.approx([0.0015, 0.0003])
