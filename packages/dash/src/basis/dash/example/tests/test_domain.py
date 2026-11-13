"""The holdings' rules on frames, with no app, no fakes and no basis: what a job could run as it stands."""

import math

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
    found = domain.positions(_holdings())
    assert found["market_value"].tolist() == [2_020_000.0, 980_000.0, 5_000_000.0]
    assert found["dv01"].tolist() == pytest.approx([1_212.0, 392.0, 500.0])
    assert found["weight"].sum() == pytest.approx(1.0)


def test_a_book_and_a_search_keep_their_positions_and_weigh_them_again() -> None:
    positions = domain.positions(_holdings())
    credit = domain.shown(positions, "Credit", "")
    assert credit["cusip"].tolist() == ["AAA111AA1", "BBB222BB2"]
    assert credit["weight"].tolist() == pytest.approx([2_020_000 / 3_000_000, 980_000 / 3_000_000])
    assert domain.shown(positions, "", "northwind")["cusip"].tolist() == ["BBB222BB2"]  # an issuer, ignoring case
    assert domain.shown(positions, "", "ccc333")["cusip"].tolist() == ["CCC333CC3"]  # a CUSIP
    assert domain.shown(positions, "Municipals", "").empty


def test_sectors_and_the_summary_weigh_yield_and_duration_by_market_value() -> None:
    positions = domain.positions(_holdings())
    sectors = domain.by_sector(positions)
    assert sectors["sector"].tolist() == ["Treasury", "Corporate"]  # the largest first
    corporate = sectors.set_index("sector").loc["Corporate"]
    assert corporate["ytw"] == pytest.approx((0.05 * 2_020_000 + 0.06 * 980_000) / 3_000_000)
    [summary] = domain.summary(positions).to_dict("records")
    assert summary["market_value"] == sectors["market_value"].sum()  # the tiles and the chart add up to one total
    assert summary["dv01"] == pytest.approx(sectors["dv01"].sum())
    assert summary["duration"] == pytest.approx((6 * 2_020_000 + 4 * 980_000 + 1 * 5_000_000) / 8_000_000)


def test_no_positions_sum_to_nothing_with_no_yield() -> None:
    [summary] = domain.summary(domain.shown(domain.positions(_holdings()), "Municipals", "")).to_dict("records")
    assert (summary["holdings"], summary["market_value"], math.isnan(summary["ytw"])) == (0.0, 0.0, True)
