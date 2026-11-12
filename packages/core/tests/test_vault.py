"""Azure Key Vault as a source of settings (step 12.9): a secret setting whose value is `keyvault:NAME` is the vault's
secret NAME, fetched once when the environment's settings load, signed in to Entra ID as the lake is. No real vault
verified this: `fakes.Vault` answers as Key Vault's REST API documents, its 200, 401, 403 and 404."""

import json
import sys
from pathlib import Path

import httpx2
import pytest

from basis.core import environments, fakes, vault
from basis.core.settings import GivenSettings, MssqlSettings, Settings, SettingsError

URL = "https://desk-kv.vault.azure.net/"


def given(**secrets: str) -> GivenSettings:
    return GivenSettings.model_validate(
        {
            "databases": {"warehouse": {"kind": "mssql", "connection_string": secrets.get("read", "Server=sql01"), "isolation": "snapshot", "timeout_s": 5, "max_rows": 5}},
            "slack": {"desk": {"webhook": secrets.get("webhook", "https://hooks.slack.com/services/T/B/x")}},
            "vault": {"url": URL, "via": "workload_identity"},
        }
    )


def read_login(settings: Settings) -> str:
    found = settings.databases["warehouse"]
    assert isinstance(found, MssqlSettings)
    return found.connection_string.get_secret_value()


@pytest.fixture
def pod(tmp_path: Path) -> dict[str, str]:
    """What AKS's workload identity webhook gives a pod."""
    (tmp_path / "token").write_text("eyJ.federated\n", encoding="utf-8")
    return {"AZURE_CLIENT_ID": "c-1", "AZURE_TENANT_ID": "t-1", "AZURE_FEDERATED_TOKEN_FILE": str(tmp_path / "token")}


def test_a_secret_that_names_the_vault_is_the_vaults_secret_and_the_rest_are_as_given(pod: dict[str, str]) -> None:
    found = fakes.Vault(URL, {"warehouse-read": "Server=sql01;UID=etl;PWD=from-the-vault"})
    resolved = vault.resolved(given(read="keyvault:warehouse-read"), transport=found.transport(), environ=pod)
    assert read_login(resolved) == "Server=sql01;UID=etl;PWD=from-the-vault"
    assert resolved.slack["desk"].webhook.get_secret_value() == "https://hooks.slack.com/services/T/B/x"
    assert found.asked == ["warehouse-read"]
    assert found.tokens == ["vault-token"]  # signed in for https://vault.azure.net/.default, as the pod's identity


def test_each_secret_is_fetched_once_however_many_settings_name_it(pod: dict[str, str]) -> None:
    found = fakes.Vault(URL, {"shared": "s3cret"})
    vault.resolved(given(read="keyvault:shared", webhook="keyvault:shared"), transport=found.transport(), environ=pod)
    assert found.asked == ["shared"]


def test_settings_that_name_no_vault_secret_reach_no_vault() -> None:
    plain = given()
    assert vault.resolved(plain, transport=httpx2.MockTransport(lambda _: pytest.fail("reached the vault"))) is plain


@pytest.mark.parametrize(
    ("secrets", "named", "says"),
    [
        ({}, "keyvault:missing", r"BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING names the vault's secret missing, and https://desk-kv\.vault\.azure\.net/ has none \(404 SecretNotFound\)"),
        ({"locked": fakes.FORBIDDEN}, "keyvault:locked", r"https://desk-kv\.vault\.azure\.net/ refused to give its secret locked \(403 Forbidden\): grant this identity get on the vault's secrets"),
        ({}, "keyvault:not a name", r"'not a name' is no Key Vault secret's name"),
    ],
)
def test_a_secret_the_vault_cannot_give_is_one_settings_error_naming_the_variable(pod: dict[str, str], secrets: dict[str, str], named: str, says: str) -> None:
    with pytest.raises(SettingsError, match=says):
        vault.resolved(given(read=named), transport=fakes.Vault(URL, secrets).transport(), environ=pod)


def test_a_reference_without_a_vault_says_which_to_set() -> None:
    settings = given(read="keyvault:warehouse-read").model_copy(update={"vault": None})
    with pytest.raises(SettingsError, match=r"BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING names a Key Vault secret, and no vault is configured: set BASIS_VAULT__URL and BASIS_VAULT__VIA"):
        vault.resolved(settings)


def test_a_login_the_vault_refuses_is_one_settings_error(pod: dict[str, str]) -> None:
    found = fakes.Vault(URL, {"warehouse-read": "x"}, accepts="another-token")
    with pytest.raises(SettingsError, match=r"https://desk-kv\.vault\.azure\.net/ refused the login \(401\): grant this identity get on the vault's secrets"):
        vault.resolved(given(read="keyvault:warehouse-read"), transport=found.transport(), environ=pod)


def test_the_cli_signs_in_as_az_login_did(tmp_path: Path) -> None:
    script = tmp_path / "az.py"
    script.write_text("import json\nprint(json.dumps({'accessToken': 'vault-token', 'expires_on': 4102444800}))\n", encoding="utf-8")
    found = fakes.Vault(URL, {"warehouse-read": "from-the-vault"})
    settings = given(read="keyvault:warehouse-read").model_copy(update={"vault": vault.VaultSettings.model_validate({"url": URL, "via": "cli"})})
    resolved = vault.resolved(settings, transport=found.transport(), az=[sys.executable, str(script)])
    assert read_login(resolved) == "from-the-vault"


def test_an_environment_resolves_its_vault_secrets_as_it_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    monkeypatch.setattr(vault, "resolved", lambda settings, **_: called.append(settings) or settings)
    folder = tmp_path / "config" / "basis"
    folder.mkdir(parents=True)
    (folder / "dev.env").write_text("BASIS_ENV=dev\nBASIS_HTTP__PRICING__URL=https://pricing.example/\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    environments.load("dev", Settings, windows=False, home=tmp_path)
    assert len(called) == 1


def test_a_fake_vault_answers_as_key_vault_documents() -> None:
    """The replies `fakes.Vault` gives, as Key Vault's REST API reference documents them for Get Secret."""
    found = fakes.Vault(URL, {"a": "1"})
    client = httpx2.Client(transport=found.transport())
    unsigned = client.get(f"{URL}secrets/a?api-version={vault.API}")
    assert unsigned.status_code == 401
    assert unsigned.headers["www-authenticate"].startswith('Bearer authorization="https://login.microsoftonline.com/')
    signed = client.get(f"{URL}secrets/a?api-version={vault.API}", headers={"Authorization": "Bearer vault-token"})
    assert (signed.status_code, signed.json()["value"], signed.json()["id"].startswith(f"{URL}secrets/a/")) == (200, "1", True)
    missing = client.get(f"{URL}secrets/b?api-version={vault.API}", headers={"Authorization": "Bearer vault-token"})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "SecretNotFound")
    assert json.loads(client.get(f"{URL}secrets/a", headers={"Authorization": "Bearer vault-token"}).content)["error"]["code"] == "BadParameter"  # no api-version
