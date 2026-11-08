"""The two OpenTelemetry variables basis reads, the one exception to its settings being its only configuration
(design.md, "Traces and logs"): `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`, which say what a process is, never
where its spans go. A deploy platform sets them per container, as berth does, from what it deployed: the service as
`<app>-<component>`, the same name its Datadog tags and metrics carry, and the environment, the version and the app.
Where they are set, a run's or an app's spans carry them in place of basis's own names (`traces.provider`), and every
log record that names its service names the platform's (`logs`), so a process's traces, logs and metrics are one
service in Datadog. Nothing else of `OTEL_*` is read: the exporter, its endpoint, headers and limits stay basis's
settings'. An attribute that names another environment than the one the process acts in is refused, since a pod
deployed to one environment with another's settings would write where nobody meant it to."""

import os
from collections.abc import Mapping
from urllib.parse import unquote

from basis.core.errors import BasisError

SERVICE_NAME = "OTEL_SERVICE_NAME"
RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"
ENVIRONMENT = "deployment.environment.name"


class TelemetryError(BasisError, ValueError):
    """An OpenTelemetry variable basis cannot read, or one that disagrees with the process's environment; the message
    gives the fix."""


def service_name(environ: Mapping[str, str] | None = None) -> str | None:
    """The service the platform names this process, `OTEL_SERVICE_NAME`, or None where it names none."""
    name = (os.environ if environ is None else environ).get(SERVICE_NAME, "").strip()
    return name or None


def attributes(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The resource attributes the platform gives this process, `OTEL_RESOURCE_ATTRIBUTES`: `key=value` pairs, comma
    separated, each value percent-encoded, as OpenTelemetry's specification writes them."""
    text = (os.environ if environ is None else environ).get(RESOURCE_ATTRIBUTES, "")
    found: dict[str, str] = {}
    for pair in (part.strip() for part in text.split(",")):
        if not pair:
            continue
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            msg = f"{RESOURCE_ATTRIBUTES} holds {pair!r}, which is not key=value: write it as key=value pairs, comma separated, as service.version=1.2.3"
            raise TelemetryError(msg)
        found[key.strip()] = unquote(value.strip())
    return found


def resource(service: str, *, version: str | None, environment: str | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The attributes a process's spans carry: basis's, `service` and, where known, `version` and `environment`, with
    the platform's in their place where it sets them. Refused when the platform's environment is not `environment`."""
    given = attributes(environ)
    if environment is not None and (named := given.get(ENVIRONMENT)) is not None and named != environment:
        msg = (
            f"{RESOURCE_ATTRIBUTES} says this process was deployed to {named}, and it acts in {environment}: give the deployment {environment}'s BASIS_ENV and settings, or deploy it to {environment}"
        )
        raise TelemetryError(msg)
    found = {"service.name": service}
    if version is not None:
        found["service.version"] = version
    if environment is not None:
        found[ENVIRONMENT] = environment
    found |= given
    if (name := service_name(environ)) is not None:
        found["service.name"] = name
    return found
