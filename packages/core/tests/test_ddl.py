import re

import pytest

from basis.core.databases.base import LEDGER_COLUMNS
from basis.core.ddl import ledger
from basis.core.destination import Dialect


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_ledgers_columns_are_those_a_publish_writes_in_its_order(dialect: Dialect) -> None:
    columns = re.findall(r"^    (\w+) ", ledger("etl.publishes", dialect), re.MULTILINE)

    assert columns == [*LEDGER_COLUMNS, "CONSTRAINT"]
