import functools
import operator

import numpy as np
import pandas as pd
import pyarrow as pa
from builders import Price, prices

import basis.job as etl
from basis.core.tables import Changes
from basis.job.cli import difference
from basis.job.diff import OperationDiff, diff, diff_frames
from basis.job.outputs import Target


def test_keyed_rows_are_matched_by_key_and_changes_counted_by_column() -> None:
    before = prices({"security_id": "S1"}, {"security_id": "S2"})
    after = prices({"security_id": "S1", "price": 101.0}, {"security_id": "S3"})
    assert diff_frames("prices", "insert", before, after, ("day", "security_id")) == OperationDiff("prices", "insert", added=1, removed=1, changed=1, columns={"price": 1})


def test_rows_without_a_key_compare_as_a_multiset() -> None:
    row = prices({})
    assert diff_frames("prices", "insert", row, pd.concat([row, row]), None) == OperationDiff("prices", "insert", added=1, removed=0, changed=0)


def test_two_nulls_are_equal_and_a_null_against_a_value_is_a_change() -> None:
    before = prices({"security_id": "S1"}, {"security_id": "S2"}, {"security_id": "S3", "note": "x"})
    after = prices({"security_id": "S1"}, {"security_id": "S2", "note": "y"}, {"security_id": "S3"})
    assert diff_frames("prices", "upsert", before, after, ("day", "security_id")) == OperationDiff("prices", "upsert", added=0, removed=0, changed=2, columns={"note": 2})
    assert diff_frames("prices", "insert", before, after, None) == OperationDiff("prices", "insert", added=2, removed=2, changed=0)
    assert diff_frames("prices", "insert", after.iloc[::-1], after, None).same


def test_a_column_added_removed_or_retyped_between_runs_is_a_change_of_its_own_and_the_rest_compare() -> None:
    before = prices({"security_id": "S1"}, {"security_id": "S2", "price": 5.0})
    after = before.drop(columns=["note"]).assign(price=before["price"].astype(str).astype(pd.ArrowDtype(pa.string())), extra=pd.Series([1.0, 2.0]), booked_at=before["booked_at"])
    after.loc[1, "booked_at"] = pd.Timestamp("2026-11-05 14:30", tz="UTC")
    keyed = diff_frames("prices", "upsert", before, after, ("day", "security_id"))
    assert keyed == OperationDiff(
        "prices", "upsert", added=0, removed=0, changed=1, columns={"booked_at": 1},
        schema={"note": "removed", "price": "retyped from float64 to string", "extra": "added"},
    )  # fmt: skip
    assert not keyed.same
    assert difference(keyed).summary == "upsert: 1 changed (booked_at 1); note removed, price retyped from float64 to string, extra added"
    assert diff_frames("prices", "insert", before, after, None) == OperationDiff("prices", "insert", added=1, removed=1, changed=0, schema=keyed.schema)


def test_a_key_that_was_retyped_compares_the_rows_as_a_multiset_and_frames_with_nothing_shared_differ_in_every_row() -> None:
    before = prices({"security_id": "S1"})
    retyped = before.assign(day=pd.Series(["2026-11-05"], dtype="string[pyarrow]"))
    assert diff_frames("prices", "upsert", before, retyped, ("day", "security_id")) == OperationDiff(
        "prices", "upsert", added=0, removed=0, changed=0, schema={"day": "retyped from date32[day] to string"}
    )
    other = pd.DataFrame({"other": pd.Series([1], dtype="int64[pyarrow]")})
    assert diff_frames("prices", "insert", before, other, None).added == 1
    assert diff_frames("prices", "insert", before, other, None).removed == 1


def test_floats_that_agree_to_15_significant_digits_are_the_same_as_a_sum_taken_in_another_order_is() -> None:
    before = prices({"security_id": "S1", "price": 0.1 + 0.2}, {"security_id": "S2", "price": 1e20 + 1e4})
    reordered = prices({"security_id": "S1", "price": 0.3}, {"security_id": "S2", "price": 1e20})
    assert diff_frames("prices", "upsert", before, reordered, ("day", "security_id")).same
    assert diff_frames("prices", "insert", before, reordered, None).same
    assert diff_frames("prices", "upsert", before, prices({"security_id": "S1", "price": 0.3000000001}, {"security_id": "S2", "price": 1e20}), ("day", "security_id")).columns == {"price": 1}


def test_identical_changes_have_no_differences() -> None:
    target = Target(Price, "warehouse", "prices", etl.Upsert(), ("day", "security_id"))
    assert diff("prices", target, Changes(upsert=prices({})), Changes(upsert=prices({}))) == []


def test_sums_of_many_groups_taken_in_another_order_are_no_change() -> None:
    """Rounded to 15 digits, 553 of 20,000 such sums differed in their last digit, at a rounding boundary."""
    rng = np.random.default_rng(7)
    parts = rng.uniform(-1e6, 1e6, size=(20_000, 7))
    forward = [functools.reduce(operator.add, row) for row in parts.tolist()]  # left to right, as a database sums; Python's sum compensates
    backward = [functools.reduce(operator.add, reversed(row)) for row in parts.tolist()]
    assert sum(a != b for a, b in zip(forward, backward, strict=True)) > 0  # the orders differ in the last bit somewhere
    ids = [f"S{index}" for index in range(len(parts))]
    before = prices(*({"security_id": security, "price": value} for security, value in zip(ids, forward, strict=True)))
    after = prices(*({"security_id": security, "price": value} for security, value in zip(ids, backward, strict=True)))
    assert diff_frames("prices", "upsert", before, after, ("day", "security_id")).same


def test_a_change_in_a_float_beyond_the_tolerance_is_a_change() -> None:
    before, after = prices({"price": 1_000_000_000.00}), prices({"price": 1_000_000_000.01})
    found = diff_frames("prices", "upsert", before, after, ("day", "security_id"))
    assert (found.changed, found.columns) == (1, {"price": 1})
