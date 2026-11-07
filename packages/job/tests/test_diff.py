from decimal import Decimal

import pandas as pd
from builders import Price, prices

import basis.job as etl
from basis.core.tables import Changes
from basis.job.diff import OperationDiff, diff, diff_frames
from basis.job.outputs import Target


def test_keyed_rows_are_matched_by_key_and_changes_counted_by_column() -> None:
    before = prices({"security_id": "S1"}, {"security_id": "S2"})
    after = prices({"security_id": "S1", "price": Decimal(101)}, {"security_id": "S3"})
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


def test_identical_changes_have_no_differences() -> None:
    target = Target(Price, "warehouse", "prices", etl.Upsert(), ("day", "security_id"))
    assert diff("prices", target, Changes(upsert=prices({})), Changes(upsert=prices({}))) == []
