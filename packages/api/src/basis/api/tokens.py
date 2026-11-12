"""Verifying each request's bearer token, for a service that declares a user (see `identity.py`), with PyJWT.

`plan` checks, when the app is built, that the service's declaration and its settings agree: a service that declares a
user has its identity configured, and configured identity has a service that declares a user, since otherwise no token
would be verified. It reads a public key, which must fit every algorithm the settings accept, as must the development
key, which it refuses off a declared desktop unless the settings are a test's own. Every app is planned here, whatever
made its settings. The app's lifespan opens the `Verifier` before its resources, once it has started and its spans have
somewhere to go, fetching the JWK set before the app reports ready, so a wrong URL stops the app before it serves.

`Bearer`, the middleware inside the request boundary (`requests.Requests`), verifies the token of every request but a
health check, so that a refusal is a problem with the request's id, and every record logged while the request is
served carries its user. Verifying checks, refusing the token at the first check that fails:

- that it is a JWS in compact form, whose header names one of the algorithms the settings accept: never `none` and
  never HMAC. A key the token carries itself (`jwk`, `jku`, `x5u`, `x5c`) is never read, and a critical extension
  PyJWT does not know is refused;
- that its key is the issuer's: the one public key, or the key its `kid` names in the JWK set, which is fetched again
  when it does not hold that key, at most once every `refresh_s`. The key must fit the algorithm, by its type and its
  curve, and so must the `alg` a JWK names, so that no key verifies under an algorithm its owner did not mean it for
  (algorithm confusion); a key shorter than PyJWT's minimum, such as an RSA key under 2048 bits, verifies nothing;
- its signature;
- its `iss`, exactly the settings' issuer, and its `aud`, the settings' audience or a list holding it;
- its `exp`, which it must have, and its `nbf` and `iat` where it has them, each against this API's clock, give or
  take `leeway_s`: basis checks these itself, as PyJWT reads the system's clock and no other;
- its `sub`, and every claim the service's `User` declares.

The JWK set is fetched with the settings' CA bundle and proxy, and nothing from the environment, within its deadline
and with a span of its own. A token whose key the set holds never waits for a fetch: a stale set is fetched again in
the background, and kept, with a warning, when that fails. Only a token naming a key the set does not hold waits, for
one fetch, within its deadline; and when the set cannot be fetched, it is a 503, `keys-unavailable`, as the token may
well be good.

`sign` makes tokens with the development key, for tests and `basis dev`."""

import asyncio
import base64
import logging
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, assert_never
from uuid import UUID

import httpx2
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from jwt.algorithms import get_default_algorithms
from jwt.types import Options
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Tracer
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from basis.api import errors
from basis.api.identity import EXPIRES, USER, Algorithm, Development, Identity, InvalidTokenError, Jwks, KeysUnavailableError, Machine, PublicKey, User, refuse_development
from basis.api.requests import HEALTH, REQUEST_ID
from basis.api.service import DeclarationError, Service
from basis.api.settings import Settings
from basis.core import logs
from basis.core.settings import SettingsError, variable

log = logging.getLogger(__name__)

type VerifyingKey = rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey | ed448.Ed448PublicKey

DEVELOPMENT_KEY: Mapping[str, str] = MappingProxyType(
    {
        "kty": "EC",
        "crv": "P-256",
        "kid": "basis-development",
        "alg": "ES256",
        "x": "Xgv3fh0kPSZcEDr60PmlPmZ8V5ytxOae1vtlhXqnhMI",
        "y": "Hgxef-hOuDO-ouolTTf9PuIj_FWSZy0BWDmChTcI9EM",
        "d": "fKf0qhP6MC3WvrtHFG6hPj1_PuqLmI-xx_mfRylqjmQ",
    }
)
"""The development key, as a JWK with its private part, published so that anything can sign tokens with it: tests,
`basis dev`, and a UI's development mode on the same desktop. It proves nothing about who signed, so settings read from
the environment refuse to verify with it on any machine not declared a developer's desktop."""

_OPTIONS: Options = {
    "require": ["exp", "iss", "aud", "sub"],
    "verify_exp": False,  # basis checks the times against its own clock, below
    "verify_nbf": False,
    "verify_iat": False,
    "enforce_minimum_key_length": True,
}
_CURVES: Mapping[Algorithm, str] = MappingProxyType({"ES256": "secp256r1", "ES384": "secp384r1", "ES512": "secp521r1"})


_SIGNING = ec.derive_private_key(int.from_bytes(base64.urlsafe_b64decode(DEVELOPMENT_KEY["d"] + "=")), ec.SECP256R1())
"""The development key's private part, from its JWK's `d`, from which its public part, `x` and `y`, derives."""
_DEVELOPMENT = _SIGNING.public_key()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Keys(Protocol):
    """Where a token's key comes from: one key, or the sign-in system's JWK set."""

    async def open(self, tracer: Tracer) -> None: ...

    async def key(self, kid: str | None, algorithm: Algorithm) -> VerifyingKey: ...

    async def close(self) -> None: ...


class Verifier:
    """Verifies a request's bearer token, and makes its `User`."""

    def __init__(self, identity: Identity, user: type[User], keys: Keys, *, now: Callable[[], datetime]) -> None:
        self._identity = identity
        self._user = user
        self._keys = keys
        self._now = now

    async def open(self, tracer: Tracer) -> None:
        await self._keys.open(tracer)

    async def close(self) -> None:
        await self._keys.close()

    async def verify(self, token: str) -> User:
        """The user `token` names, or `InvalidTokenError`, with what failed; or `KeysUnavailableError` when the
        sign-in system's keys, which would say, cannot be fetched."""
        return (await self.verified(token))[0]

    async def verified(self, token: str) -> tuple[User, float | None]:
        """The user `token` names, as `verify` gives it, and when the token expires, in seconds since 1970, or None for
        a token with no `exp`."""
        identity = self._identity
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            msg = f"the bearer token is not a JWT this API reads: {error}"
            raise InvalidTokenError(msg) from error
        named = header.get("alg")
        algorithm = next((accepted for accepted in identity.algorithms if accepted == named), None)
        if algorithm is None:
            msg = f"the token is signed with {named!r}, and this API accepts {', '.join(identity.algorithms)}"
            raise InvalidTokenError(msg)
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            msg = "the token's key id, kid, is not a string"
            raise InvalidTokenError(msg)
        key = await self._keys.key(kid, algorithm)
        try:
            claims: object = jwt.decode(token, key, algorithms=[algorithm], audience=identity.audience, issuer=identity.issuer, options=_OPTIONS)
        except jwt.InvalidSignatureError as error:
            msg = "the token's signature does not verify"
            raise InvalidTokenError(msg) from error
        except jwt.MissingRequiredClaimError as error:
            msg = f"the token has no {error.claim} claim"
            raise InvalidTokenError(msg) from error
        except jwt.InvalidIssuerError as error:
            msg = "the token is not from this API's issuer"
            raise _withheld(InvalidTokenError(msg), f"this API's issuer is {identity.issuer}") from error
        except jwt.InvalidAudienceError as error:
            msg = "the token is not for this API"
            raise _withheld(InvalidTokenError(msg), f"this API's audience is {identity.audience}") from error
        except jwt.InvalidKeyError as error:
            log.warning("the issuer's key does not verify %s: %s", algorithm, error, extra={"kid": kid, "algorithm": algorithm})
            msg = f"the issuer's key does not verify {algorithm}: {error}"
            raise InvalidTokenError(msg) from error
        except jwt.PyJWTError as error:
            msg = f"the token is not valid: {error}"
            raise InvalidTokenError(msg) from error
        if not isinstance(claims, dict):
            msg = "the token's claims are not a JSON object"
            raise InvalidTokenError(msg)
        _check_times(claims, self._now().timestamp(), identity.leeway_s)
        try:
            return self._user.model_validate(claims), _numeric_date(claims, "exp")
        except ValidationError as error:
            raise InvalidTokenError(_unclaimed(error)) from error


def _check_times(claims: Mapping[str, object], now: float, leeway_s: float) -> None:
    expires = _numeric_date(claims, "exp")
    if expires is not None and expires <= now - leeway_s:
        msg = f"the token expired at {_iso(expires)}"
        raise InvalidTokenError(msg)
    if (starts := _numeric_date(claims, "nbf")) is not None and starts > now + leeway_s:
        msg = f"the token is not valid until {_iso(starts)}"
        raise InvalidTokenError(msg)
    if (issued := _numeric_date(claims, "iat")) is not None and issued > now + leeway_s:
        msg = f"the token is issued in the future, at {_iso(issued)}"
        raise InvalidTokenError(msg)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LATEST = int((datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC) - _EPOCH).total_seconds())
"""The last second a `datetime` holds, past which a time in a token is no time."""


def _numeric_date(claims: Mapping[str, object], claim: str) -> float | None:
    """A claim that is a time, as RFC 7519's NumericDate, in seconds since 1970 and before the year 10000; None where
    the token has none. The bounds are compared before anything converts it, since JSON's integers have no limit and a
    float has one; NaN is in no bounds."""
    if (value := claims.get(claim)) is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= _LATEST:
        msg = f"the token's {claim} claim is not a time, in seconds since 1970"
        raise InvalidTokenError(msg)
    return float(value)


def _iso(seconds: float) -> str:
    return (_EPOCH + timedelta(seconds=seconds)).isoformat()


def _unclaimed(error: ValidationError) -> str:
    """Why a token's claims are not the service's user, as the first claim at fault."""
    first = error.errors(include_url=False, include_input=False)[0]
    claim = ".".join(str(part) for part in first["loc"])
    if first["type"] == "missing":
        return f"the token has no {claim} claim, which this API needs"
    return f"the token's {claim} claim is not valid: {first['msg']}"


class FixedKey:
    """One key: the settings' public key, or the development key."""

    def __init__(self, key: VerifyingKey) -> None:
        self._key = key

    async def open(self, tracer: Tracer) -> None:
        pass

    async def key(self, kid: str | None, algorithm: Algorithm) -> VerifyingKey:  # noqa: ARG002 -- one key, whatever the token names; `plan` checked it fits
        return self._key

    async def close(self) -> None:
        pass


class _JwkSet(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    keys: tuple[dict[str, JsonValue], ...]


def _withheld[E: InvalidTokenError | KeysUnavailableError](refused: E, why: str) -> E:
    """`refused`, whose problem a caller who has not signed in sees, with `why`, what it does not say of this API's
    sign-in, for its log line alone (`Bearer`)."""
    refused.add_note(why)
    return refused


def _unavailable(failure: KeysUnavailableError) -> KeysUnavailableError:
    """What a request whose token's key cannot be looked up answers: the 503 a UI retries, naming no URL."""
    return _withheld(KeysUnavailableError("the sign-in system's keys cannot be fetched now, to look up the token's key: make the request again soon"), failure.detail)


class KeySet:
    """The sign-in system's JWK set, fetched when the app opens, and fetched again when a token names a key the set
    does not hold, by one request while the others wait for it, or in the background when the set is stale."""

    def __init__(self, settings: Jwks, *, monotonic: Callable[[], float]) -> None:
        self._settings = settings
        self._monotonic = monotonic
        self._client: httpx2.AsyncClient | None = None
        self._tracer: Tracer | None = None
        self._keys: tuple[dict[str, JsonValue], ...] = ()
        self._made: dict[tuple[str | None, Algorithm], VerifyingKey] = {}
        """Each key made from the set, by its id and the algorithm it verifies, which the next fetch clears."""
        self._fetched_at: float | None = None
        """When the set held now was fetched."""
        self._tried_at: float | None = None
        """When the last fetch began, whether or not it succeeded."""
        self._failure: KeysUnavailableError | None = None
        """Why the last fetch failed, or None when it succeeded."""
        self._fetching: asyncio.Task[None] | None = None

    async def open(self, tracer: Tracer) -> None:
        settings = self._settings
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle is not None else True
        self._client = httpx2.AsyncClient(verify=verify, proxy=str(settings.proxy) if settings.proxy is not None else None, trust_env=False)
        self._tracer = tracer
        await self._refresh()

    @property
    def closed(self) -> bool:
        """Whether its client is closed, or was never made."""
        return self._client is None or self._client.is_closed

    async def close(self) -> None:
        if self._fetching is not None:
            self._fetching.cancel()
        if self._client is not None:
            await self._client.aclose()

    async def key(self, kid: str | None, algorithm: Algorithm) -> VerifyingKey:
        """The key `kid` names that verifies `algorithm`. A token whose key the set holds never waits for a fetch: a
        stale set is fetched again in the background. Only a key the set does not hold waits for one, within the
        fetch's deadline, and at most once every `refresh_s`."""
        stale = self._fetched_at is None or self._monotonic() - self._fetched_at >= self._settings.max_age_s
        if stale and self._may_fetch():
            self._fetch_soon()
        if (found := self._find(kid, algorithm)) is not None:
            return found
        if self._may_fetch():
            try:
                await self._refresh()  # a key the set did not hold, perhaps one the sign-in system has just added
            except KeysUnavailableError as error:
                raise _unavailable(error) from error
            if (found := self._find(kid, algorithm)) is not None:
                return found
        elif self._failure is not None:  # the set may lack a key the sign-in system has added since it was fetched
            raise _unavailable(self._failure)
        named = f"key {kid}" if kid is not None else "key, as it names none"
        msg = f"the sign-in system's JWK set holds no {named} that verifies {algorithm}"
        raise InvalidTokenError(msg)

    def _may_fetch(self) -> bool:
        """Whether a fetch is under way, which the caller waits for, or one may begin."""
        return self._fetching is not None or self._tried_at is None or self._monotonic() - self._tried_at >= self._settings.refresh_s

    async def _refresh(self) -> None:
        """Fetch the set, or wait for the fetch already under way; a request cancelled while it waits leaves the fetch
        to the others."""
        await asyncio.shield(self._fetch_soon())

    def _fetch_soon(self) -> asyncio.Task[None]:
        """The fetch under way, or a new one, which goes on without anyone waiting for it."""
        if self._fetching is None:
            self._tried_at = self._monotonic()
            self._fetching = asyncio.create_task(self._fetch())
            self._fetching.add_done_callback(self._fetched)
        return self._fetching

    def _fetched(self, task: asyncio.Task[None]) -> None:
        self._fetching = None
        if not task.cancelled():
            task.exception()  # each waiter raises it; this marks it retrieved when every waiter was cancelled

    async def _fetch(self) -> None:
        try:
            await self._get()
        except KeysUnavailableError as error:
            self._failure = error
            if self._fetched_at is not None:
                log.warning("the sign-in system's JWK set could not be fetched again, and the one fetched before is kept: %s", error.detail, extra={"url": str(self._settings.url)})
            raise
        self._failure = None

    async def _get(self) -> None:
        settings, client, tracer = self._settings, self._client, self._tracer
        if client is None or tracer is None:
            msg = "the JWK set is fetched before it was opened"
            raise RuntimeError(msg)
        url = str(settings.url)
        attributes = {"http.request.method": "GET", "url.full": url, "basis.deadline_s": settings.deadline_s}
        with tracer.start_as_current_span("identity keys GET", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                async with asyncio.timeout(settings.deadline_s):
                    response = await client.get(url, timeout=settings.deadline_s)
            except (TimeoutError, httpx2.TimeoutException) as error:
                msg = f"{url} did not answer within {settings.deadline_s:g}s"
                raise KeysUnavailableError(msg) from error
            except httpx2.TransportError as error:
                msg = f"{url} could not be reached: {type(error).__name__}"
                raise KeysUnavailableError(msg) from error
            span.set_attribute("http.response.status_code", response.status_code)
            if not response.is_success:
                msg = f"{url} answered {response.status_code}"
                raise KeysUnavailableError(msg)
            try:
                found = _JwkSet.model_validate_json(response.content)
            except ValidationError as error:
                msg = f"{url} answered with a body that is not a JWK set"
                raise KeysUnavailableError(msg) from error
            span.set_attribute("basis.identity.keys", len(found.keys))
        self._keys = tuple(jwk for jwk in found.keys if jwk.get("use", "sig") == "sig")
        self._made = {}
        self._fetched_at = self._monotonic()
        log.info("fetched the sign-in system's JWK set: %s signing keys", len(self._keys), extra={"url": url, "kids": [jwk.get("kid") for jwk in self._keys]})

    def _find(self, kid: str | None, algorithm: Algorithm) -> VerifyingKey | None:
        """The key that verifies `algorithm` among the set's keys that `kid` names, each considered for its algorithm
        and its type, or among all of them where the token names none, which must then be one; None where the set
        holds no key `kid` names."""
        if (made := self._made.get((kid, algorithm))) is not None:
            return made
        held = [jwk for jwk in self._keys if kid is None or jwk.get("kid") == kid]
        if not held:
            return None
        considered = [_fit(jwk, algorithm) for jwk in held]
        fitting = [found for found in considered if not isinstance(found, str)]
        if kid is None and len(fitting) > 1:
            msg = f"the token names no key id, kid, and {len(fitting)} of the sign-in system's keys verify {algorithm}"
            raise InvalidTokenError(msg)
        if not fitting:
            unfit = [found for found in considered if isinstance(found, str)]
            named = f"keys {kid}" if kid is not None else "keys"
            msg = unfit[0] if len(unfit) == 1 else f"none of the sign-in system's {len(unfit)} {named} verifies {algorithm}"
            raise InvalidTokenError(msg)
        self._made[kid, algorithm] = fitting[0]
        return fitting[0]


def _fit(jwk: Mapping[str, JsonValue], algorithm: Algorithm) -> VerifyingKey | str:
    """The public key `jwk` holds, where it verifies `algorithm`, by the algorithm it names and its type; or why not."""
    kid = jwk.get("kid")
    if (meant := jwk.get("alg")) is not None and meant != algorithm:
        return f"the token is signed with {algorithm}, and the sign-in system's key {kid} is for {meant}"
    try:
        parsed: object = jwt.PyJWK(dict(jwk), algorithm=algorithm).key
    except jwt.PyJWTError:
        return f"the sign-in system's key {kid} does not fit {algorithm}"
    if (key := _verifying(parsed)) is None or not fits(algorithm, key):
        return f"the sign-in system's key {kid} is not a public key that fits {algorithm}"
    return key


def _verifying(key: object) -> VerifyingKey | None:
    if isinstance(key, rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey | ed448.Ed448PublicKey):
        return key
    return None


def fits(algorithm: Algorithm, key: VerifyingKey) -> bool:
    """Whether `key` verifies `algorithm`: by its type, and by its curve for ECDSA."""
    match algorithm:
        case "RS256" | "RS384" | "RS512" | "PS256" | "PS384" | "PS512":
            return isinstance(key, rsa.RSAPublicKey)
        case "ES256" | "ES384" | "ES512":
            return isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == _CURVES[algorithm]
        case "EdDSA":
            return isinstance(key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey)
        case unreachable:
            assert_never(unreachable)


def plan(service: Service, settings: Settings, *, now: Callable[[], datetime] = _utc_now, monotonic: Callable[[], float] = time.monotonic, machine: Machine | None = None) -> Verifier | None:
    """The verifier of `service`'s tokens, or None for a service that declares no user, after checking that its
    settings configure identity where, and only where, the service declares a user, and that a public key, or the
    development key, fits every algorithm they accept. Every app is planned here, so the development key is refused
    here unless `machine`, by default this one, is declared a developer's desktop, or the settings are a test's own,
    from `Settings.given`, which nothing outside code can make."""
    identity = settings.identity
    if service.user is None:
        if identity is not None:
            msg = (
                f"BASIS_IDENTITY__* is set, and service {service.name} declares no user, so no token would be verified:"
                " declare user=api.User, or a subclass, in its api.Service, or unset BASIS_IDENTITY__*"
            )
            raise SettingsError(msg)
        return None
    if identity is None:
        names = ", ".join(variable(("identity", part)) for part in ("issuer", "audience", "algorithms", "leeway_s"))
        msg = (
            f"service {service.name} declares a user, and its identity is not configured, so the API cannot start: set {names} and {variable(('identity', 'keys', 'kind'))}, with the rest of its block"
        )
        raise SettingsError(msg)
    keys: Keys
    match identity.keys:
        case Jwks():
            keys = KeySet(identity.keys, monotonic=monotonic)
        case PublicKey():
            keys = FixedKey(_fitting(_read(identity.keys.path), identity.algorithms, f"the public key in {identity.keys.path}"))
        case Development():
            if not settings.from_arguments:
                refuse_development(identity, machine if machine is not None else Machine.here())
            keys = FixedKey(_fitting(_DEVELOPMENT, identity.algorithms, "the development key, an ES256 key,"))
        case unreachable:
            assert_never(unreachable)
    return Verifier(identity, service.user, keys, now=now)


def _read(path: Path) -> VerifyingKey:
    """The public key in the PEM file at `path`: a `PUBLIC KEY`, or the key of a `CERTIFICATE`."""
    where = variable(("identity", "keys", "path"))
    try:
        pem = path.read_bytes()
    except OSError as error:
        msg = f"{where} names {path}, which cannot be read ({error.strerror}): name a PEM file of the sign-in system's public key"
        raise SettingsError(msg) from None
    try:
        found: object = x509.load_pem_x509_certificate(pem).public_key() if b"-----BEGIN CERTIFICATE-----" in pem else serialization.load_pem_public_key(pem)
    except ValueError:
        msg = f"{where} names {path}, which holds no PEM public key or certificate: name a PEM file of the sign-in system's public key"
        raise SettingsError(msg) from None
    if (key := _verifying(found)) is None:
        msg = f"{where} names {path}, whose key is a {type(found).__name__}, which verifies none of the algorithms basis accepts: name an RSA, EC or Ed25519 public key"
        raise SettingsError(msg)
    return key


def _fitting(key: VerifyingKey, algorithms: Sequence[Algorithm], described: str) -> VerifyingKey:
    """`key`, after checking that it fits each of `algorithms`, and is long enough for it."""
    where = variable(("identity", "algorithms"))
    if unfit := [algorithm for algorithm in algorithms if not fits(algorithm, key)]:
        msg = f"{where} names {', '.join(unfit)}, which {described} does not fit: name only the algorithms the sign-in system signs with, which its key fits"
        raise SettingsError(msg)
    for algorithm in algorithms:
        if (short := get_default_algorithms()[algorithm].check_key_length(key)) is not None:
            msg = f"{described} is too short to verify with: {short}: ask the sign-in system for a longer key"
            raise SettingsError(msg)
    return key


class Bearer:
    """The middleware that verifies each request's bearer token, inside the request boundary, which gave the request
    its id: it puts the request's user in its scope under `USER`, None for an anonymous request; adds `user` to every
    record logged while it is served, and `enduser.id` to its server span; and answers a token it refuses with a 401
    problem. A health check is left alone."""

    def __init__(self, app: ASGIApp, verifier: Verifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or str(scope["path"]).startswith(HEALTH):
            await self.app(scope, receive, send)
            return
        span = trace.get_current_span()
        try:
            user, expires = await self._user(scope)
        except (InvalidTokenError, KeysUnavailableError) as refused:
            level = logging.INFO if isinstance(refused, InvalidTokenError) else logging.WARNING
            why = {"why": "; ".join(refused.__notes__)} if getattr(refused, "__notes__", None) else {}
            log.log(level, "refused the bearer token: %s", refused.detail, extra={"refused": refused.type, **why})
            span.set_attribute("basis.identity.refused", refused.detail)
            request_id = scope.get(REQUEST_ID)
            await errors.response(refused, request_id if isinstance(request_id, UUID) else None)(scope, receive, send)
            return
        scope[USER] = user
        if expires is not None:
            scope[EXPIRES] = expires
        if user is not None:
            span.set_attribute("enduser.id", user.subject)
        with logs.fields(user=user.subject if user is not None else None):
            await self.app(scope, receive, send)

    async def _user(self, scope: Scope) -> tuple[User | None, float | None]:
        found = [value for name, value in scope["headers"] if name == b"authorization"]
        if not found:
            return None, None
        if len(found) > 1:
            msg = "the request has more than one Authorization header"
            raise InvalidTokenError(msg)
        scheme, _, token = bytes(found[0]).decode("latin-1").strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            msg = "the Authorization header holds no bearer token: send `Authorization: Bearer TOKEN`, or no Authorization header for an anonymous request"
            raise InvalidTokenError(msg)
        return await self.verifier.verified(token.strip())


def sign(identity: Identity, subject: str, *, issued: datetime, expires: datetime, claims: Mapping[str, JsonValue] | None = None) -> str:
    """A bearer token for `subject`, signed with the development key, for the issuer and the audience `identity`
    names, with `claims` besides those: for a test, or `basis dev`, whose settings verify with the development key."""
    if not isinstance(identity.keys, Development):
        msg = f"sign signs with the development key, and these settings verify with {identity.keys.kind}: set {variable(('identity', 'keys', 'kind'))} to development, in a test or on a desktop"
        raise DeclarationError(msg)
    if issued.tzinfo is None or expires.tzinfo is None:
        msg = "a token's times are aware datetimes, such as datetime(2026, 1, 1, tzinfo=UTC)"
        raise ValueError(msg)
    registered = {"iss": identity.issuer, "aud": identity.audience, "sub": subject, "iat": int(issued.timestamp()), "exp": int(expires.timestamp())}
    return jwt.encode({**(claims or {}), **registered}, _SIGNING, algorithm="ES256", headers={"kid": DEVELOPMENT_KEY["kid"]})
