"""Process configuration, read once from `BASIS_*` environment variables and nothing else: no job repo holds a connection
string. Each database a job names is configured by that name, as `BASIS_DATABASES__WAREHOUSE__KIND=mssql`, with nested
fields under a double underscore, and so is each other service a part calls, as `BASIS_HTTP__PRICING__URL`. A database
without a `WRITE` block can be read, and never written: a laptop with read-only credentials runs a job, and writes
nothing. Where spans go is `BASIS_TRACES__*` (`Traces`), and nowhere when it is not set. A job's `main.py` names the
settings file it reads, such as `.env.prod`; the command line reads the process's environment, as `uv run --env-file
.env` sets it."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Literal, Self, TypeAliasType, Union, get_args, get_origin, override

from pydantic import BaseModel, Field, FilePath, HttpUrl, SecretStr, model_validator
from pydantic.fields import FieldInfo
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
    part publishes, such as a job, which writes it in the same transaction. An API's requests write no ledger."""


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
    part publishes, such as a job. An API's requests write no ledger."""


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


class HttpService(Model):
    """Another service, and how to reach it: from these settings alone, never `SSL_CERT_FILE`, `HTTP_PROXY` or the
    like, which configure nothing basis decides."""

    url: HttpUrl
    """The service's base URL, which each call's path follows, such as `https://pricing.internal.example/api`."""
    ca_bundle: FilePath | None = None
    """A PEM file of the certificate authorities the service's certificate must chain to, such as the firm's own,
    in place of the operating system's trusted ones. Without it, the operating system's are trusted."""
    proxy: HttpUrl | None = None
    """The proxy each call goes through, such as `http://proxy.internal.example:8080`. Without it, calls go direct."""


class Traces(Model):
    """Where spans go (`basis.core.traces`): from these settings alone, never an `OTEL_*` variable."""

    exporter: Literal["otlp", "console"]
    """`otlp` posts spans over OTLP/HTTP to `endpoint`, the Datadog Agent on the node in a pod; `console` writes them
    to stderr, to see them on a desktop, never to stdout, which carries a command's results."""
    endpoint: HttpUrl | None = None
    """For `otlp` only: the full URL spans are posted to, path and all, since basis posts to exactly the URL it is
    given, such as `http://10.0.0.5:4318/v1/traces`. In a pod, the host is the node's IP, which the downward API gives
    as `status.hostIP`: `http://$(HOST_IP):4318/v1/traces`."""
    timeout_s: Annotated[float, Field(gt=0)] = 5
    """Seconds each export may take, its retries included."""

    @model_validator(mode="after")
    def _endpoint_for_otlp(self) -> Self:
        if (self.exporter == "otlp") != (self.endpoint is not None):
            msg = "the otlp exporter needs an endpoint, BASIS_TRACES__ENDPOINT, and the console exporter takes none"
            raise ValueError(msg)
        return self


def publish_refusal(name: str, database: MssqlSettings | OracleSettings) -> tuple[str, str] | None:
    """Why a run cannot publish to the database the settings name `name`, and the fix, which names each variable to
    set; or None when it can: a publish needs a write login, and a ledger to write in the same transaction."""
    if database.write is not None and database.write.ledger is not None:
        return None
    return unpublishable(name, database)


def unpublishable(name: str, database: MssqlSettings | OracleSettings) -> tuple[str, str]:
    """Why a run cannot publish to a database whose settings lack a write login or a ledger, and the fix."""
    ledger = variable(("databases", name, "write", "ledger"))
    if database.write is not None:
        return "has a write login but no ledger, which a publish writes in its transaction", f"set {ledger}"
    logins = ("connection_string",) if isinstance(database, MssqlSettings) else ("user", "password")
    return "has no write settings", f"set {', '.join(variable(('databases', name, 'write', part)) for part in logins)} and {ledger}"


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
    http: dict[str, HttpService] = Field(default_factory=dict)
    """Each other service a part calls, by the name its declaration gives."""
    traces: Traces | None = None
    """Where spans go. Without it they are made, so every record logged in a trace carries its ids, and go nowhere:
    a desktop or a test needs no Agent."""
    git_sha: str | None = None
    """The repo's commit, which basis's Dockerfile sets in the image as `BASIS_GIT_SHA`: the version spans carry."""

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


def locate(location: str, folder: Path) -> str:
    """A run store as `BASIS_SNAPSHOT_ROOT` names it, found where a command reads it: a URL or an absolute path as it
    is, and a relative path in `folder`, which is the repo, or the clone's own folder for a check in another worktree."""
    return location if "://" in location or Path(location).is_absolute() else (folder / location).as_posix()


class FileSettings(Settings):
    """Settings from one settings file alone, never the process's environment: the QA tier's, so a variable set for
    another environment never mixes in. As in the environment, a variable that sets no setting is ignored, such as
    `BASIS_LOG_LEVEL`, and so is one under a name basis read before, such as `ETL_SNAPSHOT_ROOT`."""

    model_config = SettingsConfigDict(dotenv_filtering="only_existing")

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
        return (dotenv_settings,)


def read(path: Path) -> FileSettings:
    """The settings the file at `path` holds, and nothing else."""
    return FileSettings(_env_file=path)


type _At = tuple[object, str | None]
"""A type within the settings, and the field its union is discriminated by, where it is a discriminated union."""


def variable(location: Sequence[int | str], settings: type[BaseModel] = Settings) -> str:
    """The environment variable that sets the value at `location` in `settings`, from a validation error, such as
    `BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER`. pydantic puts the tag of a discriminated union in the location, after
    the union, such as a database's `mssql`, which no variable names, so each is left out wherever it is."""
    parts: list[str] = []
    at: _At = (settings, None)
    for part in location:
        members = _tagged(at)
        if members is not None and part in members:
            at = (members[part], None)
            continue
        parts.append(str(part))
        at = _within(at[0], part)
    return PREFIX + "__".join(parts).upper()


def _within(kind: object, part: int | str) -> _At:
    """The type at `part` within `kind`: a model's field, a mapping's value, or a sequence's item."""
    kind, _ = _unwrapped(kind)
    if isinstance(kind, type) and issubclass(kind, BaseModel):
        if (found := kind.model_fields.get(str(part))) is None:
            return None, None
        return found.annotation, found.discriminator if isinstance(found.discriminator, str) else None
    arguments = get_args(kind)
    if get_origin(kind) is dict and len(arguments) == 2:  # noqa: PLR2004 -- the key's type and the value's
        return arguments[1], None
    if get_origin(kind) in {list, tuple} and arguments:
        return arguments[0], None
    return None, None


def _tagged(at: _At) -> dict[int | str, object] | None:
    """Each member of the discriminated union at `at`, by its tag; None where `at` is no discriminated union."""
    kind, discriminator = _unwrapped(at[0])
    if (discriminator := at[1] if at[1] is not None else discriminator) is None:
        return None
    members: dict[int | str, object] = {}
    for member in get_args(kind):
        if isinstance(member, type) and issubclass(member, BaseModel) and (tag := member.model_fields.get(discriminator)) is not None:
            members |= dict.fromkeys(get_args(tag.annotation), member)
    return members


def _unwrapped(kind: object) -> tuple[object, str | None]:
    """`kind` without its alias, its `Annotated` metadata and its `| None`, and the field a `Field(discriminator=...)`
    in that metadata names."""
    discriminator: str | None = None
    while True:
        if isinstance(kind, TypeAliasType):
            kind = kind.__value__
        elif get_origin(kind) is Annotated:
            kind, *metadata = get_args(kind)
            discriminator = next((found.discriminator for found in metadata if isinstance(found, FieldInfo) and isinstance(found.discriminator, str)), discriminator)
        elif get_origin(kind) in {Union, UnionType} and NoneType in (arguments := get_args(kind)) and len(arguments) == 2:  # noqa: PLR2004 -- one type and None
            kind = next(argument for argument in arguments if argument is not NoneType)
        else:
            return kind, discriminator
