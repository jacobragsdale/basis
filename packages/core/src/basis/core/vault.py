"""Azure Key Vault as a source of settings: a secret setting, such as a connection string, a token or a webhook, whose
value is `keyvault:NAME` is the secret NAME of the vault `BASIS_VAULT__*` configures (`settings.VaultSettings`), so an
environment's file or a deployment names its secrets rather than holding them. `environments.load` resolves them as
the settings load, each secret fetched once, before any command reaches anything, so a missing or refused secret is one
`ERROR` line naming the variable, and nothing has started.

The vault is read through its REST API, `GET {vault}/secrets/{NAME}?api-version=API`, with no Azure SDK: a bearer token
for `https://vault.azure.net/.default`, from Entra ID as the pod's workload identity or the user `az login` signed in
(`basis.core.logins`), through the vault's own CA bundle and proxy, never the environment's. No real vault verified it:
`fakes.Vault` answers as the REST API's reference documents Get Secret."""

import os
import re
import ssl
from collections.abc import Mapping, Sequence

import httpx2
from pydantic import BaseModel, SecretStr

from basis.core import logins
from basis.core.errors import BadReply, Denied
from basis.core.settings import Settings, SettingsError, VaultSettings, variable

__all__ = ["API", "REFERENCE", "SCOPE", "VaultSettings", "resolved"]

REFERENCE = "keyvault:"
"""What a secret setting's value starts with when it names a vault's secret."""
API = "7.5"
"""The Key Vault REST API's version basis asks for."""
SCOPE = "https://vault.azure.net/.default"
"""The scope of a token Key Vault takes."""
TIMEOUT_S = 30.0
"""How long each call to the vault or Entra ID may take."""
_NAME = re.compile(r"^[0-9A-Za-z-]{1,127}$")
"""A Key Vault secret's name: letters, digits and dashes, 127 at most."""

type Location = tuple[int | str, ...]


def resolved[S: Settings](found: S, *, transport: httpx2.BaseTransport | None = None, environ: Mapping[str, str] = os.environ, az: Sequence[str] | None = None) -> S:
    """`found`, each secret that names a vault's secret replaced by it, or `found` itself where none does. `transport`,
    `environ` and `az` are a test's."""
    named = dict(_references(found, ()))
    if not named:
        return found
    if found.vault is None:
        first = variable(next(iter(named)))
        msg = f"{first} names a Key Vault secret, and no vault is configured: set BASIS_VAULT__URL and BASIS_VAULT__VIA in the environment's settings"
        raise SettingsError(msg)
    for location, name in named.items():
        if not _NAME.match(name):
            msg = f"{variable(location)}: {name!r} is no Key Vault secret's name, which is letters, digits and dashes: correct it"
            raise SettingsError(msg)
    configured = found.vault
    with _client(configured, transport) as client:
        source: logins.Source = logins.WorkloadIdentity("vault", SCOPE, environ) if configured.via == "workload_identity" else logins.AzureCli("vault", SCOPE, az=az)
        try:
            token = logins.fetched(source, client)
        except (Denied, BadReply) as error:
            raise SettingsError(str(error)) from error
        except httpx2.HTTPError as error:
            msg = f"Entra ID could not be reached to sign in to {configured.url} ({type(error).__name__}): check BASIS_VAULT__PROXY and BASIS_VAULT__CA_BUNDLE, and that this machine may reach it"
            raise SettingsError(msg) from error
        secrets: dict[str, str] = {}
        for location, name in named.items():
            if name not in secrets:  # each secret once, however many settings name it
                secrets[name] = _secret(client, configured, token.value, name, location)
    return _replaced(found, {location: secrets[name] for location, name in named.items()})


def _references(value: object, at: Location) -> list[tuple[Location, str]]:
    """Each secret within `value` that names a vault's secret, by its location in the settings, and the name."""
    match value:
        case SecretStr() if (text := value.get_secret_value()).startswith(REFERENCE):
            return [(at, text.removeprefix(REFERENCE))]
        case BaseModel():
            return [found for field in type(value).model_fields for found in _references(getattr(value, field), (*at, field))]
        case dict():
            return [found for key, item in value.items() for found in _references(item, (*at, str(key)))]
        case _:
            return []


def _replaced[M: BaseModel](model: M, values: Mapping[Location, str]) -> M:
    """`model` with the secret at each location replaced by its value, and the rest as it was."""
    update: dict[str, object] = {}
    for field in type(model).model_fields:
        inner = {location[1:]: value for location, value in values.items() if location[0] == field}
        if inner:
            update[field] = _within(getattr(model, field), inner)
    return model.model_copy(update=update)


def _within(value: object, values: Mapping[Location, str]) -> object:
    if () in values:
        return SecretStr(values[()])
    if isinstance(value, BaseModel):
        return _replaced(value, values)
    if isinstance(value, dict):
        return {key: _within(item, {location[1:]: text for location, text in values.items() if location[0] == str(key)}) for key, item in value.items()}
    return value


def _client(configured: VaultSettings, transport: httpx2.BaseTransport | None) -> httpx2.Client:
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=configured.ca_bundle) if configured.ca_bundle is not None else True
    proxy = str(configured.proxy) if configured.proxy is not None else None
    return httpx2.Client(verify=verify, proxy=proxy, trust_env=False, timeout=TIMEOUT_S, transport=transport)


def _secret(client: httpx2.Client, configured: VaultSettings, token: str, name: str, location: Location) -> str:
    """The vault's secret `name`, which the setting at `location` names, or one `SettingsError` saying why not."""
    url = str(configured.url)
    try:
        reply = client.get(f"{url.rstrip('/')}/secrets/{name}", params={"api-version": API}, headers={"Authorization": f"Bearer {token}"})
    except httpx2.HTTPError as error:
        msg = f"{url} could not be reached for its secret {name} ({type(error).__name__}): check BASIS_VAULT__URL and BASIS_VAULT__PROXY, and that this machine may reach it"
        raise SettingsError(msg) from error
    code = _code(reply)
    match reply.status_code:
        case 200:
            value = reply.json().get("value")
            if isinstance(value, str):
                return value
            msg = f"{url} answered its secret {name} with no value: report it"
        case 401:
            msg = f"{url} refused the login (401): grant this identity get on the vault's secrets, or correct BASIS_VAULT__VIA"
        case 403:
            msg = f"{url} refused to give its secret {name} (403 {code}): grant this identity get on the vault's secrets"
        case 404:
            msg = f"{variable(location)} names the vault's secret {name}, and {url} has none (404 {code}): add it to the vault, or correct the name"
        case status:
            msg = f"{url} answered {status} {code} for its secret {name}: try again, and report it if it does the same"
    raise SettingsError(msg)


def _code(reply: httpx2.Response) -> str:
    """The error code Key Vault's reply gives, as `SecretNotFound`, or its status's phrase."""
    try:
        found = reply.json().get("error", {}).get("code")
    except ValueError:
        found = None
    return str(found) if found else reply.reason_phrase
