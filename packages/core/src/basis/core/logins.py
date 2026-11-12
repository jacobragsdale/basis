"""How a part signs in to another service, as its settings' `login` says (`basis.core.settings.HttpLogin`): an httpx2
`Auth` that `basis.core.opened.client` gives the service's client, so every call through it, a job's fetch, an API's
call, `basis doctor`'s probe, carries the login, and a fake, which has no settings, carries none.

- `bearer`, `header` and `basic` put the secret in a header of each request.
- `client_credentials` and `entra` put a token in `Authorization: Bearer`. The token is fetched when a call first
  needs it, through the service's own client, so its proxy, CA bundle and deadline, and kept until `MARGIN_S` before
  it expires. Where the service answers 401 to a token kept from before, the token is fetched again, once, and the
  call made again: a token can be revoked before its time. One fetch at a time: the calls that wait for it share it.
  `entra` through workload identity reads what AKS's webhook gives the pod, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_FEDERATED_TOKEN_FILE` and `AZURE_AUTHORITY_HOST`, the one place basis reads the platform's variables for a
  login, as the lake's `workload_identity` does through pyarrow; and through the CLI it runs
  `az account get-access-token`.
- `negotiate` puts a Kerberos token, made by pyspnego for `SERVICE/HOST`, in `Authorization: Negotiate` on each
  request, with the user Windows logged on, or the ticket `kinit` got: a repo that signs in so takes
  `basis-core[kerberos]`.

A login the service or the identity provider refuses is `Denied`, naming the variables to correct; an identity
provider that cannot be reached is `Unavailable`, as the service would be. Neither a secret nor a token is logged."""

import asyncio
import base64
import importlib
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Protocol, override

import httpx2

from basis.core.errors import BadReply, Denied, Unavailable
from basis.core.settings import BasicLogin, BearerLogin, ClientCredentialsLogin, EntraLogin, HeaderLogin, HttpLogin, HttpService, NegotiateLogin, SettingsError, variable

log = logging.getLogger(__name__)
MARGIN_S = 60.0
"""How long before a token expires it is fetched again, so a call never carries one that expires on the way."""
UNSAID_S = 300.0
"""How long a token is kept whose reply says nothing of when it expires."""
AUTHORITY = "https://login.microsoftonline.com/"
"""Entra ID's authority, where the pod's `AZURE_AUTHORITY_HOST` names none."""
KERBEROS = "add basis-core[kerberos] to the repo's dependencies, as `uv add 'basis-core[kerberos]'`, which brings pyspnego"
_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


@dataclass(frozen=True)
class Token:
    value: str
    expires_at: float
    """When it expires, on `time.monotonic`'s clock."""


def auth(name: str, service: HttpService) -> httpx2.Auth | None:
    """The login of the service the settings name `name`, or None where they give none."""
    login: HttpLogin | None = service.login
    made: httpx2.Auth | None
    match login:
        case None:
            made = None
        case BearerLogin():
            made = Headers({"Authorization": f"Bearer {login.token.get_secret_value()}"})
        case HeaderLogin():
            made = Headers({login.name: login.value.get_secret_value()})
        case BasicLogin():
            pair = f"{login.user}:{login.password.get_secret_value()}".encode()
            made = Headers({"Authorization": f"Basic {base64.b64encode(pair).decode()}"})
        case ClientCredentialsLogin():
            made = Tokens(name, ClientCredentials(name, login))
        case EntraLogin(via="workload_identity"):
            made = Tokens(name, WorkloadIdentity(name, login.scope, os.environ))
        case EntraLogin():
            made = Tokens(name, AzureCli(name, login.scope))
        case NegotiateLogin():
            if find_spec("spnego") is None:
                msg = f"{name} signs in with Kerberos, and pyspnego is not installed: {KERBEROS}"
                raise SettingsError(msg)
            host, service_class = service.url.host or "", login.service
            made = Negotiate(name, lambda: spnego_token(host, service_class))
    return made


class Headers(httpx2.Auth):
    """A login that is a header, or several, on each request."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._headers = dict(headers)

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers.update(self._headers)
        yield request


class Source(Protocol):
    """Where a login's tokens come from: a request to an identity provider, whose reply `read` takes, or, where
    `request` gives None, `run`, which may wait, as a command does."""

    def request(self) -> httpx2.Request | None: ...

    def read(self, reply: httpx2.Response, now: float) -> Token: ...

    def run(self, now: float) -> Token: ...


def fetched(source: Source, client: httpx2.Client, *, clock: Callable[[], float] = time.monotonic) -> Token:
    """A token from `source`, through `client`, outside any call's flow: as settings fetch a vault's secrets."""
    asked = source.request()
    if asked is None:
        return source.run(clock())
    return source.read(client.send(asked), clock())


class Tokens(httpx2.Auth):
    """A login that is a bearer token from `source`, kept and fetched again as the module's docstring says."""

    def __init__(self, name: str, source: Source, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._name = name
        self._source = source
        self._clock = clock
        self._token: Token | None = None
        self._lock = asyncio.Lock()

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        stale: str | None = None
        while True:
            async with self._lock:
                kept = self._token
                fresh = kept is None or kept.expires_at - MARGIN_S <= self._clock() or kept.value == stale
                if kept is None or fresh:
                    asked = self._source.request()
                    if asked is None:
                        kept = await asyncio.to_thread(self._source.run, self._clock())
                    else:
                        reply = yield asked
                        await reply.aread()
                        kept = self._source.read(reply, self._clock())
                    self._token = kept
                    log.info("%s signed in", self._name, extra={"resource": self._name, "expires_in_s": round(kept.expires_at - self._clock())})
            request.headers["Authorization"] = f"Bearer {kept.value}"
            response = yield request
            if response.status_code != httpx2.codes.UNAUTHORIZED or fresh:
                return
            stale = kept.value  # a token kept from before, which the service no longer takes: fetched again, once
            await response.aread()


def _token(name: str, reply: httpx2.Response, now: float, *, fix: str) -> Token:
    """The token an OAuth 2.0 token endpoint's reply gives, refused as `Denied` where it refused the login."""
    url = str(reply.request.url.copy_with(query=None))
    if not reply.is_success:
        msg = f"{name}'s login was refused by {url} ({reply.status_code}): {fix}"
        raise Denied(msg, resource=name, status=reply.status_code)
    try:
        given = json.loads(reply.content)
        value = given["access_token"]
        lasts = float(given.get("expires_in", UNSAID_S))
    except (ValueError, TypeError, KeyError) as error:
        msg = f"{url} answered {name}'s login with no access_token"
        raise BadReply(msg, resource=name, status=reply.status_code) from error
    if not isinstance(value, str) or not value:
        msg = f"{url} answered {name}'s login with no access_token"
        raise BadReply(msg, resource=name, status=reply.status_code)
    return Token(value, now + lasts)


class ClientCredentials:
    """OAuth 2.0's client credentials grant, at the token URL the settings give."""

    def __init__(self, name: str, login: ClientCredentialsLogin) -> None:
        self._name = name
        self._login = login

    def request(self) -> httpx2.Request:
        login = self._login
        form = {"grant_type": "client_credentials", "client_id": login.client_id, "client_secret": login.client_secret.get_secret_value()}
        if login.scope is not None:
            form["scope"] = login.scope
        return httpx2.Request("POST", str(login.token_url), data=form, headers={"Accept": "application/json"})

    def read(self, reply: httpx2.Response, now: float) -> Token:
        names = ", ".join(variable(("http", self._name, "login", field)) for field in ("client_id", "client_secret"))
        return _token(self._name, reply, now, fix=f"correct {names}, or ask the identity provider's owners to grant the client")

    def run(self, now: float) -> Token:
        raise NotImplementedError(now)  # every token of this source is a request's


class WorkloadIdentity:
    """Entra ID's token for `scope`, for the pod's federated identity, which AKS's workload identity webhook sets up:
    its client and tenant, and the file of its service account's token, which the kubelet renews, read at each fetch."""

    def __init__(self, name: str, scope: str, environ: Mapping[str, str]) -> None:
        self._name = name
        self._scope = scope
        self._environ = environ

    def request(self) -> httpx2.Request:
        given = {key: self._environ.get(key, "") for key in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE")}
        if missing := sorted(key for key, value in given.items() if not value):
            fix = "run it in a pod whose service account AKS's workload identity webhook gives an identity, or sign in with via=cli on a desktop"
            msg = f"{self._name} signs in through workload identity, and this process has no {', '.join(missing)}: {fix}"
            raise Denied(msg, resource=self._name)
        assertion = Path(given["AZURE_FEDERATED_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        authority = self._environ.get("AZURE_AUTHORITY_HOST") or AUTHORITY
        url = f"{authority.rstrip('/')}/{given['AZURE_TENANT_ID']}/oauth2/v2.0/token"
        form = {"grant_type": "client_credentials", "client_id": given["AZURE_CLIENT_ID"], "scope": self._scope, "client_assertion_type": _ASSERTION, "client_assertion": assertion}
        return httpx2.Request("POST", url, data=form, headers={"Accept": "application/json"})

    def read(self, reply: httpx2.Response, now: float) -> Token:
        fix = f"grant the pod's identity {self._scope}, or correct {variable(('http', self._name, 'login', 'scope'))}"
        return _token(self._name, reply, now, fix=fix)

    def run(self, now: float) -> Token:
        raise NotImplementedError(now)  # every token of this source is a request's


class AzureCli:
    """Entra ID's token for `scope`, as the user `az login` signed in: `az account get-access-token`."""

    def __init__(self, name: str, scope: str, *, az: Sequence[str] | None = None) -> None:
        self._name = name
        self._scope = scope
        self._az = az

    def request(self) -> None:
        return None

    def read(self, reply: httpx2.Response, now: float) -> Token:
        raise NotImplementedError((reply, now))  # every token of this source is the CLI's

    def run(self, now: float) -> Token:
        found = shutil.which("az")
        az = self._az or ([found] if found is not None else None)
        if az is None:
            msg = f"{self._name} signs in as the user az login signed in, and the Azure CLI, az, is not on the PATH: install it and run `az login`"
            raise Denied(msg, resource=self._name)
        command = [*az, "account", "get-access-token", "--scope", self._scope, "--output", "json"]
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            said = done.stderr.strip().splitlines()[-1:] or ["no reason"]
            msg = f"{self._name}'s login through az was refused ({said[0]}): run `az login`, or ask the service's owners to grant you {self._scope}"
            raise Denied(msg, resource=self._name)
        try:
            given = json.loads(done.stdout)
            value = given["accessToken"]
            lasts = float(given["expires_on"]) - time.time() if "expires_on" in given else UNSAID_S
        except (ValueError, TypeError, KeyError) as error:
            msg = f"az answered {self._name}'s login with no accessToken"
            raise BadReply(msg, resource=self._name) from error
        return Token(str(value), now + lasts)


class Negotiate(httpx2.Auth):
    """Kerberos through SPNEGO: a token `made` gives, in a thread of its own, since the ticket's fetch may wait on the
    KDC, on each request."""

    def __init__(self, name: str, made: Callable[[], bytes]) -> None:
        self._name = name
        self._made = made

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        try:
            token = await asyncio.to_thread(self._made)
        except OSError as error:
            msg = f"{self._name}'s Kerberos login could not reach its KDC ({error})"
            raise Unavailable(msg, resource=self._name) from error
        except Exception as error:  # pyspnego's own, SpnegoError's kinds, which a missing ticket raises
            msg = f"{self._name}'s Kerberos login failed ({type(error).__name__}: {error}): sign in to Windows as a user the service knows, or run `kinit` in WSL or a pod"
            raise Denied(msg, resource=self._name) from error
        request.headers["Authorization"] = f"Negotiate {base64.b64encode(token).decode()}"
        yield request


def spnego_token(host: str, service: str) -> bytes:
    """pyspnego's first SPNEGO token for `SERVICE/HOST`, as the current user's credentials give it."""
    spnego = importlib.import_module("spnego")
    context = spnego.client(hostname=host, service=service, protocol="negotiate")
    token = context.step()
    if not isinstance(token, bytes):
        msg = f"pyspnego made no token for {service}/{host}"
        raise TypeError(msg)
    return token
