import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import APIRouter
from pydantic import BaseModel

from basis import api
from basis.api.example import service as example
from basis.api.settings import read, variable
from basis.core.settings import Settings as CoreSettings


class Pricing(api.Settings):
    markup_bp: int = 0


class Other(api.Settings):
    region: str = "east"


def price(security: str) -> dict[str, str]:
    return {"security": security}


def priced(security: str, settings: Annotated[Other, api.given(Other)]) -> dict[str, str]:
    return {"security": security, "region": settings.region}


def _service(**changed: object) -> Callable[[], api.Service]:
    declared: dict[str, object] = {"name": "pricing", "settings": Pricing, "routes": [api.get("/prices/{security}", price)]} | changed
    return lambda: api.Service(**declared)  # ty: ignore[invalid-argument-type] -- each case declares one thing wrong on purpose


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (_service(name="Pricing API"), "lower-case letters, digits and hyphens"),
        (_service(settings=CoreSettings), "not a subclass of api.Settings"),
        (_service(settings=BaseModel), "not a subclass of api.Settings"),
        (_service(routes=[]), "declares no routes"),
        (_service(routes=[APIRouter()]), "has a APIRouter among its routes"),
        (_service(routes=[api.get("/prices/{security}", price), api.get("/prices/{security}", price)]), "declares GET /prices/{security} twice"),
        (_service(routes=[api.get("/prices/{security}", priced)]), "priced asks for Other, which service pricing does not give"),
        (lambda: api.get("prices", price), "start it with /"),
        (lambda: api.get("/health/prices", price), "one the app answers itself"),
        (lambda: api.get("/openapi.json", price), "one the app answers itself"),
        (lambda: api.get("/prices/{security}", lambda security: {"security": security}), "not a named function"),
        (lambda: api.post("/prices", price, status=404), "give a 2xx"),
        (lambda: api.given(BaseModel), "basis gives a handler its service's settings"),  # ty: ignore[invalid-argument-type] -- wrong on purpose
    ],
    ids=[
        "name", "core-settings", "not-settings", "no-routes", "a-router", "twice", "asks-for-other-settings",
        "relative-path", "health-path", "openapi-path", "lambda", "failure-status", "given-not-settings",
    ],
)  # fmt: skip
def test_a_service_cannot_skip_its_declarations(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


def test_a_handler_may_ask_for_a_base_of_its_services_settings() -> None:
    def general(settings: Annotated[api.Settings, api.given(api.Settings)]) -> dict[str, int]:
        return {"port": settings.serve.port}

    assert _service(routes=[api.get("/port", general)])().routes[0].name == "general"


@pytest.mark.parametrize("reference", ["basis.api.example.service", "basis.api.example.service:service"])
def test_a_service_loads_by_its_module(reference: str) -> None:
    assert api.load(reference) is example.service


@pytest.mark.parametrize(
    ("module", "source", "message"),
    [("homemade", "from fastapi import FastAPI\nservice = FastAPI()\n", "an app the service made itself, which basis does not serve"), ("undeclared", "service = None\n", "is not an api.Service")],
    ids=["own-app", "no-service"],
)
def test_basis_serves_only_a_declared_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, source: str, message: str) -> None:
    (tmp_path / f"{module}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(api.ServiceError, match=message):
        api.load(module)


def test_an_unknown_service_names_what_is_listed() -> None:
    with pytest.raises(api.ServiceError, match="no service named 'nowhere'"):
        api.load("nowhere")


def test_a_missing_setting_names_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Needs(api.Settings):
        desk: str

    prefix = str(api.Settings.model_config.get("env_prefix") or "")  # whatever basis-core's prefix is
    for name in (f"{prefix}DESK", f"{prefix}TRACES__EXPORTER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(f"{prefix}TRACES__EXPORTER", "otlp")
    with pytest.raises(api.SettingsError) as raised:
        read(Needs)
    assert f"{prefix}DESK: Field required" in str(raised.value)
    assert f"{prefix}TRACES: Value error, the otlp exporter needs an endpoint" in str(raised.value)


def test_a_variable_is_named_by_the_settings_own_prefix() -> None:
    prefix = str(api.Settings.model_config.get("env_prefix") or "")
    assert variable(api.Settings, ("serve", "port")) == f"{prefix}SERVE__PORT"


def test_given_settings_ignore_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = str(api.Settings.model_config.get("env_prefix") or "")
    monkeypatch.setenv(f"{prefix}MARKUP_BP", "25")
    assert (Pricing().markup_bp, Pricing.given().markup_bp, Pricing.given(markup_bp=5).markup_bp) == (25, 0, 5)


def test_an_app_is_built_with_its_services_settings() -> None:
    with pytest.raises(api.ServiceError, match="build it with those, not a Other"):
        api.build(_service()(), settings=Other.given())
