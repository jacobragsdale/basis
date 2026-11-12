"""A service's login (step 12.2): each kind its settings name signs each call in, a token is fetched once and kept
until it nears its expiry, fetched again once when the service answers 401 to a kept one, and a refused login is
`Denied`, naming the variables to correct."""

import asyncio
import base64
import json
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx2
import pytest

from basis.core import logins, opened
from basis.core.errors import Denied, Unavailable
from basis.core.logins import AzureCli, ClientCredentials, Negotiate, Tokens, WorkloadIdentity
from basis.core.settings import ClientCredentialsLogin, FileSettings, HttpService, SettingsError

type Handler = Callable[[httpx2.Request], httpx2.Response]


def service(login: dict[str, object] | None) -> HttpService:
    return HttpService.model_validate({"url": "https://pricing.internal.example/api/", "login": login})


def answered(handler: Handler, auth: httpx2.Auth | None, *paths: str) -> list[httpx2.Response]:
    async def calls() -> list[httpx2.Response]:
        async with httpx2.AsyncClient(base_url="https://pricing.internal.example/api/", transport=httpx2.MockTransport(handler), auth=auth) as client:
            return [await client.get(path) for path in paths]

    return asyncio.run(calls())


class Provider:
    """An identity provider and a service at once: the token URL answers tokens, numbered, each for `lasts_s`; the
    service answers 401 to any token in `revoked`, and otherwise 200 with the Authorization it was sent."""

    def __init__(self, *, lasts_s: float = 3600, refuse: int | None = None) -> None:
        self.lasts_s = lasts_s
        self.refuse = refuse
        self.forms: list[dict[str, list[str]]] = []
        self.revoked: set[str] = set()
        self.seen: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            self.forms.append(parse_qs(request.content.decode()))
            if self.refuse is not None:
                return httpx2.Response(self.refuse, json={"error": "invalid_client"})
            return httpx2.Response(200, json={"access_token": f"t{len(self.forms)}", "token_type": "Bearer", "expires_in": self.lasts_s})
        given = request.headers.get("authorization", "")
        self.seen.append(given)
        return httpx2.Response(401 if given.removeprefix("Bearer ") in self.revoked else 200, text=given)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


CREDENTIALS = {"kind": "client_credentials", "token_url": "https://login.example/tenant/oauth2/v2.0/token", "client_id": "pricing-etl", "client_secret": "s3cret", "scope": "api://pricing/.default"}


@pytest.mark.parametrize(
    ("login", "header", "value"),
    [
        ({"kind": "bearer", "token": "abc"}, "authorization", "Bearer abc"),
        ({"kind": "header", "name": "X-API-Key", "value": "k1"}, "x-api-key", "k1"),
        ({"kind": "basic", "user": "etl", "password": "pw"}, "authorization", f"Basic {base64.b64encode(b'etl:pw').decode()}"),
    ],
)
def test_a_login_that_is_a_header_signs_every_call(login: dict[str, object], header: str, value: str) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get(header))
        return httpx2.Response(200)

    answered(handler, logins.auth("pricing", service(login)), "a", "b")
    assert seen == [value, value]


def test_without_a_login_a_call_carries_none() -> None:
    assert logins.auth("pricing", service(None)) is None
    assert opened.client("pricing", service(None)).auth is None


def test_the_services_client_carries_its_login() -> None:
    assert isinstance(opened.client("pricing", service({"kind": "bearer", "token": "abc"})).auth, logins.Headers)


def test_a_login_header_names_a_header() -> None:
    with pytest.raises(ValueError, match="name"):
        service({"kind": "header", "name": "X API Key", "value": "k"})


def test_a_login_reads_from_the_settings_variables(tmp_path: Path) -> None:
    path = tmp_path / "dev.env"
    path.write_text(
        "BASIS_HTTP__PRICING__URL=https://pricing.internal.example/api/\nBASIS_HTTP__PRICING__LOGIN__KIND=client_credentials\n"
        "BASIS_HTTP__PRICING__LOGIN__TOKEN_URL=https://login.example/t/oauth2/v2.0/token\nBASIS_HTTP__PRICING__LOGIN__CLIENT_ID=etl\n"
        "BASIS_HTTP__PRICING__LOGIN__CLIENT_SECRET=s3cret\n",
        encoding="utf-8",
    )
    login = FileSettings(_env_file=path).http["pricing"].login
    assert isinstance(login, ClientCredentialsLogin)
    assert login.client_id == "etl"
    assert "s3cret" not in repr(login)


def test_a_token_is_fetched_once_kept_and_fetched_again_before_it_expires() -> None:
    provider = Provider(lasts_s=600)
    clock = Clock()
    login = ClientCredentialsLogin.model_validate(CREDENTIALS)
    tokens = Tokens("pricing", ClientCredentials("pricing", login), clock=clock)
    answered(provider, tokens, "a", "b")
    clock.now += 600 - logins.MARGIN_S  # within a minute of its expiry
    answered(provider, tokens, "c")
    assert provider.seen == ["Bearer t1", "Bearer t1", "Bearer t2"]
    assert provider.forms[0] == {"grant_type": ["client_credentials"], "client_id": ["pricing-etl"], "client_secret": ["s3cret"], "scope": ["api://pricing/.default"]}


def test_a_kept_token_the_service_refuses_is_fetched_again_once_and_the_call_made_again() -> None:
    provider = Provider()
    tokens = Tokens("pricing", ClientCredentials("pricing", ClientCredentialsLogin.model_validate(CREDENTIALS)), clock=Clock())
    answered(provider, tokens, "a")
    provider.revoked.add("t1")
    [response] = answered(provider, tokens, "b")
    assert response.status_code == 200
    assert provider.seen == ["Bearer t1", "Bearer t1", "Bearer t2"]


def test_a_fresh_token_the_service_refuses_is_its_401_not_another_fetch() -> None:
    provider = Provider()
    provider.revoked.update({"t1", "t2"})
    tokens = Tokens("pricing", ClientCredentials("pricing", ClientCredentialsLogin.model_validate(CREDENTIALS)), clock=Clock())
    [response] = answered(provider, tokens, "a")
    assert response.status_code == 401
    assert len(provider.forms) == 1


def test_a_login_the_identity_provider_refuses_is_denied_naming_the_variables() -> None:
    tokens = Tokens("pricing", ClientCredentials("pricing", ClientCredentialsLogin.model_validate(CREDENTIALS)), clock=Clock())
    with pytest.raises(Denied, match=r"pricing's login was refused by https://login\.example/tenant/oauth2/v2\.0/token \(401\): correct BASIS_HTTP__PRICING__LOGIN__CLIENT_ID") as raised:
        answered(Provider(refuse=401), tokens, "a")
    assert "s3cret" not in str(raised.value)


def test_calls_at_once_share_one_token() -> None:
    provider = Provider()
    tokens = Tokens("pricing", ClientCredentials("pricing", ClientCredentialsLogin.model_validate(CREDENTIALS)), clock=Clock())

    async def calls() -> None:
        async with httpx2.AsyncClient(base_url="https://pricing.internal.example/api/", transport=httpx2.MockTransport(provider), auth=tokens) as client:
            await asyncio.gather(*(client.get(str(n)) for n in range(5)))

    asyncio.run(calls())
    assert len(provider.forms) == 1
    assert set(provider.seen) == {"Bearer t1"}


def test_workload_identity_exchanges_the_pods_federated_token_at_its_tenant(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("eyJ.federated.jwt\n", encoding="utf-8")
    environ = {"AZURE_CLIENT_ID": "c-1", "AZURE_TENANT_ID": "tenant-1", "AZURE_FEDERATED_TOKEN_FILE": str(tmp_path / "token"), "AZURE_AUTHORITY_HOST": "https://login.example/"}
    asked = WorkloadIdentity("pricing", "api://pricing/.default", environ).request()
    assert str(asked.url) == "https://login.example/tenant-1/oauth2/v2.0/token"
    assert parse_qs(asked.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["c-1"],
        "scope": ["api://pricing/.default"],
        "client_assertion_type": ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"],
        "client_assertion": ["eyJ.federated.jwt"],
    }
    provider = Provider()
    answered(provider, Tokens("pricing", WorkloadIdentity("pricing", "api://pricing/.default", environ), clock=Clock()), "a")
    assert provider.seen == ["Bearer t1"]


def test_workload_identity_outside_a_pod_says_what_is_missing() -> None:
    with pytest.raises(Denied, match="this process has no AZURE_CLIENT_ID, AZURE_FEDERATED_TOKEN_FILE, AZURE_TENANT_ID: run it in a pod"):
        WorkloadIdentity("pricing", "api://pricing/.default", {}).request()


def az(tmp_path: Path, *, answer: dict[str, object] | None, code: int = 0) -> list[str]:
    """A stand-in for the Azure CLI, a script that records its arguments and answers as `az account get-access-token`."""
    script = tmp_path / "az.py"
    script.write_text(
        f"import json, sys\nopen({str(tmp_path / 'argv')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(answer)!r} if {code} == 0 else '')\nsys.stderr.write('ERROR: Please run az login to setup account.\\n')\nsys.exit({code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_the_cli_login_takes_az_logins_token_for_the_scope(tmp_path: Path) -> None:
    command = az(tmp_path, answer={"accessToken": "cli-token", "expires_on": 4102444800, "tokenType": "Bearer"})
    provider = Provider()
    answered(provider, Tokens("pricing", AzureCli("pricing", "api://pricing/.default", az=command), clock=Clock()), "a")
    assert provider.seen == ["Bearer cli-token"]
    assert json.loads((tmp_path / "argv").read_text(encoding="utf-8")) == ["account", "get-access-token", "--scope", "api://pricing/.default", "--output", "json"]


def test_a_cli_login_az_refuses_is_denied_with_its_reason(tmp_path: Path) -> None:
    tokens = Tokens("pricing", AzureCli("pricing", "api://pricing/.default", az=az(tmp_path, answer=None, code=1)), clock=Clock())
    with pytest.raises(Denied, match=r"pricing's login through az was refused \(ERROR: Please run az login to setup account\.\): run `az login`"):
        answered(Provider(), tokens, "a")


def test_negotiate_sends_a_kerberos_token_on_each_call() -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200)

    answered(handler, Negotiate("pricing", lambda: b"krb"), "a", "b")
    assert seen == ["Negotiate a3Ji", "Negotiate a3Ji"]


@pytest.mark.parametrize(("raised", "kind"), [(RuntimeError("No Kerberos credentials available"), Denied), (ConnectionRefusedError("KDC"), Unavailable)])
def test_a_kerberos_login_that_fails_is_denied_or_unavailable(raised: Exception, kind: type[Exception]) -> None:
    def made() -> bytes:
        raise raised

    with pytest.raises(kind, match="pricing's Kerberos login"):
        answered(lambda _: httpx2.Response(200), Negotiate("pricing", made), "a")


def test_negotiate_without_pyspnego_says_which_extra_to_add(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logins, "find_spec", lambda _: None)
    with pytest.raises(SettingsError, match=r"pricing signs in with Kerberos, and pyspnego is not installed: add basis-core\[kerberos\]"):
        logins.auth("pricing", service({"kind": "negotiate"}))
