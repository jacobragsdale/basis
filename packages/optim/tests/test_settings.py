"""Settings are basis-core's, with optim's block, `BASIS_OPTIM__*`, beside them: each database optim reads needs its
limits there, and a variable that optim 0.2.0 read is refused, naming what replaced it."""

import re
from datetime import date

import pytest

from basis.core.settings import SettingsError
from optim.context import open_context
from optim.datasets import HOLDINGS
from optim.settings import Settings, refuse_renamed

BOOKS = {"kind": "mssql", "connection_string": "Server=books", "isolation": "snapshot", "timeout_s": 60, "max_rows": 1000}
ANALYTICS = {"kind": "oracle", "dsn": "analytics:1521/X", "user": "reader", "password": "x", "mode": "thin", "isolation": "read_only", "timeout_s": 60, "max_rows": 1000}


@pytest.mark.parametrize(
    ("name", "renamed"),
    [
        ("OPTIM_SNAPSHOT_ROOT", "BASIS_SNAPSHOT_ROOT"),
        ("OPTIM_LOG_FORMAT", "BASIS_LOG_FORMAT"),
        ("OPTIM_MSSQL__CONNECTION_STRING", "BASIS_DATABASES__MSSQL__CONNECTION_STRING"),
        ("OPTIM_MSSQL__MAX_CONCURRENT", "BASIS_OPTIM__DATABASES__MSSQL__MAX_CONCURRENT"),
        ("OPTIM_REST__URL", "BASIS_OPTIM__REST__URL"),
        ("OPTIM_ORDERS__CONNECTION_STRING", "the orders database's BASIS_DATABASES__NAME__WRITE__CONNECTION_STRING, with BASIS_OPTIM__ORDERS__DATABASE=name"),
    ],
    ids=["named", "by-prefix", "a-database", "a-databases-limit", "optims-block", "orders"],
)
def test_a_variable_optim_0_2_0_read_is_refused_naming_what_replaced_it(name: str, renamed: str) -> None:
    with pytest.raises(SettingsError, match=re.escape(f"rename {name} to {renamed}, where they are set")):
        refuse_renamed({name: "x"})


def test_another_tools_optim_variable_is_left_alone() -> None:
    refuse_renamed({"OPTIM_HOME": "/opt/tool", "BASIS_SNAPSHOT_ROOT": "runs"})


def test_a_database_is_a_source_once_optims_block_gives_its_limits() -> None:
    """Opening the sources connects to nothing; a dataset that reads a database without its limits names them."""
    settings = Settings.model_validate({"databases": {"mssql": BOOKS, "oracle": ANALYTICS}, "optim": {"databases": {"oracle": {"max_concurrent": 2}}}})

    with open_context(settings, cache=False) as context:
        assert sorted(context.loader.sources) == ["file", "oracle"]
        with pytest.raises(LookupError, match=r"holdings reads from mssql, which is not configured: set BASIS_DATABASES__MSSQL__\* and BASIS_OPTIM__DATABASES__MSSQL__MAX_CONCURRENT"):
            context.loader.load(HOLDINGS, as_of_date=date(2026, 10, 16), portfolios=("P1",))


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"optim": {"databases": {"books": {"max_concurrent": 4}}}}, "no database books is configured: set BASIS_DATABASES__BOOKS__KIND"),
        ({"databases": {"file": BOOKS}, "optim": {"databases": {"file": {"max_concurrent": 4}}}}, "file names optim's own source, never a database"),
        ({"databases": {"books": BOOKS}, "optim": {"orders": {"database": "trading", "table": "trading.orders"}}}, "BASIS_OPTIM__ORDERS__DATABASE is trading, which is not configured"),
    ],
    ids=["limits-of-no-database", "a-database-named-as-optims-own-source", "orders-in-no-database"],
)
def test_settings_that_name_a_database_no_block_configures_are_refused(values: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        Settings.model_validate(values)
