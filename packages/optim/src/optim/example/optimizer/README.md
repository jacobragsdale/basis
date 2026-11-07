# Optimizer

The cvxpy problem a portfolio's decision solves.

| File | Holds | Change it to |
|---|---|---|
| `solve.py` | `solve(portfolio, params)`: build the problem and solve it | change how a portfolio is solved, or what it reports |
| `problem.py` | `build(portfolio, params)`: the variables, the objective and its `terms`, and the list of constraints | add a constraint to the problem, or a term to the objective |
| `constraints.py` | Each constraint, a function returning cvxpy constraints: trade sizes, cash, turnover and caps on weights | write a constraint |

A limit's number comes from the params, such as `max_issuer_weight`, or from the portfolio, such as its `sector_caps`,
which its mandates tighten; the constraint that holds the problem to it is cvxpy in `constraints.py`.

`uv run optim docs how-to/add-a-constraint` and `uv run optim docs how-to/change-the-objective` say more.
