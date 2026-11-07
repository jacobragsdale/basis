import pytest

from basis.core.databases import open_database
from basis.core.settings import GivenSettings

MSSQL = {"kind": "mssql", "connection_string": "Server=nowhere", "isolation": "snapshot", "timeout_s": 5, "max_rows": 10}
ORACLE = {"kind": "oracle", "dsn": "nowhere:1521/X", "user": "reader", "password": "x", "mode": "thin", "isolation": "read_only", "timeout_s": 5, "max_rows": 10}


@pytest.mark.parametrize(
    ("database", "write", "writable"),
    [
        (MSSQL, None, False),
        (MSSQL, {"connection_string": "Server=nowhere"}, False),
        (MSSQL, {"connection_string": "Server=nowhere", "ledger": "etl.publishes"}, True),
        (ORACLE, {"user": "writer", "password": "x"}, False),
        (ORACLE, {"user": "writer", "password": "x", "ledger": "ETL.PUBLISHES"}, True),
    ],
    ids=["mssql-reads-only", "mssql-without-a-ledger", "mssql-publishes", "oracle-without-a-ledger", "oracle-publishes"],
)
def test_a_run_publishes_only_through_a_write_login_with_a_ledger(database: dict[str, object], write: dict[str, object] | None, writable: bool) -> None:  # noqa: FBT001 -- a parameter of the table
    found = open_database(GivenSettings(databases={"warehouse": database | {"write": write}}), "warehouse")
    assert found.writable is writable


@pytest.mark.parametrize("database", [MSSQL | {"write": {"connection_string": "Server=nowhere"}}, ORACLE | {"write": {"user": "writer", "password": "x"}}], ids=["mssql", "oracle"])
def test_a_publish_without_a_ledger_names_the_setting(database: dict[str, object]) -> None:
    found = open_database(GivenSettings(databases={"warehouse": database}), "warehouse")
    with pytest.raises(LookupError, match="has no ledger, which a publish writes in its transaction: set BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER"), found.writing():
        pass
