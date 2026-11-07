"""Whether a decision may trade, the same checks for every model. A solver's optimum means it solved the problem it was
given, within its tolerances; it is not permission to trade. So `round_decision` first makes each quantity exact, a
whole number of its security's trading increment, and `review` then rebuilds the portfolio after the rounded trades from
the holdings and securities alone, and rejects the decision unless:

- every trade is in a known security, and every quantity is positive;
- no sale exceeds the holding;
- cash after the trades is not negative;
- the decision was solved;
- rounding moved no trade by more than the tolerance.

One tolerance applies, in weights of the portfolio's value. The solver proved its decision meets every constraint of
the model's problem, within its own tolerances, before rounding; the last check proves rounding changed nothing
material, so no model writes a constraint a second time to check it after rounding. A model that trades whole
increments always passes it. The model's `check` adds any reasons of its own."""

from collections.abc import Mapping

import pandas as pd

from optim.contract import Decision
from optim.datasets import unit_value

TOLERANCE = 1e-6  # in weights of the portfolio's value: $1 per $1mm


def round_decision(decision: Decision, holdings: pd.DataFrame, securities: pd.DataFrame) -> Decision:
    """Each quantity as a whole number of its security's increment, to the nearest, so a solver's 2.9999999 lots are 3,
    and a sale within half an increment of the holding as the holding itself, so an odd lot sells whole. A quantity that
    rounds to nothing is no trade. A trade in an unknown security is left as it is, for `review` to reject."""
    increments = securities.set_index("security_id")["increment"]
    held = holdings.groupby("security_id")["quantity"].sum()

    def rounded(quantities: Mapping[str, float], *, sale: bool) -> dict[str, float]:
        out = {}
        for security, quantity in quantities.items():
            if security not in increments.index:
                out[security] = quantity
                continue
            increment, holding = float(increments[security]), float(held.get(security, 0.0))
            exact = holding if sale and holding > 0 and abs(quantity - holding) < increment / 2 else round(quantity / increment) * increment
            if exact != 0:
                out[security] = exact
        return out

    return Decision.model_validate(decision.model_dump() | {"buy": rounded(decision.buy, sale=False), "sell": rounded(decision.sell, sale=True)})


def review(decision: Decision, decided: Decision, holdings: pd.DataFrame, securities: pd.DataFrame) -> tuple[list[str], dict[str, float]]:
    """Why the rounded `decision` may not trade, and the portfolio's numbers after it: its `value` before trading,
    `cash` after, and `turnover`, the value bought plus sold. `decided` is the decision as the model decided it."""
    unit = pd.Series(unit_value(securities).to_numpy(), index=securities["security_id"])
    held = holdings.groupby("security_id")["quantity"].sum()
    in_cash = held.index.isin(securities.loc[securities["asset_class"] == "cash", "security_id"])
    value, cash = _worth(held, unit), _worth(held[in_cash], unit)
    buy, sell = pd.Series(decision.buy, dtype=float), pd.Series(decision.sell, dtype=float)
    tolerance = TOLERANCE * max(value, 0.0)
    reasons = []
    if holdings.empty:
        reasons.append("it has no holdings: is its id right?")
    if not decision.solved:
        reasons.append(f"not solved: {decision.solver.stop if decision.solver else 'the model says so'}")
    if unknown := sorted(buy.index.union(sell.index).difference(unit.index)):
        reasons.append(f"trades securities that are not loaded: {unknown[:5]}")
    for side, quantities in (("buy", buy), ("sell", sell)):
        if bad := sorted(quantities.index[~(quantities > 0)]):
            reasons.append(f"{side}s quantities that are not positive: {bad[:5]}")
    over = (sell - held.reindex(sell.index, fill_value=0.0)) * unit.reindex(sell.index, fill_value=0.0)
    reasons += [f"sells {sell[s]:,.2f} of {s}, more than its holding of {held.get(s, 0.0):,.2f}" for s in sell.index[over > tolerance]]
    for side, rounded, raw in (("buy", buy, decided.buy), ("sell", sell, decided.sell)):
        both = pd.DataFrame({"decided": pd.Series(raw, dtype=float), "rounded": rounded}).fillna(0.0)
        moved = (both["rounded"] - both["decided"]).abs() * unit.reindex(both.index, fill_value=0.0) > tolerance
        reasons += [f"rounding moved its {side} of {s} from {row.decided:,.2f} to {row.rounded:,.2f}: decide whole increments" for s, row in both[moved].iterrows()]
    bought, sold = _worth(buy, unit), _worth(sell, unit)
    if (after := cash + sold - bought) < -tolerance:
        reasons.append(f"leaves cash of {after:,.2f}: it spends more than it has")
    return reasons, {"value": value, "cash": after, "turnover": bought + sold}


def _worth(quantities: pd.Series, unit: pd.Series) -> float:
    """Quantities by security at their unit values, summed; an unknown security is worth nothing here."""
    worth: float = (quantities * unit.reindex(quantities.index, fill_value=0.0)).sum()
    return worth
