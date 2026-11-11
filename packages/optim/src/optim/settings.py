"""Process configuration, read once from `BASIS_*` environment variables and nothing else: no model repo holds a
connection string. basis-core's settings name each database a dataset reads, as `BASIS_DATABASES__MSSQL__KIND=mssql`,
and the snapshot root, `BASIS_SNAPSHOT_ROOT`; optim's own are its block, `BASIS_OPTIM__*`: the cache root, the worker
processes, how many reads each database serves at once, the REST source and the orders table. Nested fields use a
double underscore. A source that is not configured cannot be read, so a run that reads files needs no settings. A
model's `main.py` names the file the settings come from, such as `.env.prod`; the command line reads the environment
`--env` or `BASIS_ENV` names, through basis-core's `environments`: the user's file for it, alone, or else the
process's environment, as `uv run --env-file .env` sets it.

optim 0.2.0 read `OPTIM_*` variables. Each of those is refused, naming the variable that replaced it, rather than
ignored, so a settings file written for 0.2.0 cannot silently run with nothing configured."""

from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import Field, model_validator

from basis.core import settings as core
from basis.core.settings import SettingsError
from optim.orders import OrdersSettings
from optim.schema import Model
from optim.sources import OWN
from optim.sources.rest import ApiSettings

PREFIX = "BASIS_OPTIM__"
RENAMED = {
    "OPTIM_SNAPSHOT_ROOT": "BASIS_SNAPSHOT_ROOT",
    "OPTIM_CACHE_ROOT": f"{PREFIX}CACHE_ROOT",
    "OPTIM_WORKERS": f"{PREFIX}WORKERS",
    "OPTIM_LOG_": "BASIS_LOG_",
    "OPTIM_GIT_SHA": "BASIS_GIT_SHA",
    "OPTIM_IMAGE": "BASIS_IMAGE",
    "OPTIM_REST__": f"{PREFIX}REST__",
    "OPTIM_ORACLE__MAX_CONCURRENT": f"{PREFIX}DATABASES__ORACLE__MAX_CONCURRENT",
    "OPTIM_MSSQL__MAX_CONCURRENT": f"{PREFIX}DATABASES__MSSQL__MAX_CONCURRENT",
    "OPTIM_ORACLE__": "BASIS_DATABASES__ORACLE__",
    "OPTIM_MSSQL__": "BASIS_DATABASES__MSSQL__",
    "OPTIM_ORDERS__TABLE": f"{PREFIX}ORDERS__TABLE",
    "OPTIM_ORDERS__CONNECTION_STRING": f"the orders database's BASIS_DATABASES__NAME__WRITE__CONNECTION_STRING, with {PREFIX}ORDERS__DATABASE=name",
    "OPTIM_ORDERS__TIMEOUT_S": "the orders database's BASIS_DATABASES__NAME__TIMEOUT_S",
}
"""Each variable optim 0.2.0 read, by name or by a prefix ending in `_`, and what replaced it; a prefix's rest is kept.
Only these are refused, since other tools may read variables of their own that start `OPTIM_`."""


def refuse_renamed(environ: Mapping[str, str]) -> None:
    """Fail when `environ` sets a variable under the name optim 0.2.0 read, which optim now ignores."""
    if old := sorted(name for name in environ if _renamed(name) is not None):
        renames = "; ".join(f"{name} to {_renamed(name)}" for name in old)
        msg = (
            f"{', '.join(old)} {'is' if len(old) == 1 else 'are'} set, and optim reads only BASIS_ variables now: rename {renames}, where they are set and in"
            " each .env file. A database's block also needs its KIND and ISOLATION, as .env.example shows"
        )
        raise SettingsError(msg)


def _renamed(name: str) -> str | None:
    for old, new in RENAMED.items():
        if name == old or (old.endswith("_") and name.startswith(old)):
            return new + name.removeprefix(old) if old.endswith("_") else new
    return None


class DatabaseLimits(Model):
    """How hard a run may press one database, beside basis-core's settings of it, which bound each read's time and rows."""

    max_concurrent: Annotated[int, Field(ge=1)]
    """Reads this process has open to the database at once, each a transaction of its own; the loader holds the rest."""


class OptimSettings(Model):
    """optim's own settings, the `BASIS_OPTIM__*` variables."""

    cache_root: str | None = None
    """Where the dataset cache is, a path or URL; without it, every dataset is read live."""
    workers: Annotated[int, Field(ge=1)] = 1
    """Worker processes per run; 1 decides in the calling process."""
    databases: dict[str, DatabaseLimits] = Field(default_factory=dict)
    """Each database a dataset reads, by the name its `BASIS_DATABASES__NAME__*` block gives: a database without its
    limits here is not read."""
    rest: ApiSettings | None = None
    """The REST API that datasets with `source="rest"` read."""
    orders: OrdersSettings | None = None
    """The orders table accepted portfolios' orders are published to. Only production configures one, and only
    `basis run --publish` and `basis resume` write to it."""


CONSISTENT = {"mssql": "snapshot", "oracle": "read_only"}
"""The isolation, by kind, at which a transaction sees its database at one moment, as a batch's reads must: a
portfolio's holdings and the rest of its rows are only consistent from one moment."""


class Settings(core.Settings):
    optim: OptimSettings = Field(default_factory=OptimSettings)

    @model_validator(mode="after")
    def _databases_are_configured(self) -> Self:
        """Every database optim's block names is one basis-core's settings configure, which reads at one moment and
        takes no name of optim's own sources; and the orders database has a write login and a ledger."""
        for name in self.optim.databases:
            if name in OWN:
                msg = f"{PREFIX}DATABASES__{name.upper()}__MAX_CONCURRENT is set, and {name} names optim's own source, never a database: give the database another name"
                raise ValueError(msg)
            if (database := self.databases.get(name)) is None:
                msg = f"{PREFIX}DATABASES__{name.upper()}__MAX_CONCURRENT is set, and no database {name} is configured: set BASIS_DATABASES__{name.upper()}__KIND and the rest of its block"
                raise ValueError(msg)
            if database.isolation != (consistent := CONSISTENT[database.kind]):
                msg = f"{core.variable(('databases', name, 'isolation'))} is {database.isolation}, and optim reads each batch of portfolios from a database at one moment: set it to {consistent}"
                raise ValueError(msg)
        if (orders := self.optim.orders) is not None:
            if (database := self.databases.get(orders.database)) is None:
                msg = f"{PREFIX}ORDERS__DATABASE is {orders.database}, which is not configured: set BASIS_DATABASES__{orders.database.upper()}__KIND and the rest of its block, WRITE included"
                raise ValueError(msg)
            if (refused := core.publish_refusal(orders.database, database)) is not None:
                reason, fix = refused
                msg = f"{PREFIX}ORDERS__DATABASE is {orders.database}, which {reason}: {fix}"
                raise ValueError(msg)
        return self
