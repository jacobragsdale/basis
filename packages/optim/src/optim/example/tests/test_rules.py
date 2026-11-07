"""The model's rules, on a few bonds each and no solver."""

from dataclasses import replace

import pandas as pd
import pytest
from optim.testing import frame

from optim.example import model
from optim.example.datasets.controls import CONTROLS
from optim.example.datasets.mandates import MANDATES
from optim.example.datasets.portfolios import PORTFOLIOS
from optim.example.rules import controls, credit, mandates, screens
from optim.example.tests.builders import MARKET, params, portfolio

CONTROL = {"portfolio_id": "P1", "control_id": "R1", "reason": "test"}


def flags(found: model.Portfolio) -> pd.DataFrame:
    return found.universe.set_index("security_id")[["held", "buyable", "sellable", "must_sell"]]


def test_a_portfolio_may_buy_every_bond_with_a_signal_and_sell_what_it_holds() -> None:
    """C has no signal: it is held, so it is in the universe, but it cannot be bought."""
    found = portfolio({"security_id": "A"}, {"security_id": "C", "held": 5000.0, "expected_return": None})

    assert flags(found).to_dict("index") == {"C": {"held": 5000.0, "buyable": False, "sellable": True, "must_sell": False}, "A": {"held": 0.0, "buyable": True, "sellable": False, "must_sell": False}}


def test_controls_apply_and_one_naming_no_security_applies_to_every_one() -> None:
    rows = frame(CONTROLS, {"security_id": "A", "action": "must_sell"}, {"control_id": "R2", "security_id": None, "action": "no_buy"}, default=CONTROL)

    found = controls.apply(portfolio({"security_id": "A", "held": 5000.0}, {"security_id": "B"}, controls=rows), MARKET, params())

    assert flags(found).to_dict("index") == {"A": {"held": 5000.0, "buyable": False, "sellable": True, "must_sell": True}, "B": {"held": 0.0, "buyable": False, "sellable": False, "must_sell": False}}


def test_controls_that_both_require_and_forbid_a_sale_fail_the_portfolio() -> None:
    rows = frame(CONTROLS, {"security_id": "A", "action": "must_sell"}, {"control_id": "R2", "security_id": "A", "action": "hold"}, default=CONTROL)

    with pytest.raises(ValueError, match=r"both require and forbid selling \['A'\]"):
        controls.apply(portfolio({"security_id": "A", "held": 5000.0}, controls=rows), MARKET, params())


def test_the_rating_floor_is_the_params_or_the_portfolios_own_whichever_is_stricter() -> None:
    bonds = ({"security_id": "A", "rating": "AA"}, {"security_id": "B", "rating": "A"})
    own = frame(PORTFOLIOS, {"portfolio_id": "P1", "min_rating": "AA-"})

    def buyable(found: model.Portfolio) -> dict[str, bool]:
        return dict(zip(found.universe["security_id"], found.universe["buyable"], strict=True))

    assert buyable(credit.min_rating(portfolio(*bonds), MARKET, params(min_rating="A-"))) == {"A": True, "B": True}
    assert buyable(credit.min_rating(portfolio(*bonds, portfolios=own), MARKET, params(min_rating="A-"))) == {"A": True, "B": False}


def test_an_excluded_sector_leaves_the_universe_but_a_holding_in_it_may_still_be_sold() -> None:
    found = portfolio({"security_id": "A", "sector": "Energy"}, {"security_id": "B", "sector": "Energy", "held": 5000.0}, {"security_id": "C"})

    after = screens.exclude_sectors(found, MARKET, params(exclude_sectors=["Energy"]))

    assert flags(after).to_dict("index") == {"B": {"held": 5000.0, "buyable": False, "sellable": True, "must_sell": False}, "C": {"held": 0.0, "buyable": True, "sellable": False, "must_sell": False}}


def test_a_portfolios_mandates_cap_sectors_and_the_tighter_of_two_caps_applies() -> None:
    rows = frame(
        MANDATES,
        {"mandate_id": "M1", "sector": "Financials", "max_weight": 0.2},
        {"mandate_id": "M2", "sector": "Energy", "max_weight": 0.05},
        {"mandate_id": "M3", "sector": "Utilities", "max_weight": 0.3},
        default={"portfolio_id": "P1"},
    )
    found = replace(portfolio({"security_id": "A"}, mandates=rows), sector_caps={"Financials": 0.1, "Energy": 0.1})

    after = mandates.sector_caps(found, MARKET, params())

    assert after.sector_caps == {"Financials": 0.1, "Energy": 0.05, "Utilities": 0.3}
