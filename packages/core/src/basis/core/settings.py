"""Process configuration, read once from `BASIS_*` environment variables and nothing else: no job repo holds a connection
string. Each database a job names is configured by that name, as `BASIS_DATABASES__WAREHOUSE__KIND=mssql`, with nested
fields under a double underscore. A database without a `WRITE` block can be read, and never written: a laptop with
read-only credentials runs a job, and writes nothing. A job's `main.py` names the settings file it reads, such as
`.env.prod`; the command line reads the process's environment, as `uv run --env-file .env` sets it."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, override

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from basis.core.errors import BasisError
from basis.core.model import Model
from basis.core.storage import Root
from basis.core.tables import IDENTIFIER

PREFIX = "BASIS_"
RENAMED = ("ETL_SNAPSHOT_ROOT", "ETL_DATABASES__", "ETL_LOG_", "ETL_GIT_SHA", "ETL_TEST_")
"""The variables basis read before its settings took the `BASIS_` prefix, by name or by prefix. Only these are
refused, since other tools at a firm that runs ETL may well read variables of their own that start `ETL_`."""


class SettingsError(BasisError, ValueError):
    """Settings that are missing, or that cannot be read."""


def refuse_renamed(environ: Mapping[str, str]) -> None:
    """Fail when `environ` sets a variable under the name basis read before, which it now ignores."""
    if old := sorted(name for name in environ if name.startswith(RENAMED)):
        renames = ", ".join(f"{name} to {PREFIX}{name.removeprefix('ETL_')}" for name in old)
        msg = f"{', '.join(old)} {'is' if len(old) == 1 else 'are'} set, and basis reads only BASIS_ variables now: rename {renames}, where they are set and in each .env file"
        raise SettingsError(msg)


LEDGER = r"^[A-Za-z_][A-Za-z0-9_$]{0,127}(\.[A-Za-z_][A-Za-z0-9_$]{0,127})?$"


class Limits(Model):
    timeout_s: Annotated[int, Field(ge=1)]
    """Seconds each statement may take, the login included, though a login never waits more than 30."""
    max_rows: Annotated[int, Field(ge=1)]
    """The most rows a query may return: one that returns more fails, rather than filling the run's memory."""


class MssqlWrite(Model):
    connection_string: SecretStr
    """A login that may insert, update and delete in the job's tables and in the ledger, and nothing else needs."""
    ledger: Annotated[str, Field(pattern=LEDGER)] | None = None
    """The ledger table, as `basis ddl --ledger TABLE --dialect mssql` creates it, such as `etl.publishes`: where a
    job or a model publishes, which writes it in the same transaction. An API's requests write no ledger."""


class MssqlSettings(Limits):
    kind: Literal["mssql"]
    connection_string: SecretStr
    """A login that may read, such as `Server=host;Database=db;Authentication=ActiveDirectoryDefault;Encrypt=yes`."""
    isolation: Literal["snapshot", "read_committed"]
    """How a run's reads from this database see it: `snapshot`, all of them at one moment, which the database must
    allow (`ALTER DATABASE ... SET ALLOW_SNAPSHOT_ISOLATION ON`); `read_committed`, each query at its own."""
    write: MssqlWrite | None = None
    """How a run writes, where this machine may; without it, the database is never written."""


class OracleWrite(Model):
    user: str
    password: SecretStr
    ledger: Annotated[str, Field(pattern=LEDGER)] | None = None
    """The ledger table, as `basis ddl --ledger TABLE --dialect oracle` creates it, such as `ETL.PUBLISHES`: where a
    job or a model publishes. An API's requests write no ledger."""


class OracleSettings(Limits):
    kind: Literal["oracle"]
    dsn: str
    """`host:port/service`, or a TNS alias."""
    user: str
    password: SecretStr
    mode: Literal["thin", "thick"]
    """`thin` needs no client libraries; `thick` loads Oracle Instant Client, which native network encryption and older
    servers need."""
    isolation: Literal["read_only", "read_committed"]
    """How a run's reads from this database see it: `read_only`, all of them at one moment, which fails on a table
    whose definition changed in the seconds before (ORA-01466; a TRUNCATE counts); `read_committed`, each query at
    its own."""
    client_lib_dir: Path | None = None
    """Thick mode only, where Oracle Instant Client is not on the library path."""
    write: OracleWrite | None = None


type DatabaseSettings = Annotated[MssqlSettings | OracleSettings, Field(discriminator="kind")]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        validate_default=True,
        revalidate_instances="always",
        allow_inf_nan=False,
        hide_input_in_errors=True,  # an error would otherwise print every secret beside the field it is missing
    )

    snapshot_root: str | None = None
    """Where `basis run` saves its snapshots, and where a replay finds a run by its id: a path, or a URL such as
    `abfs://runs@account.dfs.core.windows.net/etl`. A run from Python saves in its repo's `snapshots/` instead."""
    databases: dict[str, DatabaseSettings] = Field(default_factory=dict)
    """Each database a job reads or writes, by the name its datasets and tables give."""

    def snapshots(self) -> Root:
        if self.snapshot_root is None:
            msg = "no snapshot root is set, which is where `basis run` saves its snapshots and where a replay finds a run by its id: set BASIS_SNAPSHOT_ROOT, as .env.example shows"
            raise SettingsError(msg)
        return Root.at(self.snapshot_root)

    def database(self, name: str) -> MssqlSettings | OracleSettings:
        if not IDENTIFIER.match(name):
            msg = f"database {name!r}: a database's name is lower-case letters, digits and underscores: rename it where the job names it"
            raise SettingsError(msg)
        if (found := self.databases.get(name)) is None:
            msg = f"database {name!r} is not configured: set BASIS_DATABASES__{name.upper()}__KIND and the rest of its block, as .env.example shows"
            raise SettingsError(msg)
        return found


class GivenSettings(Settings):
    """Settings from arguments alone, never the environment or a file: a contract test's, which reads no database."""

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


_KINDS = {"mssql", "oracle"}
"""Each database's `kind`, which pydantic adds to a validation error's location, after the database's name."""


def variable(location: Sequence[int | str]) -> str:
    """The environment variable that sets the value at `location` in `Settings`, from a validation error, such as
    `BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER`."""
    parts = [str(part) for part in location]
    if parts[:1] == ["databases"] and len(parts) > 2 and parts[2] in _KINDS:  # noqa: PLR2004 -- databases, its name, its kind
        del parts[2]
    return PREFIX + "__".join(parts).upper()
