"""Process configuration, read once from `BASIS_*` environment variables and nothing else: no job repo holds a connection
string. Each database a job names is configured by that name, as `BASIS_DATABASES__WAREHOUSE__KIND=mssql`, with nested
fields under a double underscore, and so is each other service a part calls, as `BASIS_HTTP__PRICING__URL`. A database
without a `WRITE` block can be read, and never written: a laptop with read-only credentials runs a job, and writes
nothing. Where spans go is `BASIS_TRACES__*` (`Traces`), and nowhere when it is not set. Which variables a command
reads is its environment's (`basis.core.environments`): on a desktop, the user's file for it, such as `qa.env`, alone,
and in a pod, the process's environment."""

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Annotated, Literal, Self, TypeAliasType, Union, get_args, get_origin, override

from pydantic import BaseModel, Field, FilePath, HttpUrl, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, DotEnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources.utils import parse_env_vars

from basis.core.errors import BasisError
from basis.core.model import Model
from basis.core.tables import IDENTIFIER

if TYPE_CHECKING:  # pyarrow loads with storage, when a verb opens the run store, never for `basis --version`
    from basis.core.storage import Root

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


HEADER = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$"
"""An HTTP header's name, a token of RFC 9110's characters."""


class BearerLogin(Model):
    """A fixed token, sent as `Authorization: Bearer TOKEN`."""

    kind: Literal["bearer"]
    token: SecretStr


class HeaderLogin(Model):
    """A key, sent in the header the service names, such as `X-API-Key`."""

    kind: Literal["header"]
    name: Annotated[str, Field(pattern=HEADER)]
    value: SecretStr


class BasicLogin(Model):
    """A user and password, sent as HTTP Basic authentication, which only HTTPS keeps secret."""

    kind: Literal["basic"]
    user: str
    password: SecretStr


class ClientCredentialsLogin(Model):
    """OAuth 2.0's client credentials: a token for the client id and secret from the token URL, such as Entra ID's
    `https://login.microsoftonline.com/TENANT/oauth2/v2.0/token`, sent as a bearer token until it expires."""

    kind: Literal["client_credentials"]
    token_url: HttpUrl
    client_id: str
    client_secret: SecretStr
    scope: str | None = None
    """The scope the token is for, such as `api://pricing/.default`, where the identity provider asks for one."""


class EntraLogin(Model):
    """Entra ID's token for `scope`, through the pod's workload identity, which AKS's webhook sets up, or as the user
    `az login` signed in on a desktop, sent as a bearer token until it expires."""

    kind: Literal["entra"]
    via: Literal["workload_identity", "cli"]
    scope: str
    """The scope the token is for, such as `api://pricing/.default`."""


class NegotiateLogin(Model):
    """Kerberos through SPNEGO, `Authorization: Negotiate`, as the user Windows logged on or the ticket `kinit` got:
    a repo that signs in so takes `basis-core[kerberos]`."""

    kind: Literal["negotiate"]
    service: str = "HTTP"
    """The service class of the service's principal, `HTTP/HOST`, its host the URL's."""


type HttpLogin = Annotated[BearerLogin | HeaderLogin | BasicLogin | ClientCredentialsLogin | EntraLogin | NegotiateLogin, Field(discriminator="kind")]
"""How a part signs in to another service (`basis.core.logins`)."""


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
    login: HttpLogin | None = None
    """How each call signs in, as `BASIS_HTTP__NAME__LOGIN__KIND` names it. Without it, calls carry no login."""


class SlackChannel(Model):
    """A Slack channel, through its incoming webhook, from these settings alone, as another service's are."""

    webhook: SecretStr
    """The channel's incoming-webhook URL, `https://hooks.slack.com/services/...`, a secret: whoever has it posts."""
    ca_bundle: FilePath | None = None
    """A PEM file of the certificate authorities Slack's certificate must chain to, as the firm's proxy re-signs it."""
    proxy: HttpUrl | None = None
    """The proxy each post goes through. Without it, posts go direct."""


class FolderSettings(Model):
    """A shared folder, as this machine reaches it: `\\\\server\\share\\pm` on Windows, and its mount path in WSL and in
    a pod, such as `/mnt/pm`."""

    path: str
    """The folder's absolute path on this machine."""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if not Path(self.path).is_absolute():
            msg = f"a folder's path is absolute on this machine, as \\\\server\\share on Windows or /mnt/share elsewhere, not {self.path!r}"
            raise ValueError(msg)
        return self


class MongoSettings(Model):
    """A Mongo database, and how a part reaches it: from these settings alone."""

    uri: SecretStr
    """The connection string, `mongodb://user:password@host:27017/?replicaSet=rs0` or `mongodb+srv://...`, a secret."""
    database: str
    """The database the part's collections are in."""
    timeout_s: Annotated[float, Field(gt=0)]
    """Seconds each read or write may take, the server's selection included."""
    max_documents: Annotated[int, Field(ge=1)]
    """The most documents a read may return: one that returns more fails, rather than filling the run's memory."""
    ca_bundle: FilePath | None = None
    """A PEM file of the certificate authorities the server's certificate must chain to, where it uses TLS."""


ACCOUNT = r"^[a-z0-9]{3,24}$"
"""An Azure storage account's name."""
CONTAINER = r"^[a-z0-9](?:-?[a-z0-9])+$"
"""An Azure storage container's name: lower-case letters, digits and single hyphens, 3 to 63 of them."""


class LakeSettings(Model):
    """An Azure Data Lake Storage Gen2 container, with the hierarchical namespace on, and how this machine signs in to
    it (`basis.core.lake`): from these settings alone, never Azure's default chain of credentials, which on a desktop
    that is an Azure VM signs in as the VM rather than as its user."""

    account: Annotated[str, Field(pattern=ACCOUNT)]
    """The storage account's name, as in `ACCOUNT.dfs.core.windows.net`."""
    container: Annotated[str, Field(pattern=CONTAINER, min_length=3, max_length=63)]
    """The container the lake's tables are folders of. basis never creates it."""
    auth: Literal["key", "sas", "workload_identity", "cli"]
    """`key` signs in with the account's key, and `sas` with a SAS token, each `secret`; `workload_identity` with the
    pod's federated identity, which AKS's workload identity webhook sets up; `cli` as the user `az login` signed in."""
    secret: SecretStr | None = None
    """The account key, for `key`, or the SAS token, for `sas`, with or without its leading `?`: a secret."""
    emulator: str | None = None
    """`HOST:PORT` of a local emulator such as Azurite, reached over plain HTTP with `key` or `sas`: a test's, never a
    real account's."""

    @model_validator(mode="after")
    def _secret_for_key_or_sas(self) -> Self:
        if (self.auth in {"key", "sas"}) != (self.secret is not None):
            msg = f"a lake signed in to by {self.auth} {'needs' if self.secret is None else 'takes no'} secret: set SECRET for key and sas alone"
            raise ValueError(msg)
        if self.emulator is not None and self.auth not in {"key", "sas"}:
            msg = "an emulator is signed in to with key or sas alone"
            raise ValueError(msg)
        return self


EMAIL = r"^[^@\s<>,;]+@[^@\s<>,;]+$"
"""An address, `name@host`, with nothing around it."""


class EmailRelay(Model):
    """An SMTP relay, such as the firm's, and who a message to this name goes to: so each environment's settings say
    who reads it, and a message from dev or QA reaches no desk."""

    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    security: Literal["none", "starttls", "tls"]
    """`starttls` upgrades a plain connection, as a relay on port 587 does; `tls` connects over TLS, as port 465 does;
    `none` sends in the clear, as an internal relay on port 25 may."""
    sender: Annotated[str, Field(pattern=EMAIL)]
    """The address a message is from."""
    to: Annotated[tuple[Annotated[str, Field(pattern=EMAIL)], ...], Field(min_length=1)]
    """The addresses a message goes to, as JSON: `["desk@example.com"]`."""
    user: str | None = None
    password: SecretStr | None = None
    """The login, where the relay asks for one; `user` and `password` come together."""

    @model_validator(mode="after")
    def _login_whole(self) -> Self:
        if (self.user is None) != (self.password is None):
            msg = "a relay's login is a user and a password: set both, or neither for a relay that takes none"
            raise ValueError(msg)
        return self


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
        dotenv_filtering="only_existing",  # a file's variable that sets no setting is ignored, as the environment's is
    )

    snapshot_root: str | None = None
    """Where `basis run` saves its snapshots, and where a replay finds a run by its id: a path, or a URL such as
    `abfs://runs@account.dfs.core.windows.net/etl`. A run from Python saves in its repo's `snapshots/` instead."""
    databases: dict[str, DatabaseSettings] = Field(default_factory=dict)
    """Each database a job reads or writes, by the name its datasets and tables give."""
    http: dict[str, HttpService] = Field(default_factory=dict)
    """Each other service a part calls, by the name its declaration gives."""
    mongo: dict[str, MongoSettings] = Field(default_factory=dict)
    """Each Mongo database a part reads or upserts into, by the name its declaration gives."""
    folders: dict[str, FolderSettings] = Field(default_factory=dict)
    """Each shared folder a part reads or writes files in, by the name its declaration gives."""
    lakes: dict[str, LakeSettings] = Field(default_factory=dict)
    """Each Azure Data Lake container a part archives to, by the name its declaration gives."""
    slack: dict[str, SlackChannel] = Field(default_factory=dict)
    """Each Slack channel a part posts to, by the name its declaration gives."""
    email: dict[str, EmailRelay] = Field(default_factory=dict)
    """Each list of people a part emails, and the relay it goes through, by the name its declaration gives."""
    traces: Traces | None = None
    """Where spans go. Without it they are made, so every record logged in a trace carries its ids, and go nowhere:
    a desktop or a test needs no Agent."""
    git_sha: str | None = None
    """The repo's commit, which basis's Dockerfile sets in the image as `BASIS_GIT_SHA`: the version spans carry."""

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
        if _FILE_ALONE.get() and isinstance(dotenv_settings, DotEnvSettingsSource):
            return (_Verbatim(settings_cls, env_file=dotenv_settings.env_file, env_file_encoding="utf-8-sig"),)
        return super().settings_customise_sources(settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings)

    def snapshots(self) -> Root:
        if self.snapshot_root is None:
            msg = "no snapshot root is set, which is where `basis run` saves its snapshots and where a replay finds a run by its id: set BASIS_SNAPSHOT_ROOT, as .env.example shows"
            raise SettingsError(msg)
        from basis.core.storage import Root  # noqa: PLC0415 -- pyarrow loads when the run store is opened

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


_FILE_ALONE = ContextVar("basis.core.settings.file_alone", default=False)
"""Set by `read_file` while it validates, so that no source but its file is read."""


class FileSettings(Settings):
    """Settings from one settings file alone, never the process's environment: a user's file for an environment, such
    as the QA tier's `qa.env`, so a variable set for another environment never mixes in. As in the environment, a
    variable that sets no setting is ignored, such as `BASIS_LOG_LEVEL`, and so is one under a name basis read before,
    such as `ETL_SNAPSHOT_ROOT`."""


def read(path: Path) -> FileSettings:
    """The settings the file at `path` holds, and nothing else."""
    return read_file(FileSettings, path)


def read_values(path: Path) -> dict[str, str]:
    """The `NAME=VALUE` lines of the settings file at `path`, each value as written: a line starting `#` is a comment,
    a value in matching quotes is what they hold, and nothing else is read into a value, no `${NAME}` expanded from the
    process, no `\\n` made a newline, as a Windows path holds it, and no ` #` cut off as a comment."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:  # noqa: PLR2004 -- a quote at each end
            value = value[1:-1]
        found[name.strip()] = value
    return found


class _Verbatim(DotEnvSettingsSource):
    """The settings file's values as `read_values` reads them, where pydantic's reader would expand and unescape them."""

    @override
    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        return parse_env_vars(dict(read_values(file_path)), self.case_sensitive, self.env_ignore_empty, self.env_parse_none_str)


def read_file[S: Settings](kind: type[S], path: Path) -> S:
    """`kind`'s settings, a part's own subclass among them, from the file at `path` alone, each value as written
    (`read_values`)."""
    token = _FILE_ALONE.set(True)
    try:
        return kind(_env_file=path)
    finally:
        _FILE_ALONE.reset(token)


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
