import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import ssl
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, override

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from jwt.algorithms import get_default_algorithms
from opentelemetry.trace import NoOpTracer

from basis import api
from basis.api import opened, tokens
from basis.api.identity import InvalidTokenError, Jwks, KeysUnavailableError

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

ISSUER = "https://signin.internal.example"
AUDIENCE = "positions"
NOW = datetime(2026, 6, 1, tzinfo=UTC)
AT = int(NOW.timestamp())
LEEWAY_S = 30
SIGNING = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
P256 = ec.generate_private_key(ec.SECP256R1())
P384 = ec.generate_private_key(ec.SECP384R1())
ED25519 = ed25519.Ed25519PrivateKey.generate()
CERTIFICATES_FROM, CERTIFICATES_UNTIL = datetime(2026, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)


class Trader(api.User):
    desk: str
    roles: tuple[str, ...] = ()


def me(user: Annotated[Trader | None, api.given(Trader)]) -> dict[str, str]:
    if user is None:
        msg = "positions are shown to a signed-in user"
        raise api.SignInRequiredError(msg)
    return {"subject": user.subject, "desk": user.desk}


SERVICE = api.Service(name="positions", settings=api.Settings, user=Trader, routes=[api.get("/me", me, deadline_s=5, response=dict[str, str], errors=[])])


def token(key: object, *, algorithm: str = "RS256", headers: Mapping[str, object] | None = None, **claims: object) -> str:
    """A token signed with `key`, whose claims are a valid trader's but for `claims`; a claim given as None is left out."""
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "jsmith", "iat": AT - 60, "exp": AT + 3600, "desk": "rates"} | claims
    return jwt.encode({name: value for name, value in payload.items() if value is not None}, key, algorithm=algorithm, headers=dict(headers) if headers is not None else None)  # ty: ignore[invalid-argument-type] -- each test's key


def crafted(header: Mapping[str, object], sign: Callable[[bytes], bytes]) -> str:
    """A token PyJWT would refuse to make, such as one signed with `none`, or with HMAC under a public key."""
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "jsmith", "iat": AT - 60, "exp": AT + 3600, "desk": "rates"}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(sign(signing_input.encode()))}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pem(key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def jwk(key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey, **fields: str) -> dict[str, object]:
    algorithm = "RS256" if isinstance(key, rsa.RSAPublicKey) else "ES256"
    found: object = get_default_algorithms()[algorithm].to_jwk(key, as_dict=True)
    assert isinstance(found, dict)
    return {str(name): value for name, value in found.items()} | fields


def identity(keys: Mapping[str, object], *, algorithms: tuple[str, ...] = ("RS256",)) -> dict[str, object]:
    return {"issuer": ISSUER, "audience": AUDIENCE, "algorithms": list(algorithms), "leeway_s": LEEWAY_S, "keys": dict(keys)}


@dataclass
class Clock:
    """A monotonic clock a test moves."""

    at: float = 0

    def __call__(self) -> float:
        return self.at


def verifier(keys: Mapping[str, object], *, algorithms: tuple[str, ...] = ("RS256",), monotonic: Clock | None = None) -> tokens.Verifier:
    found = tokens.plan(SERVICE, api.Settings.given(identity=identity(keys, algorithms=algorithms)), now=lambda: NOW, monotonic=monotonic if monotonic is not None else Clock())
    assert found is not None
    return found


def outcome(verifying: tokens.Verifier, *tokens_: str) -> list[str]:
    """What verifying each token, in turn, gives: the user's subject and desk, or why it is refused."""

    async def main() -> list[str]:
        await verifying.open(NoOpTracer())
        try:
            return [await _one(verifying, found) for found in tokens_]
        finally:
            await verifying.close()

    return asyncio.run(main())


async def _one(verifying: tokens.Verifier, found: str) -> str:
    try:
        user = await verifying.verify(found)
    except InvalidTokenError as refused:
        return f"refused: {refused.detail}"
    except KeysUnavailableError as unavailable:
        return f"unavailable: {unavailable.detail}"
    assert isinstance(user, Trader)
    return f"{user.subject} of {user.desk}"


@pytest.fixture(scope="module")
def public_key(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    path = tmp_path_factory.mktemp("keys") / "signin.pem"
    path.write_bytes(pem(SIGNING.public_key()))
    return {"kind": "public_key", "path": str(path)}


@pytest.mark.parametrize(
    ("made", "expected"),
    [
        (lambda: token(SIGNING), "jsmith of rates"),
        (lambda: token(SIGNING, exp=AT - LEEWAY_S + 1), "jsmith of rates"),
        (lambda: token(SIGNING, nbf=AT + LEEWAY_S, iat=AT + LEEWAY_S), "jsmith of rates"),
        (lambda: token(SIGNING, aud=["pricing", AUDIENCE]), "jsmith of rates"),
        (lambda: token(SIGNING, exp=AT - LEEWAY_S), "refused: the token expired at 2026-05-31T23:59:30+00:00"),
        (lambda: token(SIGNING, nbf=AT + LEEWAY_S + 1), "refused: the token is not valid until 2026-06-01T00:00:31+00:00"),
        (lambda: token(SIGNING, iat=AT + LEEWAY_S + 1), "refused: the token is issued in the future, at 2026-06-01T00:00:31+00:00"),
        (lambda: token(SIGNING, exp="tomorrow"), "refused: the token's exp claim is not a time, in seconds since 1970"),
        (lambda: token(SIGNING, exp=10**400), "refused: the token's exp claim is not a time, in seconds since 1970"),
        (lambda: token(SIGNING, nbf=10**400), "refused: the token's nbf claim is not a time, in seconds since 1970"),
        (lambda: token(SIGNING, iat=-1), "refused: the token's iat claim is not a time, in seconds since 1970"),
        (lambda: token(SIGNING, exp=1e300), "refused: the token's exp claim is not a time, in seconds since 1970"),
        (lambda: token(SIGNING, nbf=253402300799), "refused: the token is not valid until 9999-12-31T23:59:59+00:00"),
        (lambda: token(SIGNING, exp=None), "refused: the token has no exp claim"),
        (lambda: token(SIGNING, sub=None), "refused: the token has no sub claim"),
        (lambda: token(SIGNING, iss="https://elsewhere.example"), f"refused: the token is not from this API's issuer, {ISSUER}"),
        (lambda: token(SIGNING, aud="pricing"), f"refused: the token is not for this API, whose audience is {AUDIENCE}"),
        (lambda: token(SIGNING, desk=None), "refused: the token has no desk claim, which this API needs"),
        (lambda: token(SIGNING, roles=7), "refused: the token's roles claim is not valid: Input should be a valid tuple"),
        (lambda: token(OTHER), "refused: the token's signature does not verify"),
        (lambda: token(SIGNING, algorithm="RS512"), "refused: the token is signed with 'RS512', and this API accepts RS256"),
        (lambda: crafted({"alg": "none", "typ": "JWT"}, lambda _: b""), "refused: the token is signed with 'none', and this API accepts RS256"),
        (
            lambda: crafted({"alg": "HS256", "typ": "JWT"}, lambda data: hmac.new(pem(SIGNING.public_key()), data, hashlib.sha256).digest()),
            "refused: the token is signed with 'HS256', and this API accepts RS256",
        ),
        (lambda: token(OTHER, headers={"jwk": jwk(OTHER.public_key())}), "refused: the token's signature does not verify"),
        (lambda: "not.a.jwt", "refused: the bearer token is not a JWT this API reads: Invalid header padding"),
    ],
    ids=[
        "valid",
        "expires-within-the-leeway",
        "starts-and-is-issued-within-the-leeway",
        "one-audience-of-several",
        "expired",
        "not-yet-valid",
        "issued-in-the-future",
        "expiry-not-a-time",
        "an-expiry-past-any-float",
        "a-start-past-any-float",
        "issued-before-1970",
        "an-expiry-past-the-year-9999",
        "a-start-in-the-last-second-a-datetime-holds",
        "no-expiry",
        "no-subject",
        "another-issuer",
        "another-audience",
        "a-declared-claim-missing",
        "a-declared-claim-not-valid",
        "another-key",
        "an-algorithm-not-accepted",
        "alg-none",
        "hmac-under-the-public-key",
        "a-key-the-token-carries",
        "not-a-jwt",
    ],
)
def test_a_token_is_verified_against_the_settings_and_the_clock(public_key: dict[str, object], made: Callable[[], str], expected: str) -> None:
    assert outcome(verifier(public_key), made()) == [expected]


def test_a_critical_extension_pyjwt_does_not_know_is_refused(public_key: dict[str, object]) -> None:
    [found] = outcome(verifier(public_key), token(SIGNING, headers={"crit": ["exp-policy"], "exp-policy": 1}))
    assert found == "refused: the bearer token is not a JWT this API reads: Unsupported critical extension: exp-policy"


@pytest.mark.parametrize(
    ("key", "algorithms", "refusal"),
    [
        (SIGNING.public_key(), ("RS256", "PS256"), None),
        (P256.public_key(), ("ES256",), None),
        (ED25519.public_key(), ("EdDSA",), None),
        (SIGNING.public_key(), ("RS256", "ES256"), "BASIS_IDENTITY__ALGORITHMS names ES256, which the public key in"),
        (P256.public_key(), ("ES384",), "BASIS_IDENTITY__ALGORITHMS names ES384, which the public key in"),
        (rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key(), ("RS256",), "is too short to verify with"),  # noqa: S505 -- too short on purpose
    ],
    ids=["rsa", "p-256", "ed25519", "rsa-for-ecdsa", "p-256-for-p-384", "rsa-1024"],
)
def test_a_public_key_must_fit_every_algorithm_accepted(
    tmp_path: Path, key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey, algorithms: tuple[str, ...], refusal: str | None
) -> None:
    path = tmp_path / "signin.pem"
    path.write_bytes(pem(key))
    if refusal is None:
        verifier({"kind": "public_key", "path": str(path)}, algorithms=algorithms)
        return
    with pytest.raises(api.SettingsError, match=refusal):
        verifier({"kind": "public_key", "path": str(path)}, algorithms=algorithms)


def test_a_certificates_key_verifies(tmp_path: Path) -> None:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "signin.internal.example")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(SIGNING.public_key()).serial_number(1)
        .not_valid_before(CERTIFICATES_FROM).not_valid_after(CERTIFICATES_UNTIL).sign(SIGNING, hashes.SHA256())
    )  # fmt: skip
    path = tmp_path / "signin.crt"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    assert outcome(verifier({"kind": "public_key", "path": str(path)}), token(SIGNING)) == ["jsmith of rates"]


def test_a_file_that_holds_no_public_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "signin.pem"
    path.write_text("not a key\n", encoding="utf-8")
    with pytest.raises(api.SettingsError, match=r"BASIS_IDENTITY__KEYS__PATH names .*signin.pem, which holds no PEM public key or certificate"):
        verifier({"kind": "public_key", "path": str(path)})


def test_the_development_key_fits_es256_alone() -> None:
    with pytest.raises(api.SettingsError, match="BASIS_IDENTITY__ALGORITHMS names RS256, which the development key, an ES256 key, does not fit"):
        verifier({"kind": "development"}, algorithms=("RS256",))


def test_the_published_development_key_is_one_key() -> None:
    """Its public part, which a UI's development mode reads from the same JWK, derives from its private part."""
    found: object = get_default_algorithms()["ES256"].from_jwk(dict(tokens.DEVELOPMENT_KEY))
    assert isinstance(found, ec.EllipticCurvePrivateKey)
    assert {name: jwk(found.public_key())[name] for name in ("x", "y")} == {name: tokens.DEVELOPMENT_KEY[name] for name in ("x", "y")}


def test_only_the_development_key_signs() -> None:
    settings = api.Settings.given(identity=identity({"kind": "jwks", "url": "https://signin.internal.example/jwks.json"}))
    assert settings.identity is not None
    with pytest.raises(api.ServiceError, match="sign signs with the development key, and these settings verify with jwks"):
        tokens.sign(settings.identity, "jsmith", issued=NOW, expires=NOW)


# The sign-in system's JWK set, served over https from 127.0.0.1 with a certificate of a test CA's.


@dataclass
class Signin:
    """The sign-in system's JWK set endpoint, whose answers a test changes, and which counts the fetches it served."""

    url: str
    ca_bundle: Path
    keys: list[dict[str, object]] = field(default_factory=list)
    status: int = HTTPStatus.OK
    fetches: int = 0
    answering: threading.Event = field(default_factory=threading.Event)
    """Cleared, the endpoint holds each fetch until it is set: a slow sign-in system."""

    def __post_init__(self) -> None:
        self.answering.set()

    def settings(self, **changed: object) -> dict[str, object]:
        return {"kind": "jwks", "url": self.url, "ca_bundle": str(self.ca_bundle), "max_age_s": 600, "refresh_s": 30} | changed


@pytest.fixture
def signin(tmp_path: Path) -> Iterator[Signin]:
    server_context, ca_bundle = _tls(tmp_path)
    served: Signin | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert served is not None
            served.fetches += 1
            served.answering.wait(timeout=10)
            body = json.dumps({"keys": served.keys}).encode()
            self.send_response(served.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    served = Signin(url=f"https://127.0.0.1:{server.server_address[1]}/.well-known/jwks.json", ca_bundle=ca_bundle)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        served.answering.set()
        server.shutdown()
        server.server_close()
        thread.join()


def _tls(folder: Path) -> tuple[ssl.SSLContext, Path]:
    """A server context whose certificate, for 127.0.0.1, a new CA signed, and the PEM file of that CA."""
    authority_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    authority = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "basis test CA")])
    usage = dict.fromkeys(("content_commitment", "key_encipherment", "data_encipherment", "key_agreement", "encipher_only", "decipher_only"), False)
    ca = (
        x509.CertificateBuilder().subject_name(authority).issuer_name(authority).public_key(authority_key.public_key()).serial_number(1)
        .not_valid_before(CERTIFICATES_FROM).not_valid_after(CERTIFICATES_UNTIL)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, **usage), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False)
        .sign(authority_key, hashes.SHA256())
    )  # fmt: skip
    server = (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])).issuer_name(authority)
        .public_key(server_key.public_key()).serial_number(2).not_valid_before(CERTIFICATES_FROM).not_valid_after(CERTIFICATES_UNTIL)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .sign(authority_key, hashes.SHA256())
    )  # fmt: skip
    (folder / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (folder / "server.key").write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "server.pem", folder / "server.key")
    return context, folder / "ca.pem"


def test_a_key_the_set_does_not_hold_fetches_it_again_at_most_once_every_refresh(signin: Signin) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]
    clock = Clock()
    rotated = token(OTHER, headers={"kid": "2026-07"})

    async def main() -> list[tuple[str, int]]:
        verifying = verifier(signin.settings(), monotonic=clock)
        await verifying.open(NoOpTracer())
        seen: list[tuple[str, int]] = []
        try:
            for at, added in ((10, False), (31, False), (40, True), (61, False)):
                clock.at = at
                if added:  # the sign-in system starts signing with a key of its own
                    signin.keys.append(jwk(OTHER.public_key(), kid="2026-07"))
                seen.append((await _one(verifying, rotated), signin.fetches))
            seen.append((await _one(verifying, token(SIGNING, headers={"kid": "2026-06"})), signin.fetches))
        finally:
            await verifying.close()
        return seen

    unknown = "refused: the sign-in system's JWK set holds no key 2026-07 that verifies RS256"
    assert asyncio.run(main()) == [(unknown, 1), (unknown, 2), (unknown, 2), ("jsmith of rates", 3), ("jsmith of rates", 3)]


def test_requests_that_need_a_fetch_at_once_share_it(signin: Signin) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]

    clock = Clock()

    async def main() -> list[str]:
        verifying = verifier(signin.settings(), monotonic=clock)
        await verifying.open(NoOpTracer())
        signin.keys.append(jwk(OTHER.public_key(), kid="2026-07"))
        clock.at = 100
        try:
            return list(await asyncio.gather(*(_one(verifying, token(OTHER, headers={"kid": "2026-07"})) for _ in range(5))))
        finally:
            await verifying.close()

    assert asyncio.run(main()) == ["jsmith of rates"] * 5
    assert signin.fetches == 2


def test_a_stale_set_is_fetched_again_in_the_background_and_kept_when_that_fails(signin: Signin, logged: Records) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]
    clock = Clock()
    known, unknown = token(SIGNING, headers={"kid": "2026-06"}), token(OTHER, headers={"kid": "2026-07"})

    async def main() -> list[str]:
        verifying = verifier(signin.settings(), monotonic=clock)
        await verifying.open(NoOpTracer())
        try:
            clock.at = 600  # stale: the next request starts a fetch, and does not wait for it
            signin.status = HTTPStatus.INTERNAL_SERVER_ERROR
            seen = [await _one(verifying, known)]
            clock.at = 610  # within refresh_s of that fetch, which a key the set does not hold waits for, or finds failed
            seen.append(await _one(verifying, unknown))
            clock.at = 620
            seen.append(await _one(verifying, known))
            return seen
        finally:
            await verifying.close()

    assert asyncio.run(main()) == ["jsmith of rates", f"unavailable: {signin.url} answered 500", "jsmith of rates"]
    assert signin.fetches == 2
    assert [record["message"] for record in logged() if record["level"] == "warning"] == [
        f"the sign-in system's JWK set could not be fetched again, and the one fetched before is kept: {signin.url} answered 500"
    ]


def test_a_slow_sign_in_system_stalls_no_request_whose_key_the_set_holds(signin: Signin) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]
    clock = Clock()

    async def main() -> list[str]:
        verifying = verifier(signin.settings(), monotonic=clock)
        await verifying.open(NoOpTracer())
        try:
            clock.at = 600
            signin.answering.clear()  # every fetch from now on waits until the test lets it answer
            signin.keys.append(jwk(OTHER.public_key(), kid="2026-07"))
            known = await asyncio.wait_for(_one(verifying, token(SIGNING, headers={"kid": "2026-06"})), timeout=5)  # fails, not hangs, if it waits
            waiting = asyncio.create_task(_one(verifying, token(OTHER, headers={"kid": "2026-07"})))  # waits for the fetch under way
            while signin.fetches < 2:  # noqa: ASYNC110 -- the endpoint's thread counts the fetch it holds
                await asyncio.sleep(0.01)
            assert not waiting.done()
            signin.answering.set()
            return [known, await waiting]
        finally:
            await verifying.close()

    assert asyncio.run(main()) == ["jsmith of rates", "jsmith of rates"]
    assert signin.fetches == 2


@pytest.mark.parametrize(
    ("keys", "algorithm", "kid", "expected"),
    [
        ([jwk(SIGNING.public_key(), kid="k1")], "RS256", None, "jsmith of rates"),
        ([jwk(SIGNING.public_key(), kid="k1"), jwk(OTHER.public_key(), kid="k2")], "RS256", None, "refused: the token names no key id, kid, and 2 of the sign-in system's keys verify RS256"),
        ([jwk(P256.public_key(), kid="k1"), jwk(SIGNING.public_key(), kid="k2")], "RS256", None, "jsmith of rates"),
        ([jwk(P256.public_key(), kid="k1"), jwk(SIGNING.public_key(), kid="k1")], "RS256", "k1", "jsmith of rates"),
        ([jwk(OTHER.public_key(), kid="k1", alg="PS256"), jwk(SIGNING.public_key(), kid="k1", alg="RS256")], "RS256", "k1", "jsmith of rates"),
        ([jwk(P256.public_key(), kid="k1"), jwk(SIGNING.public_key(), kid="k1", alg="RS256")], "PS256", "k1", "refused: none of the sign-in system's 2 keys k1 verifies PS256"),
        ([jwk(SIGNING.public_key(), kid="k1", alg="RS256")], "PS256", "k1", "refused: the token is signed with PS256, and the sign-in system's key k1 is for RS256"),
        ([jwk(P256.public_key(), kid="k1")], "RS256", "k1", "refused: the sign-in system's key k1 does not fit RS256"),
        ([jwk(SIGNING.public_key(), kid="k1", use="enc")], "RS256", "k1", "refused: the sign-in system's JWK set holds no key k1 that verifies RS256"),
    ],
    ids=[
        "the-only-key-for-a-token-that-names-none",
        "no-kid-among-several",
        "no-kid-and-one-key-that-fits",
        "one-kid-for-an-ec-and-an-rsa-key",
        "one-kid-for-two-algorithms",
        "one-kid-and-no-key-that-fits",
        "a-jwk-meant-for-another-algorithm",
        "a-jwk-of-another-type",
        "an-encryption-key",
    ],
)
def test_a_jwk_verifies_only_the_algorithm_it_fits(signin: Signin, keys: list[dict[str, object]], algorithm: str, kid: str | None, expected: str) -> None:
    signin.keys = keys
    found = token(SIGNING, algorithm=algorithm, headers={"kid": kid} if kid is not None else None)
    assert outcome(verifier(signin.settings(), algorithms=("RS256", "PS256", "ES256")), found) == [expected]


def test_an_es256_token_does_not_verify_under_a_p384_key(signin: Signin) -> None:
    signin.keys = [jwk(P384.public_key(), kid="k1")]
    [found] = outcome(verifier(signin.settings(), algorithms=("ES256", "ES384")), token(P256, algorithm="ES256", headers={"kid": "k1"}))
    assert found.startswith("refused: the sign-in system's key k1 ")


def test_the_app_fetches_the_set_before_it_is_ready_through_its_settings_alone(signin: Signin, monkeypatch: pytest.MonkeyPatch) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # a proxy that answers nothing, which the app must not use
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    app = api.build(SERVICE, settings=api.Settings.given(identity=identity(signin.settings())))
    with TestClient(app.asgi) as client:
        assert signin.fetches == 1
        response = client.get("/me", headers={"authorization": f"Bearer {token(SIGNING, headers={'kid': '2026-06'}, exp=4102444800)}"})
    assert (response.status_code, response.json()) == (HTTPStatus.OK, {"subject": "jsmith", "desk": "rates"})


def test_a_time_no_float_holds_is_a_401(public_key: dict[str, object]) -> None:
    app = api.build(SERVICE, settings=api.Settings.given(identity=identity(public_key)))
    with TestClient(app.asgi) as client:
        response = client.get("/me", headers={"authorization": f"Bearer {token(SIGNING, exp=10**400)}"})
    assert (response.status_code, response.json()["type"], response.json()["detail"]) == (HTTPStatus.UNAUTHORIZED, "invalid-token", "the token's exp claim is not a time, in seconds since 1970")


def test_the_set_is_fetched_once_the_app_has_started_and_exports_spans(signin: Signin, capsys: pytest.CaptureFixture[str]) -> None:
    signin.keys = [jwk(SIGNING.public_key(), kid="2026-06")]
    app = api.build(SERVICE, settings=api.Settings.given(identity=identity(signin.settings()), traces={"exporter": "console"}))
    with TestClient(app.asgi):
        pass
    assert '"name": "identity keys GET"' in capsys.readouterr().err
    with pytest.raises(api.ServiceError, match="has started once already"), TestClient(app.asgi):
        pass
    assert signin.fetches == 1


def test_an_app_whose_set_cannot_be_fetched_does_not_start(signin: Signin) -> None:
    signin.status = HTTPStatus.NOT_FOUND
    app = api.build(SERVICE, settings=api.Settings.given(identity=identity(signin.settings())))
    with pytest.raises(KeysUnavailableError, match=f"{signin.url} answered 404"), TestClient(app.asgi):
        pass
    assert app.lifecycle.stage == "starting"


def test_a_set_that_cannot_be_fetched_at_startup_leaves_no_client_open(signin: Signin) -> None:
    signin.status = HTTPStatus.SERVICE_UNAVAILABLE
    keys = tokens.KeySet(Jwks.model_validate(signin.settings()), monotonic=Clock())

    async def main() -> bool:
        resources = opened.Resources.open(opened.plan(SERVICE, api.Settings.given(identity=identity(signin.settings())), {}), NoOpTracer(), keys)
        with pytest.raises(KeysUnavailableError):
            await resources
        return keys.closed

    assert asyncio.run(main())


def test_the_set_is_fetched_over_https_alone() -> None:
    with pytest.raises(ValueError, match="the key set is fetched over https only"):
        api.Settings.given(identity=identity({"kind": "jwks", "url": "http://signin.internal.example/jwks.json"}))
