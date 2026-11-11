import re

import pytest

from basis.core.databases.base import LEDGER_COLUMNS
from basis.core.ddl import ledger, ledger_tables
from basis.core.destination import Dialect


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_ledgers_columns_are_those_a_publish_writes_in_its_order(dialect: Dialect) -> None:
    first = ledger("etl.publishes", dialect).split(";\n")[0]
    columns = re.findall(r"^    (\w+) ", first, re.MULTILINE)

    assert columns == [*LEDGER_COLUMNS, "CONSTRAINT"]


def test_oracles_ledger_has_a_table_of_its_publishes_locks_beside_it() -> None:
    assert ledger_tables("etl.publishes", "oracle") == ("etl.publishes", "etl.publishes_locks")
    assert ledger_tables("etl.publishes", "mssql") == ("etl.publishes",)
    assert "CREATE TABLE etl.publishes_locks (" in ledger("etl.publishes", "oracle")
    assert "_locks" not in ledger("etl.publishes", "mssql")
