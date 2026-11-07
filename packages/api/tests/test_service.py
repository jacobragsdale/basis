import sys
from collections.abc import Callable
from importlib.metadata import EntryPoint
from pathlib import Path
from types import FunctionType
from typing import Annotated

import pytest
from fastapi import APIRouter
from pydantic import BaseModel

from basis import api
from basis.api.example import service as example
from basis.api.settings import read
from basis.core.settings import Settings as CoreSettings


class Pricing(api.Settings):
    markup_bp: int = 0


class Other(api.Settings):
    region: str = "east"


def price(security: str) -> dict[str, str]:
    return {"security": security}


def priced(security: str, settings: Annotated[Other, api.given(Other)]) -> dict[str, str]:
    return {"security": security, "region": settings.region}


def priced_later(security: str, settings: "Annotated[Other, api.given(Other)]") -> dict[str, str]:  # noqa: UP037 -- a string annotation, on purpose
    return {"security": security, "region": settings.region}


def _get(path: str, handler: FunctionType) -> api.Route:
    return api.get(path, handler, deadline_s=1, response=dict[str, str], errors=[])


def _service(**changed: object) -> Callable[[], api.Service]:
    declared: dict[str, object] = {"name": "pricing", "settings": Pricing, "routes": [_get("/prices/{security}", price)]} | changed
    return lambda: api.Service(**declared)  # ty: ignore[invalid-argument-type] -- each case declares one thing wrong on purpose


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (_service(name="Pricing API"), "lower-case letters, digits and hyphens"),
        (_service(settings=CoreSettings), "not a subclass of api.Settings"),
        (_service(settings=BaseModel), "not a subclass of api.Settings"),
        (_service(routes=[]), "declares no routes"),
        (_service(routes=[APIRouter()]), "has a APIRouter among its routes"),
        (_service(routes=[_get("/prices/{security}", price), _get("/prices/{security}", price)]), "declares GET /prices/{security} twice"),
        (_service(routes=[_get("/prices/{security}", price), _get("/prices/{isin}", price)]), "/prices/{security} and /prices/{isin}, which match the same paths, twice"),
        (_service(routes=[_get("/prices/{security}", priced)]), "priced asks for Other, which service pricing does not give"),
        (_service(routes=[_get("/prices/{security}", priced_later)]), "priced_later asks for Other, which service pricing does not give"),
        (_service(fakes="pricing.dev"), "give fakes='module:function'"),
        (lambda: _get("prices", price), "start it with /"),
        (lambda: _get("/health/prices", price), "one the app answers itself"),
        (lambda: _get("/openapi.json", price), "one the app answers itself"),
        (lambda: _get("/prices/{security}", lambda security: {"security": security}), "not a named function"),
        (lambda: api.post("/prices", price, deadline_s=1, response=dict[str, str], errors=[], status=404), "give a 2xx"),
        (lambda: api.given(BaseModel), "basis gives a handler its service's settings"),  # ty: ignore[invalid-argument-type] -- wrong on purpose
    ],
    ids=[
        "name", "core-settings", "not-settings", "no-routes", "a-router", "twice", "twice-by-another-name", "asks-for-other-settings", "asks-in-a-string",
        "fakes-not-a-function", "relative-path", "health-path", "openapi-path", "lambda", "failure-status", "given-not-settings",
    ],
)  # fmt: skip
def test_a_service_cannot_skip_its_declarations(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


def test_a_handler_may_ask_for_a_base_of_its_services_settings() -> None:
    def general(settings: Annotated[api.Settings, api.given(api.Settings)]) -> dict[str, int]:
        return {"port": settings.serve.port}

    assert _service(routes=[api.get("/port", general, deadline_s=1, response=dict[str, int], errors=[])])().routes[0].name == "general"


@pytest.mark.parametrize(("declare", "method"), [(api.get, "GET"), (api.post, "POST"), (api.put, "PUT"), (api.patch, "PATCH"), (api.delete, "DELETE")])
def test_each_method_declares_its_own_routes(declare: Callable[..., api.Route], method: str) -> None:
    route = declare("/prices/{security}", price, deadline_s=1, response=dict[str, str], errors=[])
    assert (route.method, route.name) == (method, "price")


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


EXAMPLE = EntryPoint(name="accruals", value="basis.api.example.service:service", group="basis.apis")
BY_MODULE = EntryPoint(name="by-module", value="basis.api.example.service", group="basis.apis")


@pytest.mark.parametrize(("reference", "listed"), [("accruals", [EXAMPLE]), (None, [EXAMPLE]), ("by-module", [BY_MODULE, EXAMPLE])], ids=["by-name", "the-one-listed", "its-module"])
def test_a_service_loads_by_its_entry_point(reference: str | None, listed: list[EntryPoint]) -> None:
    assert api.load(reference, listed=listed) is example.service


@pytest.mark.parametrize("listed", [[], [EXAMPLE, BY_MODULE]], ids=["none", "two"])
def test_without_a_name_one_service_must_be_listed(listed: list[EntryPoint]) -> None:
    with pytest.raises(api.ServiceError, match="name the service to serve"):
        api.load(listed=listed)


def test_a_missing_import_of_the_services_module_is_not_a_missing_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pricing_service.py").write_text("import pricing\n", encoding="utf-8")  # its name starts with the missing one's
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.delitem(sys.modules, "pricing_service", raising=False)
    with pytest.raises(ModuleNotFoundError, match="No module named 'pricing'"):
        api.load("pricing_service")


def test_an_unknown_service_names_what_is_listed() -> None:
    with pytest.raises(api.ServiceError, match="no service named 'nowhere'"):
        api.load("nowhere")


def test_a_missing_setting_names_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Needs(api.Settings):
        desk: str

    monkeypatch.delenv("BASIS_DESK", raising=False)
    monkeypatch.setenv("BASIS_TRACES__EXPORTER", "otlp")
    with pytest.raises(api.SettingsError) as raised:
        read(Needs)
    assert "BASIS_DESK: Field required" in str(raised.value)
    assert "BASIS_TRACES: Value error, the otlp exporter needs an endpoint" in str(raised.value)


def test_a_value_that_does_not_parse_names_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_DATABASES", '{"warehouse": ')
    with pytest.raises(api.SettingsError, match="set BASIS_DATABASES, whose value does not parse: Expecting value"):
        read(api.Settings)


def test_given_settings_ignore_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_MARKUP_BP", "25")
    assert (Pricing().markup_bp, Pricing.given().markup_bp, Pricing.given(markup_bp=5).markup_bp) == (25, 0, 5)


def test_an_app_is_built_with_its_services_settings() -> None:
    with pytest.raises(api.ServiceError, match="build it with those, not a Other"):
        api.build(_service()(), settings=Other.given())
