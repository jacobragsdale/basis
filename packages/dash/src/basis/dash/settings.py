"""A Dash app's settings: basis-core's, which name each database and the rest, with how the app serves (`serve`). An
app subclasses `Settings` with fields of its own, which read from `BASIS_*` variables too: `BASIS_SERVE__PORT` sets
`serve.port`, and `BASIS_SERVE__PREFIX` the path the firm's proxy serves the app under. The settings are read once,
when the app is built, from the environment `--env` names (`basis.core.environments`), and a missing or invalid one
stops it there, with one line that names the variable to set."""

import re
from contextvars import ContextVar
from typing import Annotated, Self, override

from pydantic import AfterValidator, Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic_settings import SettingsError as SourceError

from basis.core import serving
from basis.core import settings as core
from basis.core.settings import SettingsError, variable

_PREFIX = re.compile(r"^/(?:[A-Za-z0-9._~-]+/)*$")


def _prefix(value: str) -> str:
    if not _PREFIX.match(value):
        msg = f"a path prefix starts and ends with /, with letters, digits, dots, dashes, underscores and tildes between, as /apps/positions/, not {value!r}"
        raise ValueError(msg)
    return value


class Serve(serving.Serve):
    """How the app serves: basis-core's `host`, `port`, `drain_s` and `shutdown_s`, and the path it is served under."""

    prefix: Annotated[str, AfterValidator(_prefix)] = "/"
    """The path the browser reaches the app under, such as `/apps/positions/`, behind the firm's proxy: `/` where the
    app has a host of its own."""
    prefix_stripped: bool = False
    """Whether the proxy strips `prefix` before it forwards a request, so the server answers at `/` while the page asks
    for everything under the prefix. Where it keeps the prefix, the server answers under it, its health routes too."""


class Settings(core.Settings):
    serve: Serve = Field(default_factory=Serve)
    cache_mb: Annotated[int, Field(ge=1)] = 512
    """The most megabytes of datasets the process keeps, `BASIS_CACHE_MB`, as their Arrow tables count them: past it,
    the least recently used are evicted first (`basis.dash.cache`)."""

    @classmethod
    def given(cls, **values: object) -> Self:
        """These settings from `values` alone, never the environment or a file: a test's, which a developer's own
        variables must not change."""
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


_ARGUMENTS_ONLY = ContextVar("basis.dash.settings.arguments_only", default=False)
"""Set by `Settings.given` while it validates, so that no source but its arguments is read."""


def unreadable(error: ValidationError | SourceError, settings: type[Settings]) -> SettingsError:
    """Why `settings` cannot be read, from the environment or an environment's file, naming each variable to set."""
    if isinstance(error, ValidationError):
        problems = "; ".join(f"{variable(entry['loc'], settings)}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        return SettingsError(f"the settings are not valid, so the app cannot start: set {problems}")
    field = re.search(r'field "([^"]+)"', str(error))  # a value that does not parse before it is validated; pydantic-settings names the field in its message alone
    name = variable((field.group(1),)) if field is not None else f"the variable that starts {core.PREFIX}"
    return SettingsError(f"the settings are not valid, so the app cannot start: set {name}, whose value does not parse")
