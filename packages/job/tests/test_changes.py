from datetime import UTC, date, datetime

import pandas as pd
import pytest
from builders import DAY, Price, prices

import basis.job as etl
from basis.core.tables import Changes, Delete, Range
from basis.job.changes import Planned, build, plan
from basis.job.outputs import FrameWrite, Target, Write


def target(write: Write, key: tuple[str, ...] | None = ("day", "security_id")) -> Target:
    return Target(Price, "warehouse", "prices", write, key)


def planned(write: Write, value: pd.DataFrame | Changes, *, key: tuple[str, ...] | None = ("day", "security_id"), interval: etl.Interval | None = DAY) -> Planned:
    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table="prices", write=write, key=key)

    return plan(Outputs(prices=value), interval)["prices"]


@pytest.mark.parametrize(
    ("write", "operations", "rerun_safe"),
    [
        (etl.Append(), {"insert"}, False),
        (etl.Replace(), {"insert"}, True),
        (etl.ReplaceWhere(interval="day"), {"insert"}, True),
        (etl.ReplaceWhere(matching=("security_id",)), {"insert", "delete_matching"}, True),
        (etl.Upsert(), {"upsert"}, True),
        (etl.InsertNew(), {"insert_new"}, True),
    ],
)
def test_each_write_mode_builds_its_operations(write: FrameWrite, operations: set[str], *, rerun_safe: bool) -> None:
    changes = build(target(write), prices({}), DAY)
    assert set(changes.frames()) == operations
    assert changes.rerun_safe is rerun_safe


def test_replace_where_an_interval_deletes_the_runs_interval() -> None:
    changes = build(target(etl.ReplaceWhere(interval="day")), prices({}), DAY)
    assert changes.delete is not None
    assert changes.delete.within == Range("day", DAY.start, DAY.end)


def test_replace_where_an_interval_needs_a_run_with_one() -> None:
    assert planned(etl.ReplaceWhere(interval="day"), prices({}), interval=None).reasons == ["prices: ReplaceWhere(interval='day') replaces the run's interval, and this run has none: give it one"]


@pytest.mark.parametrize(("day", "covered"), [(date(2026, 11, 5), True), (date(2026, 11, 6), False), (date(2026, 11, 4), False)])
def test_an_inserted_row_outside_the_replaced_interval_is_refused(day: date, *, covered: bool) -> None:
    reasons = planned(etl.ReplaceWhere(interval="day"), prices({"day": day})).reasons
    assert (reasons == []) is covered
    assert covered or "1 inserted rows are outside what the delete removes" in reasons[0]


def test_a_manual_insert_outside_its_matching_delete_is_refused() -> None:
    changes = Changes(delete=Delete(matching=pd.DataFrame({"security_id": ["S1"]})), insert=prices({"security_id": "S1"}, {"security_id": "S2"}))
    [reason] = planned(etl.Manual(), changes).reasons
    assert reason.startswith("prices: 1 inserted rows are outside what the delete removes, so a rerun would insert them twice")
    assert "security_id='S2'" in reason


def test_a_key_repeated_across_operations_is_refused() -> None:
    changes = Changes(upsert=prices({}), insert_new=prices({}))
    assert planned(etl.Manual(), changes).reasons == ["prices: 1 keys appear more than once in what the run writes, such as day=datetime.date(2026, 11, 5), security_id='S1'"]


def test_a_delete_matching_a_null_is_refused() -> None:
    changes = Changes(delete=Delete(matching=pd.DataFrame({"note": pd.Series([None], dtype="string[pyarrow]")})))
    assert planned(etl.Manual(), changes).reasons == ["prices: the delete matches on 1 nulls, which match nothing"]


def test_every_frame_is_conformed_to_the_row() -> None:
    bad = prices({}).assign(price=[float("inf")])
    reasons = planned(etl.Append(), bad).reasons
    assert len(reasons) == 1
    assert "insert: price: infinite values" in reasons[0]


def test_replace_where_matching_finds_its_columns_as_conform_does_and_says_why_it_cannot() -> None:
    renamed = planned(etl.ReplaceWhere(matching=("security_id",)), prices({}).rename(columns={"security_id": "SECURITY_ID"}))
    assert renamed.reasons == []
    assert renamed.changes is not None
    assert renamed.changes.delete is not None
    assert renamed.changes.delete.matching is not None
    assert renamed.changes.delete.matching["security_id"].tolist() == ["S1"]
    [missing] = planned(etl.ReplaceWhere(matching=("security_id",)), prices({}).drop(columns=["security_id"])).reasons
    assert missing == "prices: the frame ReplaceWhere(matching=...) deletes by does not match its row:\n  - missing columns: security_id"
    [indexed] = planned(etl.ReplaceWhere(matching=("security_id",)), prices({}).set_index("security_id")).reasons
    assert "missing columns: security_id (security_id in the frame's index: reset_index() it, or group with as_index=False)" in indexed


def test_an_empty_replace_deletes_every_row_and_inserts_none() -> None:
    changes = planned(etl.Replace(), Price.empty()).changes
    assert changes is not None
    assert changes.delete is not None
    assert (changes.delete.within, changes.delete.matching) == (None, None)


@pytest.mark.parametrize(
    ("write", "key", "message"),
    [
        (etl.Upsert(), None, "Upsert\\(\\) needs the table's key"),
        (etl.InsertNew(), None, "InsertNew\\(\\) needs the table's key"),
        (etl.Append(), ("note",), "may not be null"),
        (etl.Append(), ("missing",), "names columns its row does not have"),
        (etl.Append(), (), "names at least one column"),
        (etl.ReplaceWhere(interval="price"), None, "needs a date or datetime column"),
        (etl.ReplaceWhere(matching=("note",)), None, "null matches nothing"),
    ],
)
def test_a_table_declaration_that_cannot_be_written_is_refused(write: FrameWrite, key: tuple[str, ...] | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        etl.Table(Price, database="warehouse", table="prices", write=write, key=key)


def test_a_table_name_is_never_quoted_or_injected() -> None:
    with pytest.raises(ValueError, match=r"a table is TABLE or SCHEMA\.TABLE"):
        etl.Table(Price, database="warehouse", table="prices; DROP TABLE x", write=etl.Append())


def test_outputs_take_every_declared_table_and_nothing_else() -> None:
    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table="prices", write=etl.Append())

    with pytest.raises(TypeError, match="missing outputs"):
        Outputs()
    with pytest.raises(TypeError, match="declares no outputs named"):
        Outputs(prices=prices({}), other=prices({}))
    with pytest.raises(TypeError, match="its value is a DataFrame, not a Changes"):
        Outputs(prices=Changes())


def test_a_datetime_its_zone_shows_twice_is_refused_since_a_naive_value_would_name_two_instants() -> None:
    first, second = datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # both 1:30 in New York
    found = planned(etl.Upsert(), prices({"security_id": "S1", "booked_at": first}, {"security_id": "S2", "booked_at": second}))
    assert found.reasons == [
        (
            "prices: booked_at: 2 values are at a time America/New_York shows twice as its clocks go back, such as 2026-11-01 05:30:00+00:00,"
            " which a naive value cannot tell apart: store the column in UTC, as Annotated[datetime, etl.Zone('UTC')], or leave those rows out"
        )
    ]


def test_a_delete_range_on_a_zoned_column_whose_end_its_zone_shows_twice_is_refused() -> None:
    hour = etl.Interval.parse("2026-11-01T05:00Z..2026-11-01T06:00Z")  # 1:00 to 1:00 in New York, which deletes nothing
    found = planned(etl.ReplaceWhere(interval="booked_at"), prices({"booked_at": datetime(2026, 11, 1, 5, 30, tzinfo=UTC)}), interval=hour)
    assert any("the delete's range on booked_at ends at 2026-11-01 06:00:00+00:00, 01:00 in America/New_York, which shows that time twice" in reason for reason in found.reasons)


def test_a_delete_range_across_the_repeated_hour_whose_ends_are_outside_it_is_sound() -> None:
    day = etl.Interval.parse("2026-11-01T04:00Z..2026-11-02T05:00Z")
    found = planned(etl.ReplaceWhere(interval="booked_at"), prices({"booked_at": datetime(2026, 11, 1, 4, 30, tzinfo=UTC)}), interval=day)
    assert found.reasons == []
