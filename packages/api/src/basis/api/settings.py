"""An API's settings: basis-core's, which name each database, with how the API serves and where its spans go. A
service subclasses `Settings` with fields of its own, which read from the same variables, under basis-core's prefix:
`SERVE__PORT` after the prefix sets `serve.port`, and a service's `max_days` is `MAX_DAYS` after it. The settings
are read once, when the app is built, and a missing or invalid one stops it there, naming the variable to set."""

import re
from contextvars import ContextVar
from typing import Annotated, Literal, Self, override

from pydantic import Field, HttpUrl, ValidationError, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic_settings import SettingsError as SourceError

from basis.core import settings as core
from basis.core.model import Model


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
            msg = "the otlp exporter needs an endpoint, TRACES__ENDPOINT after the prefix, and the console exporter takes none"
            raise ValueError(msg)
        return self


class Settings(core.Settings):
    serve: Serve = Field(default_factory=Serve)
    traces: Traces | None = None
    """Where the API's spans go. Without it they go nowhere, so a test or a desktop needs no collector."""

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


class SettingsError(ValueError):
    """Settings the environment does not give, or gives wrong; the message names each variable to set."""


def read[S: Settings](settings: type[S]) -> S:
    """`settings` from the environment, or an error that names each variable to set."""
    try:
        return settings()
    except ValidationError as error:
        problems = "; ".join(f"{variable(settings, entry['loc'])}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        msg = f"the settings are not valid, so the API cannot start: set {problems}"
        raise SettingsError(msg) from None  # the validation error would print every value beside the field it names
    except SourceError as error:  # a value that does not parse before it is validated, such as a block's JSON
        field = re.search(r'field "([^"]+)"', str(error))  # pydantic-settings names the field in its message alone
        name = variable(settings, (field.group(1),)) if field is not None else f"the variable that starts {variable(settings, ())}"
        reason = f": {error.__cause__}" if error.__cause__ is not None else ""
        msg = f"the settings are not valid, so the API cannot start: set {name}, whose value does not parse{reason}"
        raise SettingsError(msg) from None


def variable(settings: type[BaseSettings], location: tuple[str | int, ...]) -> str:
    """The environment variable that sets the field at `location`, with whatever prefix the settings read."""
    config = settings.model_config
    delimiter = config.get("env_nested_delimiter") or "__"
    return f"{config.get('env_prefix') or ''}{delimiter.join(str(part) for part in location)}".upper()
