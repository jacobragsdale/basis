"""Each request's boundary, the app's outermost middleware of its own: basis-core's (`basis.core.serving.requests`,
whose docstring says what it does), with what is an API's own. An exception nothing expected becomes a 500 problem,
with the request's id, and each request's line names the user, in a service that declares one. FastAPI's telemetry,
around it, has already read the caller's `traceparent` and started the server span, which is the current span here.

A health check makes no span, and logs only when it fails, at INFO."""

from collections.abc import Mapping
from uuid import UUID

from starlette.types import ASGIApp, Scope

from basis.api import errors
from basis.api.identity import USER, User
from basis.core.serving import HEALTH
from basis.core.serving import requests as boundary
from basis.core.serving.requests import REQUEST_ID

__all__ = ["HEALTH", "REQUEST_ID", "Requests"]


class Requests(boundary.Requests):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app, answer=_problem, fields=_user)


def _problem(error: Exception, request_id: UUID) -> ASGIApp:
    return errors.response(error, request_id)


def _user(scope: Scope) -> Mapping[str, object]:
    if USER not in scope:  # verified inside the boundary, in a service that declares a user: who made the request, or None
        return {}
    user = scope[USER]
    return {"user": user.subject if isinstance(user, User) else None}
