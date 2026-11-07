import logging
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import MappingProxyType
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import AliasChoices, AliasPath, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic.alias_generators import to_camel

from basis import api
from basis.api import tokens
from basis.api.identity import Machine, refuse_development
from basis.api.settings import read

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

log = logging.getLogger("basis.api.tests")

ISSUER = "basis-development"
AUDIENCE = "positions"
ISSUED, EXPIRES = datetime(2026, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)  # the app checks them against its own clock
DEVELOPMENT = {"issuer": ISSUER, "audience": AUDIENCE, "algorithms": ["ES256"], "leeway_s": 30, "keys": {"kind": "development"}}


class Trader(api.User):
    desk: str
    roles: tuple[str, ...] = ()


def me(user: Annotated[Trader | None, api.given(Trader)]) -> dict[str, object]:
    log.info("showing positions")
    if user is None:
        msg = "positions are shown to a signed-in user"
        raise api.SignInRequiredError(msg)
    return {"subject": user.subject, "desk": user.desk, "roles": list(user.roles)}


def greeting(user: Annotated[Trader | None, api.given(Trader)]) -> dict[str, str]:
    return {"hello": user.subject if user is not None else "anonymous"}


def ping() -> dict[str, str]:
    log.info("pinged")
    return {"ping": "pong"}


SERVICE = api.Service(
    name="positions",
    settings=api.Settings,
    user=Trader,
    routes=[
        api.get("/me", me, deadline_s=5, response=dict[str, object], errors=[]),
        api.get("/greeting", greeting, deadline_s=5, response=dict[str, str], errors=[]),
        api.get("/ping", ping, deadline_s=5, response=dict[str, str], errors=[]),
    ],
)


def _app(**identity: object) -> api.App:
    return api.build(SERVICE, settings=api.Settings.given(identity=DEVELOPMENT | identity))


def _signed(subject: str = "jsmith", **claims: object) -> dict[str, str]:
    settings = api.Settings.given(identity=DEVELOPMENT)
    assert settings.identity is not None
    found = tokens.sign(settings.identity, subject, issued=ISSUED, expires=EXPIRES, claims={"desk": "rates", **claims})  # ty: ignore[invalid-argument-type] -- JSON claims
    return {"authorization": f"Bearer {found}"}


def _of(records: list[dict[str, object]], request_id: str) -> list[dict[str, object]]:
    return [record for record in records if record.get("request_id") == request_id]


def test_a_signed_in_handler_gets_its_user_with_the_claims_its_service_declares(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get("/me", headers=_signed(roles=["trader"], email="jsmith@example.com"))
    assert (response.status_code, response.json()) == (HTTPStatus.OK, {"subject": "jsmith", "desk": "rates", "roles": ["trader"]})
    records = _of(logged(), response.headers["x-request-id"])
    assert [(record["message"], record["user"]) for record in records] == [("showing positions", "jsmith"), ("GET /me: 200", "jsmith")]


def test_a_request_without_a_token_is_anonymous(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        greeted, refused = client.get("/greeting"), client.get("/me")
    assert (greeted.status_code, greeted.json()) == (HTTPStatus.OK, {"hello": "anonymous"})
    assert {record["user"] for record in _of(logged(), greeted.headers["x-request-id"])} == {None}
    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    assert refused.headers["www-authenticate"] == "Bearer"
    assert refused.json() == {
        "type": "sign-in-required",
        "title": "Sign-in is required",
        "status": 401,
        "detail": "positions are shown to a signed-in user",
        "instance": f"urn:uuid:{refused.headers['x-request-id']}",
    }


@pytest.mark.parametrize(
    ("path", "headers", "detail"),
    [
        ("/ping", {"authorization": "Bearer not.a.jwt"}, "the bearer token is not a JWT this API reads: Invalid header padding"),
        ("/nowhere", {"authorization": "Bearer not.a.jwt"}, "the bearer token is not a JWT this API reads: Invalid header padding"),
        (
            "/greeting",
            {"authorization": "Basic anNtaXRoOmh1bnRlcjI="},
            "the Authorization header holds no bearer token: send `Authorization: Bearer TOKEN`, or no Authorization header for an anonymous request",
        ),
        ("/greeting", {"authorization": "Bearer"}, "the Authorization header holds no bearer token: send `Authorization: Bearer TOKEN`, or no Authorization header for an anonymous request"),
        ("/greeting", {"authorization": _signed(desk=None)["authorization"]}, "the token's desk claim is not valid: Input should be a valid string"),
    ],
    ids=["a-route-that-asks-for-no-user", "no-route", "basic-credentials", "an-empty-bearer", "a-declared-claim-not-valid"],
)
def test_every_request_with_a_token_that_does_not_verify_is_refused_and_never_anonymous(path: str, headers: dict[str, str], detail: str, logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get(path, headers=headers)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    request_id = response.headers["x-request-id"]
    assert response.json() == {"type": "invalid-token", "title": "The bearer token is not valid", "status": 401, "detail": detail, "instance": f"urn:uuid:{request_id}"}
    records = _of(logged(), request_id)
    assert [record["message"] for record in records] == [f"refused the bearer token: {detail}", f"GET {path}: 401"]
    assert all("user" not in record for record in records)


def test_a_token_for_another_audience_is_refused() -> None:
    with TestClient(_app(audience="pricing").asgi) as client:
        response = client.get("/greeting", headers=_signed())
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.UNAUTHORIZED, "the token is not for this API, whose audience is pricing")


def test_a_health_check_is_left_alone() -> None:
    with TestClient(_app().asgi) as client:
        assert client.get("/health/ready", headers={"authorization": "Bearer not.a.jwt"}).status_code == HTTPStatus.OK


def test_the_user_goes_into_the_requests_server_span() -> None:
    app = _app()
    spans = InMemorySpanExporter()
    app.provider.add_span_processor(SimpleSpanProcessor(spans))
    with TestClient(app.asgi) as client:
        client.get("/greeting", headers=_signed())
        client.get("/greeting")
    server = [span for span in spans.get_finished_spans() if span.name == "GET /greeting" and span.parent is None]
    assert [(span.attributes or {}).get("enduser.id") for span in server] == ["jsmith", None]


def test_a_service_that_declares_no_user_reads_no_token() -> None:
    service = api.Service(name="prices", settings=api.Settings, routes=[api.get("/ping", ping, deadline_s=5, response=dict[str, str], errors=[])])
    with TestClient(api.build(service, settings=api.Settings.given()).asgi) as client:
        response = client.get("/ping", headers={"authorization": "Bearer not.a.jwt"})
    assert response.status_code == HTTPStatus.OK


class Bare(api.User):
    pass


def unannotated(user: Annotated[Trader, api.given(Trader)]) -> dict[str, str]:
    return {"user": user.subject}


def anyone(user: Annotated[api.User | None, api.given(api.User)]) -> dict[str, str]:
    return {"user": type(user).__qualname__}


def _declared(**changed: object) -> Callable[[], object]:
    declared: dict[str, object] = {
        "name": "positions",
        "settings": api.Settings,
        "user": Trader,
        "routes": [api.get("/greeting", greeting, deadline_s=5, response=dict[str, str], errors=[])],
    } | changed
    return lambda: api.Service(**declared)  # ty: ignore[invalid-argument-type] -- each case declares one thing wrong on purpose


def _expiring() -> object:
    class Expiring(api.User):
        expires: Annotated[int, Field(alias="exp")]

    return Expiring


def _renamed() -> object:
    class Renamed(api.User):
        user_id: Annotated[str, Field(alias="sub")]

    return Renamed


def _unchallenged() -> object:
    class Unauthorized(api.Error):
        status = HTTPStatus.UNAUTHORIZED
        title = "Not signed in"

    return Unauthorized


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (_declared(user=dict), "service positions's user is <class 'dict'>, not a subclass of api.User"),
        (_declared(user=None), "greeting asks for Trader, which service positions does not give: it declares no user=..."),
        (_declared(user=Bare), "greeting asks for Trader, which service positions does not give: its user is Bare"),
        (
            lambda: api.get("/me", unannotated, deadline_s=5, response=dict[str, str], errors=[]),
            r"unannotated's parameter user asks for the user: annotate it Annotated\[Trader \| None, api.given\(Trader\)\]",
        ),
        (_expiring, "Expiring.expires declares the exp claim, which basis verifies itself"),
        (_renamed, "Renamed.user_id declares the sub claim, which basis verifies itself"),
        (_unchallenged, "Unauthorized is a 401, whose response carries a challenge: raise api.SignInRequiredError instead"),
    ],
    ids=["a-user-that-is-not-api-user", "no-user-declared", "another-user-declared", "a-user-that-cannot-be-none", "a-claim-basis-verifies", "the-subject-again", "a-401-without-a-challenge"],
)
def test_a_declaration_that_asks_for_a_user_wrongly_is_refused(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.DeclarationError, match=message):
        declare()


def signed_in(user: Annotated[Trader | None, api.given(Trader)]) -> Trader:
    if user is None:
        msg = "positions are shown to a signed-in user"
        raise api.SignInRequiredError(msg)
    return user


def required(user: Annotated[Trader, api.given(Trader)]) -> Trader:
    return user


def through(trader: Annotated[Trader, Depends(required)]) -> Trader:
    return trader


def desk_of_the_signed_in(user: Annotated[Trader, Depends(signed_in)]) -> dict[str, str]:
    return {"desk": user.desk}


def desk_through_two(user: Annotated[Trader, Depends(through)]) -> dict[str, str]:
    return {"desk": user.desk}


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (desk_of_the_signed_in, "desk_of_the_signed_in's dependency signed_in -> user asks for the user: ask for it in desk_of_the_signed_in's own parameters"),
        (desk_through_two, "desk_through_two's dependency through -> required -> user asks for the user: ask for it in desk_through_two's own parameters"),
    ],
    ids=["a-dependency-that-handles-the-anonymous-case", "a-dependency-of-a-dependency-that-cannot-be-none"],
)
def test_a_dependency_that_asks_for_the_user_is_refused_as_one_that_asks_for_a_resource(handler: Callable[..., dict[str, str]], message: str) -> None:
    """Else the route would read the user without its parameters showing it, past the checks of its service and its
    annotation."""
    with pytest.raises(api.DeclarationError, match=message):
        api.get("/desk", handler, deadline_s=5, response=dict[str, str], errors=[])  # ty: ignore[invalid-argument-type] -- a named function


def _choosing() -> object:
    class Choosing(api.User):
        expires: Annotated[int, Field(validation_alias=AliasChoices("expires", "exp"))]

    return Choosing


def _pathed() -> object:
    class Pathed(api.User):
        expires: Annotated[int, Field(validation_alias=AliasPath("exp"))]

    return Pathed


def _validated() -> object:
    class Validated(api.User):
        expires: Annotated[int, Field(validation_alias="exp")]

    return Validated


def _generated() -> object:
    class Generated(api.User):
        model_config = ConfigDict(alias_generator=to_camel)
        desk_id: str

    return Generated


def _by_name() -> object:
    class ByName(api.User):
        model_config = ConfigDict(validate_by_name=True)

    return ByName


def _populated() -> object:
    class Populated(api.User):
        model_config = ConfigDict(populate_by_name=True)

    return Populated


def _allowing() -> object:
    class Allowing(api.User):
        model_config = ConfigDict(extra="allow")

    return Allowing


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (_choosing, r"Choosing.expires reads its claim through validation_alias=AliasChoices\(choices=\['expires', 'exp'\]\): name the one claim it reads with Field\(alias=...\) alone"),
        (_pathed, r"Pathed.expires reads its claim through validation_alias=AliasPath\(path=\['exp'\]\)"),
        (_validated, r"Validated.expires reads its claim through validation_alias='exp'"),
        (_generated, "Generated sets alias_generator in its model_config: a User reads each claim by its field's name, or a plain Field"),
        (_by_name, "ByName sets validate_by_name in its model_config"),
        (_populated, "Populated sets populate_by_name in its model_config"),
        (_allowing, "Allowing sets extra in its model_config"),
    ],
    ids=["alias-choices", "alias-path", "validation-alias", "alias-generator", "validate-by-name", "populate-by-name", "extra-allow"],
)
def test_a_user_names_each_claim_by_its_field_or_a_plain_alias_alone(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.DeclarationError, match=message):
        declare()


_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def test_a_route_whose_parameters_ask_for_the_user_answers_sign_in_required_without_declaring_it() -> None:
    assert api.SignInRequiredError in api.get("/me", me, deadline_s=5, response=dict[str, object], errors=[]).answers
    assert api.SignInRequiredError in api.get("/anyone", anyone, deadline_s=5, response=dict[str, str], errors=[]).answers  # a base of the user
    assert api.SignInRequiredError not in api.get("/ping", ping, deadline_s=5, response=dict[str, str], errors=[]).answers


def test_sign_in_required_is_in_the_openapi_document_of_each_route_that_asks_for_the_user_alone() -> None:
    document = _JSON.validate_python(_app().asgi.openapi())
    assert isinstance(document, dict)
    paths = document["paths"]
    assert isinstance(paths, dict)

    def responses(path: str) -> dict[str, JsonValue]:
        found = paths[path]
        assert isinstance(found, dict)
        operation = found["get"]
        assert isinstance(operation, dict)
        answered = operation["responses"]
        assert isinstance(answered, dict)
        return answered

    problem = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
    assert responses("/me")["401"] == responses("/greeting")["401"] == {"description": "sign-in-required: Sign-in is required", "content": problem}
    assert "401" not in responses("/ping")


def test_a_route_that_declares_sign_in_required_is_refused_since_basis_adds_it() -> None:
    with pytest.raises(api.DeclarationError, match=r"me declares SignInRequiredError, which basis adds to each route whose parameters ask for the user: remove it from errors=\[\.\.\.\]"):
        api.get("/me", me, deadline_s=5, response=dict[str, object], errors=[api.SignInRequiredError])


def gatekeeper() -> dict[str, str]:
    msg = "positions are shown to a signed-in user"
    raise api.SignInRequiredError(msg)


def test_sign_in_required_from_a_route_that_does_not_ask_for_the_user_is_a_500_whose_log_says_to_ask(logged: Records) -> None:
    service = api.Service(name="positions", settings=api.Settings, user=Trader, routes=[api.get("/gate", gatekeeper, deadline_s=5, response=dict[str, str], errors=[])])
    with TestClient(api.build(service, settings=api.Settings.given(identity=DEVELOPMENT)).asgi) as client:
        assert client.get("/gate").status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    [failure] = [record for record in logged() if "traceback" in record]
    assert failure["error"] == (
        "basis.api.routes.UndeclaredError: gatekeeper raised SignInRequiredError, which its route does not declare:"
        " ask for the user in its parameters, as Annotated[User | None, api.given(User)], which adds it to the route's errors"
    )


def test_a_route_may_ask_for_a_base_of_the_services_user() -> None:
    service = api.Service(name="positions", settings=api.Settings, user=Trader, routes=[api.get("/anyone", anyone, deadline_s=5, response=dict[str, str], errors=[])])
    with TestClient(api.build(service, settings=api.Settings.given(identity=DEVELOPMENT)).asgi) as client:
        assert client.get("/anyone", headers=_signed()).json() == {"user": "Trader"}


def test_a_user_needs_identity_configured_and_configured_identity_needs_a_user() -> None:
    with pytest.raises(api.SettingsError, match="service positions declares a user, and its identity is not configured, so the API cannot start: set BASIS_IDENTITY__ISSUER, "):
        api.build(SERVICE, settings=api.Settings.given())
    service = api.Service(name="prices", settings=api.Settings, routes=[api.get("/ping", ping, deadline_s=5, response=dict[str, str], errors=[])])
    with pytest.raises(api.SettingsError, match="BASIS_IDENTITY__\\* is set, and service prices declares no user, so no token would be verified"):
        api.build(service, settings=api.Settings.given(identity=DEVELOPMENT))


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"BASIS_IDENTITY__KEYS__KIND": "jwks"}, "BASIS_IDENTITY__KEYS__URL: Field required"),
        ({"BASIS_IDENTITY__ALGORITHMS": '["HS256"]'}, "BASIS_IDENTITY__ALGORITHMS__0: Input should be 'RS256'"),
        ({"BASIS_IDENTITY__ALGORITHMS": '["none"]'}, "BASIS_IDENTITY__ALGORITHMS__0: Input should be 'RS256'"),
        ({"BASIS_IDENTITY__ALGORITHMS": "[]"}, "BASIS_IDENTITY__ALGORITHMS: Tuple should have at least 1 item"),
        ({"BASIS_IDENTITY__LEEWAY_S": "3600"}, "BASIS_IDENTITY__LEEWAY_S: Input should be less than or equal to 300"),
    ],
    ids=["jwks-without-a-url", "hmac", "none", "no-algorithm", "a-leeway-of-an-hour"],
)
def test_identity_settings_name_the_variable_to_fix(monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], message: str) -> None:
    for name, value in ({"BASIS_IDENTITY__ISSUER": ISSUER, "BASIS_IDENTITY__AUDIENCE": AUDIENCE, "BASIS_IDENTITY__ALGORITHMS": '["RS256"]', "BASIS_IDENTITY__LEEWAY_S": "30"} | variables).items():
        monkeypatch.setenv(name, value)
    if "BASIS_IDENTITY__KEYS__KIND" not in variables:
        monkeypatch.setenv("BASIS_IDENTITY__KEYS__KIND", "public_key")
        monkeypatch.setenv("BASIS_IDENTITY__KEYS__PATH", __file__)
    with pytest.raises(api.SettingsError, match=message.replace("[", r"\[")):
        read(api.Settings)


# The development key, which anyone can sign with: refused by settings read from the environment, but on a desktop.


def _machine(home: Path, *, declared: str | None = "desktop = true\n", environ: dict[str, str] | None = None, container: bool = False) -> Machine:
    declaration = home / "basis" / "desktop.toml"
    if declared is not None:
        declaration.parent.mkdir(parents=True, exist_ok=True)
        declaration.write_text(declared, encoding="utf-8")
    marker = home / ".dockerenv"
    if container:
        marker.write_text("", encoding="utf-8")
    return Machine(environ or {}, declaration, (marker,))


@pytest.mark.parametrize(
    ("machine", "refusal"),
    [
        (_machine, None),
        (lambda home: _machine(home, declared="# desktop = true\ndesktop = true\nowner = 'jsmith'\n"), None),
        (lambda home: _machine(home, declared=None), "{home}/basis/desktop.toml does not exist"),
        (lambda home: _machine(home, declared="desktop = false\n"), "{home}/basis/desktop.toml does not say `desktop = true`"),
        (lambda home: _machine(home, declared='desktop = "yes"\n'), "{home}/basis/desktop.toml does not say `desktop = true`"),
        (lambda home: _machine(home, declared="desktop: true\n"), "{home}/basis/desktop.toml cannot be read as TOML (TOMLDecodeError)"),
        (lambda home: _machine(home, environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"}), "it runs in a Kubernetes pod, as KUBERNETES_SERVICE_HOST says"),
        (lambda home: _machine(home, container=True), "it runs in a container, as {home}/.dockerenv says"),
        (lambda _: Machine({}, None, ()), "its user has no configuration folder, in which a desktop is declared"),
    ],
    ids=["declared", "declared-with-more", "undeclared", "declared-false", "declared-not-a-boolean", "not-toml", "a-kubernetes-pod", "a-container", "no-configuration-folder"],
)
def test_the_development_key_is_refused_but_on_a_declared_desktop(tmp_path: Path, machine: Callable[[Path], Machine], refusal: str | None) -> None:
    identity = api.Settings.given(identity=DEVELOPMENT).identity
    if refusal is None:
        refuse_development(identity, machine(tmp_path))
        return
    with pytest.raises(api.SettingsError) as raised:
        refuse_development(identity, machine(tmp_path))
    assert str(raised.value).startswith(
        "BASIS_IDENTITY__KEYS__KIND is development, whose key anyone can sign tokens with, so only a developer's desktop may verify with it, and this machine is not one: "
        + refusal.format(home=tmp_path)
    )
    assert "anywhere else, verify with the sign-in system's keys, setting BASIS_IDENTITY__KEYS__KIND to jwks or public_key" in str(raised.value)


def test_the_sign_in_systems_keys_are_never_refused(tmp_path: Path) -> None:
    identity = api.Settings.given(identity=DEVELOPMENT | {"algorithms": ["RS256"], "keys": {"kind": "jwks", "url": "https://signin.internal.example/jwks.json"}}).identity
    refuse_development(identity, _machine(tmp_path, declared=None, environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, container=True))


def _development_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {"ISSUER": ISSUER, "AUDIENCE": AUDIENCE, "ALGORITHMS": '["ES256"]', "LEEWAY_S": "30", "KEYS__KIND": "development"}.items():
        monkeypatch.setenv(f"BASIS_IDENTITY__{name}", value)


@pytest.mark.parametrize(
    "build",
    [lambda: api.build(SERVICE), lambda: api.build(SERVICE, settings=api.Settings()), lambda: api.build(SERVICE, settings=api.Settings.model_validate({}))],
    ids=["settings-read-by-build", "settings-made-from-the-environment", "settings-validated-with-the-environment"],
)
def test_every_app_refuses_the_development_key_in_a_pod_whatever_made_its_settings(monkeypatch: pytest.MonkeyPatch, build: Callable[[], api.App]) -> None:
    """The bypass review found: settings made with `Settings()`, which read the environment as `read` does, but skip it."""
    _development_in_the_environment(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with pytest.raises(api.SettingsError, match=r"BASIS_IDENTITY__KEYS__KIND is development, .* and this machine is not one: it runs in a Kubernetes pod"):
        build()


def test_settings_from_the_environment_take_the_development_key_on_a_declared_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _development_in_the_environment(monkeypatch)
    with pytest.raises(api.SettingsError, match=r"this machine is not one: .*desktop.toml does not exist"):
        tokens.plan(SERVICE, api.Settings(), machine=_machine(tmp_path / "undeclared", declared=None))
    assert tokens.plan(SERVICE, api.Settings(), machine=_machine(tmp_path)) is not None


def test_only_given_settings_are_a_tests_and_no_variable_makes_them_so(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BASIS_FROM_ARGUMENTS", "BASIS__GIVEN", "BASIS_GIVEN"):
        monkeypatch.setenv(name, "true")
    assert [api.Settings.given().from_arguments, api.Settings().from_arguments, api.Settings.given().model_copy().from_arguments] == [True, False, True]


def test_settings_given_in_a_test_take_the_development_key_anywhere() -> None:
    """A test builds its app from settings it gives, which no deployment can: CI signs and verifies as a desktop does."""
    with TestClient(_app().asgi) as client:
        assert client.get("/greeting", headers=_signed()).json() == {"hello": "jsmith"}


def test_a_desktop_is_declared_in_the_users_own_configuration_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    found = Machine.here()  # on Linux, as basis's own tests run; on Windows the folder is %APPDATA%
    assert found.declaration == tmp_path / "basis" / "desktop.toml"
    assert found.containers == (Path("/.dockerenv"), Path("/run/.containerenv"))


def test_a_401_declares_its_own_challenge() -> None:
    class Expired(api.Error):
        status = HTTPStatus.UNAUTHORIZED
        title = "The session expired"
        headers = MappingProxyType({"WWW-Authenticate": 'Bearer error="invalid_token"'})

    assert Expired.type == "expired"
