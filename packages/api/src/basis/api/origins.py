"""Which pages of other origins may call the API from a browser: none, unless the settings name them.

A UI and its API sit behind one host, the ingress routing `/api` to the API and the rest to the UI, so the browser makes
no cross-origin call and the API allows none. Where that routing is not possible, the API's settings name each origin
whose pages call it, per environment, as the browser sends it:

    BASIS_ALLOWED_ORIGINS='["https://positions.example", "http://localhost:5173"]'

Each is a scheme, `https` or `http`, and a host, with a port only where it is not the scheme's own, in lower case, and
nothing after: no path, not even `/`, and no `*`, since an origin basis allows is one the settings name. The app then
answers each of their preflight requests, and lets their pages send a request's bearer token, `Content-Type`,
`traceparent` and `tracestate`, and read its `X-Request-Id`, `traceparent` and `WWW-Authenticate`; it sends no
cookie's credentials, since a UI's token is a header. Without the setting, or with an empty list, a browser refuses
every call from another origin's page, as it does for any API that answers none of them."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
"""The methods a route answers, which a page of an allowed origin may send."""
HEADERS = ("Authorization", "Content-Type", "Idempotency-Key", "traceparent", "tracestate")
EXPOSED = ("X-Request-Id", "traceparent", "WWW-Authenticate")
PREFLIGHT_S = 600
"""How long a browser may keep an answer to its preflight request before it asks again."""
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(given: str) -> str:
    """`given`, an origin as a browser sends it in `Origin`, or a `ValueError` that says how to write it."""
    if given == "*":
        msg = "basis allows no wildcard: name each origin whose pages call the API, as https://positions.example"
        raise ValueError(msg)
    parts = urlsplit(given)
    hostname = parts.hostname
    if parts.scheme not in _DEFAULT_PORTS or hostname is None or hostname == "" or parts.username is not None or parts.password is not None:
        msg = f"{given!r} is not an origin: give a scheme, https or http, and a host, as https://positions.example"
        raise ValueError(msg)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port == -1:
        msg = f"{given!r} has no port a browser sends: give a number from 1 to 65535, or none for the scheme's own"
        raise ValueError(msg)
    host = f"[{hostname}]" if ":" in hostname else hostname  # an IPv6 address, as a URL brackets it
    written = f"{parts.scheme}://{host}" + (f":{port}" if port is not None and port != _DEFAULT_PORTS[parts.scheme] else "")
    if given != written:
        msg = f"{given!r} is not an origin as a browser sends it: give {written!r}, with nothing after its host and port"
        raise ValueError(msg)
    return given


type Origin = Annotated[str, AfterValidator(_origin)]
"""An origin as a browser sends it in `Origin`, such as `https://positions.example`."""


def middleware(allowed: tuple[str, ...]) -> list[Middleware]:
    """The middleware that answers the `allowed` origins' pages, outermost, so every answer the app makes reaches them,
    a 500 included; none when no origin is allowed."""
    if not allowed:
        return []
    return [
        Middleware(CORSMiddleware, allow_origins=list(allowed), allow_methods=list(METHODS), allow_headers=list(HEADERS), expose_headers=list(EXPOSED), allow_credentials=False, max_age=PREFLIGHT_S)
    ]
