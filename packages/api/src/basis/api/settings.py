"""An API's settings: basis-core's, which name each database, with how the API serves, where its spans go, and where
each other service it calls is. A service subclasses `Settings` with fields of its own, which read from `BASIS_*`
variables too: `BASIS_SERVE__PORT` sets `serve.port`, `BASIS_HTTP__PRICING__URL` the pricing service's URL, and a
service's `max_days` is `BASIS_MAX_DAYS`. The settings are read once, when the app is built,
and a missing or invalid one stops it there, with one line that names the variable to set."""

import re
from contextvars import ContextVar
from typing import Annotated, Literal, Self, override

from pydantic import Field, FilePath, HttpUrl, ValidationError, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic_settings import SettingsError as SourceError

from basis.core import settings as core
from basis.core.model import Model
from basis.core.settings import SettingsError, variable


class Serve(Model):
    host: str = "0.0.0.0"  # noqa: S104 -- `basis serve` runs in a container, whose network decides who reaches it
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    drain_s: Annotated[float, Field(ge=0)] = 5
    """Seconds the server keeps serving after SIGTERM while readiness reports it not ready, so that the cluster stops
    sending it requests before it stops taking them. Ctrl+C stops without them."""
    shutdown_s: Annotated[int, Field(ge=1)] = 15
    """Seconds the requests still in flight then have to finish, before they are cancelled. With `drain_s` and the
    last export of spans, which `traces.timeout_s` bounds, the defaults fit in Kubernetes' 30-second grace period."""


class Traces(Model):
    exporter: Literal["otlp", "console"]
    """`otlp` sends spans to a collector over OTLP/HTTP; `console` prints them to stdout, to see them on a desktop."""
    endpoint: HttpUrl | None = None
    """The collector's traces endpoint, for `otlp` only, such as `http://otel-collector:4318/v1/traces`."""
    timeout_s: Annotated[float, Field(gt=0)] = 5
    """Seconds each export to the collector may take."""

    @model_validator(mode="after")
    def _endpoint_for_otlp(self) -> Self:
        if (self.exporter == "otlp") != (self.endpoint is not None):
            msg = "the otlp exporter needs an endpoint, BASIS_TRACES__ENDPOINT, and the console exporter takes none"
            raise ValueError(msg)
        return self


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


class Settings(core.Settings):
    serve: Serve = Field(default_factory=Serve)
    traces: Traces | None = None
    """Where the API's spans go. Without it they go nowhere, so a test or a desktop needs no collector."""
    http: dict[str, HttpService] = Field(default_factory=dict)
    """Each other service the API calls, by the name its `api.http` declaration gives."""

    @classmethod
    def given(cls, **values: object) -> Self:
        """These settings from `values` alone, never the environment or a file: a test's, which a developer's own
        variables must not change. (`model_validate` reads the environment too, as settings run it through `__init__`.)"""
        token = _ARGUMENTS_ONLY.set(True)
        try:
            return cls.model_validate(values)
        finally:
            _ARGUMENTS_ONLY.reset(token)

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
        if _ARGUMENTS_ONLY.get():
            return (init_settings,)
        return super().settings_customise_sources(settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings)


_ARGUMENTS_ONLY = ContextVar("basis.api.settings.arguments_only", default=False)
"""Set by `Settings.given` while it validates, so that no source but its arguments is read."""


def read[S: Settings](settings: type[S]) -> S:
    """`settings` from the environment, or an error that names each variable to set."""
    try:
        return settings()
    except ValidationError as error:
        problems = "; ".join(f"{variable(entry['loc'])}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        msg = f"the settings are not valid, so the API cannot start: set {problems}"
        raise SettingsError(msg) from None  # the validation error would print every value beside the field it names
    except SourceError as error:  # a value that does not parse before it is validated, such as a block's JSON
        field = re.search(r'field "([^"]+)"', str(error))  # pydantic-settings names the field in its message alone
        name = variable((field.group(1),)) if field is not None else f"the variable that starts {core.PREFIX}"
        reason = f": {error.__cause__}" if error.__cause__ is not None else ""
        msg = f"the settings are not valid, so the API cannot start: set {name}, whose value does not parse{reason}"
        raise SettingsError(msg) from None
