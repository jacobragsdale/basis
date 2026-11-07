"""The portfolio's trading controls, which bind whatever the params say."""

from dataclasses import replace

import numpy as np

from optim.example import model


def apply(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """`no_buy`, `no_sell`, `hold` and `must_sell`, from the portfolio's controls. A control naming no security applies
    to every one. Controls that both require and forbid a sale fail the portfolio, quoting both: the model does not
    choose between them."""
    universe, controls = portfolio.universe, portfolio.data.controls

    def applies(*actions: str) -> np.ndarray:
        these = controls[controls["action"].isin(actions)]
        return universe["security_id"].isin(these["security_id"].dropna()).to_numpy() | bool(these["security_id"].isna().any())

    held = universe["sellable"].to_numpy()
    must, blocked = applies("must_sell") & held, applies("no_sell", "hold") & held
    if conflict := sorted(set(universe.loc[must & blocked, "security_id"])):
        involved = controls[controls["security_id"].isin(conflict) | controls["security_id"].isna()]
        quoted = "; ".join(f"{c.control_id} {c.action} {c.security_id if isinstance(c.security_id, str) else 'every security'} ({c.reason})" for c in involved.itertuples())
        msg = f"controls both require and forbid selling {conflict[:5]}: {quoted}"
        raise ValueError(msg)
    flags = {"buyable": universe["buyable"] & ~applies("no_buy", "hold", "must_sell"), "sellable": held & ~blocked, "must_sell": must}
    return replace(portfolio, universe=universe.assign(**flags))
