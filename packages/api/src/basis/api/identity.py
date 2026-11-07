"""Who makes a request: the user its bearer token names, which basis verifies before any handler runs.

A UI sends the user's JWT as `Authorization: Bearer TOKEN`. A service that declares a user, `api.Service(...,
user=User)`, has every request's token verified (its signature against the sign-in system's keys, its expiry, its
issuer and its audience) whichever route it asks for, by `basis.api.tokens`. A request without an `Authorization`
header is anonymous. A token that does not verify, or a header that holds no bearer token, is refused with a 401
problem, `invalid-token`, and never treated as anonymous, so a broken sign-in in a UI fails loudly.

A handler asks for the user as it asks for its settings, and gets None for an anonymous request, so the type checker
makes it handle that case:

    class User(api.User):
        desk: str
        roles: tuple[str, ...] = ()


    async def positions(user: Annotated[User | None, api.given(User)]) -> Positions:
        if user is None:
            raise api.SignInRequiredError("positions are shown to a signed-in user")

`api.User` holds the token's subject, `sub`: the sign-in system's id for the user. A service declares each other claim
it needs as a field of its own subclass, named as the claim, or with `Field(alias=...)` where the claim's name is no
identifier; a token without a required one, or with one that does not validate, is refused. No other way of naming a
claim is allowed (`validation_alias`, `AliasChoices`, `AliasPath`, an alias generator, population by name), so that the
claims a user reads are plain to see, and checked. The claims that basis
verifies itself (`iss`, `aud`, `exp`, `nbf`, `iat`) are not a service's to declare, and the user holds nothing else
of the token, the token itself included.

The subject goes into every log record of the request, as `user` (null when the request is anonymous), and into its
server span as `enduser.id`.

How tokens are verified is the settings' alone, `BASIS_IDENTITY__*` (`Identity`): the issuer, the audience, the
algorithms accepted, the leeway for clock skew, and the keys, each set explicitly, and none guessed:

    BASIS_IDENTITY__ISSUER=https://signin.internal.example
    BASIS_IDENTITY__AUDIENCE=accruals
    BASIS_IDENTITY__ALGORITHMS=["RS256"]
    BASIS_IDENTITY__LEEWAY_S=30
    BASIS_IDENTITY__KEYS__KIND=jwks
    BASIS_IDENTITY__KEYS__URL=https://signin.internal.example/.well-known/jwks.json

The keys are the sign-in system's JWK set, from its URL (`Jwks`); one public key, from a PEM file (`PublicKey`); or
the development key (`Development`), which anyone can sign with, so that every app refuses it on any machine that is
not declared a developer's desktop (`Machine`), unless its settings are a test's own, from `Settings.given`."""

import os
import platform
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from types import MappingProxyType, NoneType, UnionType
from typing import Annotated, Literal, Union, get_args, get_origin, override

from pydantic import BaseModel, ConfigDict, Field, FilePath, HttpUrl, field_validator
from starlette.requests import Request

from basis.api.errors import Error, ServiceError
from basis.core.model import Model
from basis.core.settings import SettingsError, variable

USER = "basis.user"
"""The scope key that holds the request's `User`, or None for an anonymous request, in a service that declares a
user. It is absent from a request whose token was refused, and from every request of a service that declares none."""
VERIFIED = frozenset({"iss", "aud", "exp", "nbf", "iat", "sub"})
"""The claims basis verifies itself, which a service's `User` does not declare."""

type Algorithm = Literal["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
"""An algorithm a token may be signed with, each one verified with a public key. HMAC's (`HS256` and the others),
whose key signs as well as verifies, and `none`, which signs nothing, are never accepted."""


class User(BaseModel):
    """The user a request's bearer token names, from its claims. A service subclasses it with a field for each other
    claim it needs."""

    model_config = ConfigDict(extra="ignore", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False, use_attribute_docstrings=True)
    """A token's claims the service declares nothing for are ignored: the sign-in system may add claims at any time."""

    subject: Annotated[str, Field(alias="sub", min_length=1)]
    """`sub`: the sign-in system's id for the user, which logs and traces carry."""

    @classmethod
    @override
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        config = cls.model_config
        changed = [key for key, value in _CONFIG.items() if config.get(key, value) != value]
        if changed:
            msg = f"{cls.__qualname__} sets {', '.join(changed)} in its model_config: a User reads each claim by its field's name, or a plain Field(alias=...), and ignores the rest, so remove it"
            raise ServiceError(msg)
        for name, found in cls.model_fields.items():
            if found.validation_alias is not None and found.validation_alias != found.alias:
                msg = f"{cls.__qualname__}.{name} reads its claim through validation_alias={found.validation_alias!r}: name the one claim it reads with Field(alias=...) alone"
                raise ServiceError(msg)
            claim = found.alias if found.alias is not None else name
            if claim in VERIFIED and name != "subject":
                msg = f"{cls.__qualname__}.{name} declares the {claim} claim, which basis verifies itself: declare only the other claims the service needs, and read the subject as user.subject"
                raise ServiceError(msg)


_CONFIG: Mapping[str, object] = MappingProxyType({"extra": "ignore", "alias_generator": None, "validate_by_name": False, "populate_by_name": False, "validate_by_alias": True})
"""The configuration a `User` keeps, which a subclass may not change: each claim it reads is then the name or the plain
alias of one of its fields, which basis checks against the claims it verifies itself."""


class InvalidTokenError(Error):
    status = HTTPStatus.UNAUTHORIZED
    title = "The bearer token is not valid"
    headers = MappingProxyType({"WWW-Authenticate": 'Bearer error="invalid_token"'})


class SignInRequiredError(Error):
    """For a handler to raise when its user is None and its route needs one. basis adds it to the errors of each route
    whose parameters ask for the user, so a route never declares it."""

    status = HTTPStatus.UNAUTHORIZED
    title = "Sign-in is required"
    headers = MappingProxyType({"WWW-Authenticate": "Bearer"})


class KeysUnavailableError(Error):
    """The sign-in system's JWK set cannot be fetched to look up a token's key: the token may well be good, so this is
    no 401."""

    status = HTTPStatus.SERVICE_UNAVAILABLE
    title = "The sign-in system's keys are unavailable"


class Jwks(Model):
    """The sign-in system's JWK set (RFC 7517), fetched from its URL when the app starts, before it reports ready;
    again when a token names a key the set does not hold, at most once every `refresh_s`; and when the set is older
    than `max_age_s`, so that a key the sign-in system withdraws stops verifying. It is reached through these settings
    alone, never `SSL_CERT_FILE`, `HTTPS_PROXY` or the like."""

    kind: Literal["jwks"]
    url: HttpUrl
    """Over https, since keys fetched over http could be anyone's, such as
    `https://signin.internal.example/.well-known/jwks.json`."""
    ca_bundle: FilePath | None = None
    """A PEM file of the certificate authorities the sign-in system's certificate must chain to, in place of the
    operating system's trusted ones."""
    proxy: HttpUrl | None = None
    deadline_s: Annotated[float, Field(gt=0)] = 5
    """Seconds each fetch may take."""
    max_age_s: Annotated[float, Field(gt=0)] = 600
    """Seconds a fetched set is used before a request starts fetching it again, in the background, without waiting for
    it. A set that cannot be fetched then is kept, with a warning, until a fetch succeeds."""
    refresh_s: Annotated[float, Field(ge=0)] = 30
    """The fewest seconds between two fetches made for a key the set does not hold, so that tokens naming made-up keys
    cannot make the API hammer the sign-in system."""

    @field_validator("url")
    @classmethod
    def _https(cls, url: HttpUrl) -> HttpUrl:
        if url.scheme != "https":
            msg = "the key set is fetched over https only, since keys fetched over http could be anyone's"
            raise ValueError(msg)
        return url


class PublicKey(Model):
    """One public key, which every accepted algorithm must fit, from a PEM file: a `PUBLIC KEY` block, or a
    `CERTIFICATE` whose key it is, whose dates and chain are not checked, since the file is trusted as the settings
    name it. A token's `kid` is not read. A new key needs the API redeployed; a JWK set rotates without that."""

    kind: Literal["public_key"]
    path: FilePath


class Development(Model):
    """The development key, an ES256 key that basis publishes (`basis.api.tokens.DEVELOPMENT_KEY`), so that tests,
    `basis dev` and a UI on the same desktop can sign tokens with it (`basis.api.tokens.sign`). Anyone can, so every app
    refuses it on any machine that is not declared a developer's desktop (`Machine`), unless its settings are a test's
    own, from `Settings.given`."""

    kind: Literal["development"]


class Identity(Model):
    issuer: Annotated[str, Field(min_length=1)]
    """The `iss` every token carries, exactly: the sign-in system's name for itself, such as
    `https://signin.internal.example`."""
    audience: Annotated[str, Field(min_length=1)]
    """The `aud` a token carries, or holds among its audiences: this API's name at the sign-in system, so that a token
    issued for another API is refused here."""
    algorithms: Annotated[tuple[Algorithm, ...], Field(min_length=1)]
    """The algorithms a token may be signed with, such as `["RS256"]`, each of which must fit the keys; a token signed
    with any other is refused."""
    leeway_s: Annotated[float, Field(ge=0, le=300)]
    """Seconds by which this API's clock and the sign-in system's may differ, allowed when checking `exp`, `nbf` and
    `iat`."""
    keys: Annotated[Jwks | PublicKey | Development, Field(discriminator="kind")]


@dataclass(frozen=True)
class Machine:
    """What the development key's check reads of the machine the API runs on, given so that a test can be any
    machine.

    A developer declares a desktop once, by writing `desktop = true` to `basis/desktop.toml` in their own
    configuration folder: `%APPDATA%` on Windows, and `$XDG_CONFIG_HOME`, or `~/.config`, in WSL and on Linux, the
    folders uv reads its own `uv.toml` from. The file is never in a repo, so an image built from one never holds it,
    and a deployment's settings, which are variables, cannot declare it. A machine that shows it runs in a container,
    in Kubernetes or under Docker or Podman, is refused even where the file exists."""

    environ: Mapping[str, str]
    declaration: Path | None
    """Where a desktop is declared, or None where the user has no configuration folder."""
    containers: tuple[Path, ...]
    """Files that exist only in a container."""

    @classmethod
    def here(cls) -> Machine:
        environ = dict(os.environ)
        folder: Path | None
        if platform.system() == "Windows":
            folder = Path(appdata) if (appdata := environ.get("APPDATA", "")) != "" else None
        elif (xdg := environ.get("XDG_CONFIG_HOME", "")) != "":
            folder = Path(xdg)
        else:
            try:
                folder = Path.home() / ".config"
            except RuntimeError:  # no HOME, and no entry for the user in the password database, as in some containers
                folder = None
        return cls(environ, folder / "basis" / "desktop.toml" if folder is not None else None, (Path("/.dockerenv"), Path("/run/.containerenv")))


def refuse_development(identity: Identity | None, machine: Machine) -> None:
    """Fail when `identity` verifies tokens with the development key, which anyone can sign with, and `machine` is not
    declared a developer's desktop."""
    if identity is None or not isinstance(identity.keys, Development) or (reason := _not_a_desktop(machine)) is None:
        return
    declare = f"on your own desktop, write `desktop = true` to {machine.declaration}" if machine.declaration is not None else "on your own desktop, declare it with basis/desktop.toml"
    kind = variable(("identity", "keys", "kind"))
    msg = (
        f"{kind} is development, whose key anyone can sign tokens with, so only a developer's desktop may verify with it, and this machine is not one: {reason}:"
        f" {declare}; anywhere else, verify with the sign-in system's keys, setting {kind} to jwks or public_key"
    )
    raise SettingsError(msg)


def _not_a_desktop(machine: Machine) -> str | None:
    """Why `machine` is not a developer's desktop, or None when it is one."""
    if "KUBERNETES_SERVICE_HOST" in machine.environ:
        return "it runs in a Kubernetes pod, as KUBERNETES_SERVICE_HOST says"
    if (marker := next((found for found in machine.containers if found.exists()), None)) is not None:
        return f"it runs in a container, as {marker} says"
    if machine.declaration is None:
        return "its user has no configuration folder, in which a desktop is declared"
    return _undeclared(machine.declaration)


def _undeclared(declaration: Path) -> str | None:
    """Why the file at `declaration` does not declare a desktop, or None when it does."""
    try:
        declared = tomllib.loads(declaration.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{declaration} does not exist"
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return f"{declaration} cannot be read as TOML ({type(error).__name__})"
    return None if declared.get("desktop") is True else f"{declaration} does not say `desktop = true`"


@dataclass(frozen=True)
class GivenUser:
    """The dependency `api.given(User)` is: the request's user, which `basis.api.tokens.Bearer` verified."""

    kind: type[User]

    async def __call__(self, request: Request) -> User | None:
        if USER not in request.scope:
            msg = "the request has no user, since its service declares none: declare user=... in its api.Service"
            raise RuntimeError(msg)
        found: object = request.scope[USER]
        if found is None or isinstance(found, self.kind):
            return found
        msg = f"the request's user is a {type(found).__qualname__}, not a {self.kind.__qualname__}"
        raise TypeError(msg)


def annotation_refusal(kind: type[User], annotation: object) -> str | None:
    """How to annotate a parameter that asks for the user, unless `annotation` is already `kind | None`."""
    if get_origin(annotation) in {Union, UnionType} and set(get_args(annotation)) == {kind, NoneType}:
        return None
    name = kind.__qualname__
    return f"annotate it Annotated[{name} | None, api.given({name})], since a request without a token is anonymous, which the handler must handle"
