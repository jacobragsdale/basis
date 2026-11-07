# /// script
# requires-python = ">=3.14"
# dependencies = ["pyarrow>=26.0.1"]
# ///
"""Records a run of optim's example model as plain JSON, and compares a run with this oracle's record.

    uv run oracles/optim-0.2.0/record.py record SNAPSHOT OUT
    uv run oracles/optim-0.2.0/record.py compare SNAPSHOT

SNAPSHOT is a run's folder as optim 0.2.0 saves it: `run.json` beside `decisions.parquet`, `trades.parquet`,
`reasons.parquet` and `metrics.parquet`. `record` writes `OUT/decisions.json` and `OUT/details.json`. `compare` records
SNAPSHOT and compares it with the two files beside this script; it exits 1, naming each difference, unless they match. A
run saved in another layout, such as the ported optim's, needs only a `load` of its own.

`decisions.json` must match exactly: the as-of date, the portfolios in the order they were decided, whether each was
accepted, the reasons it was not, and every trade that rounding left, by security and side. `details.json` must match
within TOLERANCE: each decision's value, cash, turnover, objective and metrics, its solver and how the solve stopped, and
every trade as the model decided it before rounding. Nothing else is compared: times, ids, paths, and the solver's gap,
node and iteration counts, which change with its version."""

import difflib
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type Row = dict[str, Json]

ORACLE = Path(__file__).parent
TOLERANCE = 1e-9
"""Relative, and absolute for numbers near zero."""
DETAILS = ("value", "cash", "turnover", "solved", "objective", "solver", "solver_status", "stop")
"""The columns of a decision that `details.json` keeps."""


@dataclass(frozen=True)
class Tables:
    """A run's result tables, a row per decision, trade, reason and metric, as optim 0.2.0's snapshot holds them."""

    as_of_date: str
    decisions: list[Row]
    trades: list[Row]
    reasons: list[Row]
    metrics: list[Row]


def load(snapshot: Path) -> Tables:
    info = _json(json.loads((snapshot / "run.json").read_bytes()))
    if not isinstance(info, dict) or not isinstance(as_of_date := info.get("as_of_date"), str):
        msg = f"{snapshot / 'run.json'} has no as_of_date: it is not a snapshot of optim 0.2.0"
        raise TypeError(msg)
    tables = {name: [_row(row) for row in pq.read_table(snapshot / f"{name}.parquet").to_pylist()] for name in ("decisions", "trades", "reasons", "metrics")}
    return Tables(as_of_date=as_of_date, **tables)


def record(tables: Tables) -> tuple[Json, Json]:
    """`decisions.json` and `details.json` for one run."""
    reasons: dict[str, list[str]] = {}
    for row in tables.reasons:
        reasons.setdefault(_text(row["portfolio_id"]), []).append(_text(row["reason"]))
    trades: dict[str, list[Row]] = {}
    for row in sorted(tables.trades, key=lambda row: (_text(row["security_id"]), _text(row["side"]))):
        trades.setdefault(_text(row["portfolio_id"]), []).append(row)
    metrics: dict[str, dict[str, Json]] = {}
    for row in sorted(tables.metrics, key=lambda row: _text(row["metric"])):
        metrics.setdefault(_text(row["portfolio_id"]), {})[_text(row["metric"])] = _number(row["value"])
    decisions: dict[str, Json] = {}
    details: dict[str, Json] = {}
    for row in tables.decisions:
        portfolio_id, its = _text(row["portfolio_id"]), trades.get(_text(row["portfolio_id"]), [])
        rounded: list[Json] = [{"security_id": trade["security_id"], "side": trade["side"], "quantity": _number(trade["quantity"])} for trade in its if _number(trade["quantity"]) != 0]
        decided: list[Json] = [{"security_id": trade["security_id"], "side": trade["side"], "quantity": _number(trade["quantity"]), "decided": trade["decided"]} for trade in its]
        why: list[Json] = [*sorted(reasons.get(portfolio_id, []))]
        decisions[portfolio_id] = {"status": row["status"], "reasons": why, "trades": rounded}
        details[portfolio_id] = {**{key: row[key] for key in DETAILS}, "metrics": metrics.get(portfolio_id, {}), "trades": decided}
    return {"as_of_date": tables.as_of_date, "portfolios": decisions}, {"portfolios": details}


def compare(snapshot: Path) -> list[str]:
    """Every difference between SNAPSHOT's record and the oracle's, as lines to print."""
    decisions, details = record(load(snapshot))
    want, got = (ORACLE / "decisions.json").read_text(encoding="utf-8"), _dump(decisions)
    problems = list(difflib.unified_diff(want.splitlines(), got.splitlines(), "the oracle's decisions.json", f"{snapshot}'s decisions", lineterm=""))
    return problems + _differences("details.json", _json(json.loads((ORACLE / "details.json").read_bytes())), details)


def _differences(where: str, want: Json, got: Json) -> list[str]:
    match want, got:
        case dict() as want_dict, dict() as got_dict:
            lines = [f"{where}.{key}: only in {'the oracle' if key in want_dict else 'this run'}" for key in dict.fromkeys([*want_dict, *got_dict]) if (key in want_dict) != (key in got_dict)]
            return lines + [line for key in want_dict if key in got_dict for line in _differences(f"{where}.{key}", want_dict[key], got_dict[key])]
        case list() as want_list, list() as got_list if len(want_list) == len(got_list):
            return [line for index, (one, other) in enumerate(zip(want_list, got_list, strict=True)) for line in _differences(f"{where}[{index}]", one, other)]
        case (bool(), _) | (_, bool()):  # a bool is an int to Python, but True is not 1.0 in a record
            return [] if type(want) is type(got) and want == got else [f"{where}: {want!r} in the oracle, {got!r} in this run"]
        case int() | float() as want_number, int() | float() as got_number:
            return [] if math.isclose(want_number, got_number, rel_tol=TOLERANCE, abs_tol=TOLERANCE) else [f"{where}: {want_number!r} in the oracle, {got_number!r} in this run"]
        case _:
            return [] if want == got else [f"{where}: {want!r} in the oracle, {got!r} in this run"]


def _dump(value: Json) -> str:
    return json.dumps(value, indent=2, allow_nan=False) + "\n"


def _json(value: object) -> Json:
    match value:
        case None | bool() | int() | float() | str():
            return value
        case list():
            return [_json(item) for item in value]
        case dict():
            return {_text(key): _json(item) for key, item in value.items()}
        case _:
            msg = f"{value!r} is not a JSON value"
            raise TypeError(msg)


def _row(value: object) -> Row:
    if not isinstance(row := _json(value), dict):
        msg = f"{value!r} is not a row"
        raise TypeError(msg)
    return row


def _text(value: object) -> str:
    if not isinstance(value, str):
        msg = f"{value!r} is not text"
        raise TypeError(msg)
    return value


def _number(value: object) -> float:
    """optim's numbers are floats; a whole number read as an int is the same number, and is written as a float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{value!r} is not a number"
        raise TypeError(msg)
    return float(value)


def main(argv: Sequence[str]) -> int:
    match argv:
        case ["record", snapshot, out]:
            decisions, details = record(load(Path(snapshot)))
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / "decisions.json").write_text(_dump(decisions), encoding="utf-8")
            (Path(out) / "details.json").write_text(_dump(details), encoding="utf-8")
            return 0
        case ["compare", snapshot]:
            problems = compare(Path(snapshot))
            sys.stdout.write("".join(f"{line}\n" for line in problems) if problems else "matches the oracle\n")
            return 1 if problems else 0
        case _:
            sys.stderr.write(f"{__doc__}\n")
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
