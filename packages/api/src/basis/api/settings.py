"""An API's settings: basis-core's, which name each database, with how the API serves, where its spans go, where
each other service it calls is, how it verifies its users' tokens (`identity`, which `basis.api.identity` holds), and
which other origins' pages may call it (`allowed_origins`, which `basis.api.origins` describes). A service subclasses `Settings` with fields of its own, which read from `BASIS_*`
variables too: `BASIS_SERVE__PORT` sets `serve.port`, `BASIS_HTTP__PRICING__URL` the pricing service's URL, and a
service's `max_days` is `BASIS_MAX_DAYS`. The settings are read once, when the app is built,
and a missing or invalid one stops it there, with one line that names the variable to set."""

import re
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Self, override

from pydantic import Field, PrivateAttr, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic_settings import SettingsError as SourceError

from basis.api.identity import Identity
from basis.api.origins import Origin
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
    docs: bool = False
    """Whether the app serves `/docs`, Swagger UI's page of its document, which loads Swagger UI's script and style
    from a public CDN, at an exact version, into the page of the API's own origin: off unless a developer turns it on,
    as `BASIS_SERVE__DOCS=true`, since on an origin that serves a UI that script runs beside the UI's token."""
    max_body_bytes: Annotated[int, Field(ge=1)] = 1_048_576
    """The largest request body a route takes, in bytes: a larger one answers 413 `body-too-large`, refused by its
    `Content-Length` before any of it is read, or once that many bytes have arrived. A body is read whole and parsed
    in memory, so this bounds what one request holds."""
    heartbeat_s: Annotated[float, Field(gt=0)] = 15
    """Seconds a stream of live updates (`api.stream`) goes without a message before it sends a heartbeat, a comment
    the reader ignores, so that no proxy between it and the browser closes a connection it thinks idle; and the
    longest one message may take to be sent, after which a reader too slow to take it loses its stream."""
    stream_s: Annotated[float, Field(gt=0)] = 300
    """Seconds one stream of live updates lasts at most, after which it ends, saying so, and the UI connects again: so
    a connection is never held for ever, and each replica's streams move as the cluster moves its requests."""


class Pool(Model):
    """The processes a service's compute functions run in, which start with the app."""

    workers: Annotated[int, Field(ge=1)]
    """How many: each runs one call at a time, so at most this many run at once and the rest wait their turn, within
    their deadlines. Each is a Python process of its own, with the service's compute modules imported."""


class Smoke(Model):
    """Where `basis smoke --env ENV` reaches the API as that environment serves it."""

    url: str
    """The base its document's paths are under there, as a client calls it: `https://qa.example.com/api`, where its
    document is `openapi.json` and its readiness `health/ready`."""
    ca_bundle: Path | None = None
    """The certificates its TLS is checked against, where they are not the system's."""


class Settings(core.Settings):
    serve: Serve = Field(default_factory=Serve)
    smoke: Smoke | None = None
    """Where `basis smoke` reaches the deployed API, in an environment's settings: `BASIS_SMOKE__URL`."""
    compute: Pool | None = None
    """The process pool, `BASIS_COMPUTE__WORKERS`, which a service whose routes ask for a compute function must give."""
    identity: Identity | None = None
    """How the API verifies its users' tokens, which a service that declares a user needs: see `basis.api.identity`."""
    allowed_origins: tuple[Origin, ...] = ()
    """The origins whose pages may call the API from a browser, besides its own, each as the browser sends it, such as
    `https://positions.example`: none unless they are named, and a UI behind the API's own host needs none. See
    `basis.api.origins`."""
    _given: bool = PrivateAttr(default=False)
    """Whether these settings were validated inside `given`; no variable, file or argument sets a private attribute."""

    @property
    def from_arguments(self) -> bool:
        """Whether these settings are `given`'s, made in code from its arguments, as only a test's are, and never read
        from the environment: the development key is refused in any other settings off a declared desktop."""
        return self._given

    @classmethod
    def given(cls, **values: object) -> Self:
        """These settings from `values` alone, never the environment or a file: a test's, which a developer's own
        variables must not change. (`model_validate` reads the environment too, as settings run it through `__init__`.)"""
        token = _ARGUMENTS_ONLY.set(True)
        try:
            return cls.model_validate(values)
        finally:
            _ARGUMENTS_ONLY.reset(token)

    def verifying(self, identity: Identity) -> Self:
        """These settings, verifying tokens as `identity` says, as settings made in code are: for an app on its fakes
        alone, which `basis dev` serves on this machine and `basis call --fake` calls in its process (`dev.settings`)."""
        copied = self.model_copy(update={"identity": identity})
        copied._given = True  # noqa: SLF001 -- the class's own mark, on its copy
        return copied

    @override
    def model_post_init(self, context: object, /) -> None:
        super().model_post_init(context)
        self._given = _ARGUMENTS_ONLY.get()

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
    except (ValidationError, SourceError) as error:
        raise unreadable(error, settings) from None  # the validation error would print every value beside the field it names


def unreadable(error: ValidationError | SourceError, settings: type[Settings]) -> SettingsError:
    """Why `settings` cannot be read, from the environment or an environment's file, naming each variable to set."""
    if isinstance(error, ValidationError):
        problems = "; ".join(f"{variable(entry['loc'], settings)}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        return SettingsError(f"the settings are not valid, so the API cannot start: set {problems}")
    field = re.search(r'field "([^"]+)"', str(error))  # a value that does not parse before it is validated, such as a block's JSON; pydantic-settings names the field in its message alone
    name = variable((field.group(1),)) if field is not None else f"the variable that starts {core.PREFIX}"
    reason = f": {error.__cause__}" if error.__cause__ is not None else ""
    return SettingsError(f"the settings are not valid, so the API cannot start: set {name}, whose value does not parse{reason}")
