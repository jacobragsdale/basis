"""Process configuration, read once at startup from `OPTIM_*` environment variables and nothing else: the sources'
hosts and credentials, the snapshot root, the cache root and the orders table are the same for every model, so no
model repo holds a connection string. A source that is not configured cannot be read, so a run that reads files needs
no settings. Each source owns its settings model; nested fields use a double underscore, as in `OPTIM_ORACLE__DSN`. A
model's `main.py` names the file the settings come from, such as `.env.prod`; the command line reads the process's
environment, as `uv run --env-file .env` sets it."""

from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from optim.orders import OrdersSettings
from optim.sources.mssql import MssqlSettings
from optim.sources.oracle import OracleSettings
from optim.sources.rest import ApiSettings
from optim.storage import Root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPTIM_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        validate_default=True,
        revalidate_instances="always",
        allow_inf_nan=False,
        hide_input_in_errors=True,  # an error would otherwise print every secret beside the field it is missing
    )

    snapshot_root: str | None = None
    """Where `optim run` saves its snapshots, and where a replay finds a run by its id: a path, or a URL such as
    `abfs://runs@account.dfs.core.windows.net/optim`. A run from Python saves in its repo's `snapshots/` instead."""
    cache_root: str | None = None
    """Where the dataset cache is, a path or URL; without it, every dataset is read live."""
    workers: Annotated[int, Field(ge=1)] = 1
    """Worker processes per run; 1 decides in the calling process."""
    oracle: OracleSettings | None = None
    mssql: MssqlSettings | None = None
    rest: ApiSettings | None = None
    orders: OrdersSettings | None = None
    """The orders table accepted portfolios' orders are written to. Only production configures one, and only
    `optim run` writes to it."""

    def snapshots(self) -> Root:
        """The snapshot root, which `optim run` saves under and a replay by id reads from."""
        if self.snapshot_root is None:
            msg = "set OPTIM_SNAPSHOT_ROOT: where optim run saves its snapshots, and where a replay finds a run by its id"
            raise ValueError(msg)
        return Root.at(self.snapshot_root)
