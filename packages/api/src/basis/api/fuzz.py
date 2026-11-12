"""What basis makes of an API's app, in its own process, with no network. `basis check` builds and starts the app on
the fakes its service declares (`dev.py`): `builds`. `basis check --full`, which the push hook runs, builds it once more
and sends it the requests Schemathesis makes from its own OpenAPI document, through `from_asgi`, on fakes too: `fuzz`.
It takes longer than a commit should wait, so the commit hook leaves it to the push. Each failure is one `ERROR` line
with its fix.

The app is built from its settings' defaults alone, never the environment, as a test builds one (`Settings.given`);
a service that verifies tokens is given the development key, as `basis dev` is (`dev.settings`), and every request is
signed in as its example user (`Service.example_user`), so a signed-in handler's body runs; the anonymous request's
401 is basis's own, which its tests check. Each compute function runs in the process (`fakes.Compute`), unless the
service's fakes give one; a service that reaches no database and no other service needs no fakes declared.

Schemathesis makes `EXAMPLES` requests to each operation that its document allows, and as many that it refuses, each
the same on every run (`derandomize`), so no check is flaky, and checks that the app never answers 500, answers only
the statuses and content types its document lists, with a body its response's schema holds, and refuses a request
its document refuses. A request its document allows may still be refused with an error the route declares, as one for
a bond that has matured is. What it finds is the smallest request that shows it, which is written as it was sent to a
file of its own in the system's temporary folder (`FAILURES`), never the repo, which `basis call ROUTE @FILE --fake`
reads and makes again on the fakes: its path and query exactly as the URL carried them, its body, as JSON or as its
bytes, which a command line could not always hold, and the claims of the user it signed in as. The app's logs of its
requests go nowhere while it runs; Hypothesis keeps no examples, and what else it keeps goes to a temporary folder,
removed after."""

import base64
import json
import logging
import shlex
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from basis.api import call, dev, fakes, layers
from basis.api.app import App, build
from basis.api.compute import ComputeFunction
from basis.api.identity import User
from basis.api.loading import load
from basis.api.resources import DatabaseResource, HttpResource
from basis.api.service import Service
from basis.api.settings import Settings
from basis.core.errors import BasisError
from basis.core.sync import Problem

if TYPE_CHECKING:
    from hypothesis.strategies import SearchStrategy
    from schemathesis import Case, CheckFunction

    type Request = Case[Any]
    """A request Schemathesis makes of an operation, of whichever kind of operation its document has."""

EXAMPLES = 50
"""The requests Schemathesis makes to each operation, of each kind: enough to find a route's 500, within a second or
two for a service the size of the example's."""
FAILURES = Path(tempfile.gettempdir()) / "basis-fuzz"
"""Where each request Schemathesis finds is written, as `SERVICE-ROUTE.json`, for `basis call ROUTE @FILE --fake`."""
EXAMPLE_USER: Mapping[str, JsonValue] = {"sub": "example-user"}
"""The user a service is signed in as where it declares no `example_user`: enough for a user that needs no claim but
its subject."""
INSTALL = "add schemathesis to [dependency-groups] dev, and run `uv lock`"
BASE = "http://basis"
"""The base of the URL a request Schemathesis made is prepared on, to read its path and query as sent."""


def built(repo: Path) -> list[Problem]:
    """Each way the repo's one service, as the repo at `repo` holds its code, fails to build or start on its fakes:
    `basis check`'s. Its code is imported from the repo's folder, never the installed package, which in the push hook's
    worktree is the clone's own checkout. A service that does not load is `openapi.json`'s line already."""
    with layers.importable(repo, layers.packages(repo)):
        try:
            service = load()
        except Exception:  # noqa: BLE001 -- basis sync's openapi.json says why, as its one line
            return []
        return builds(service)


def fuzzed(repo: Path) -> list[Problem]:
    """Each request Schemathesis finds the repo's one service answers as its document does not say: `basis check
    --full`'s, with its code imported as `built` imports it."""
    with layers.importable(repo, layers.packages(repo)):
        try:
            service = load()
        except Exception:  # noqa: BLE001 -- basis sync's openapi.json says why, as its one line
            return []
        return fuzz(service)


def builds(service: Service) -> list[Problem]:
    """Each way `service` fails to build or start on its fakes."""
    try:
        with _quiet(), TestClient(_app(service).asgi):
            pass
    except (BasisError, ValidationError) as error:
        return [(f"service {service.name}'s app does not build on the fakes it declares", " ".join(str(error).split()))]
    except Exception as error:  # noqa: BLE001 -- the repo's own code, its fakes or its app's startup, whatever it raises, which `basis dev` shows in full
        said = f"{type(error).__name__}: {' '.join(str(error).split())}"
        return [(f"service {service.name}'s app does not build on the fakes it declares, since it raised {said}", "fix it, as `uv run basis dev` shows it in full")]
    return []


def fuzz(service: Service, *, folder: Path | None = None) -> list[Problem]:
    """Each request Schemathesis finds `service`, on its fakes, answers as its document does not say, each written to
    a file in `folder`, by default `FAILURES`. An app that does not build is `builds`' line, which `basis check --full` printed first."""
    try:
        app = _app(service)
    except Exception:  # noqa: BLE001 -- `builds` says why, in basis check, which basis check --full runs first
        return []
    user: Mapping[str, JsonValue] | None = None
    if service.user is not None:
        user = service.example_user if service.example_user is not None else EXAMPLE_USER
        if service.example_user is None and (needed := _needed(service.user)):
            fix = f'declare the claims of a user to sign in as, as api.Service(..., example_user={{"sub": "jsmith", ...}}), with each claim {service.user.__qualname__} needs'
            return [(f"service {service.name}'s user, {service.user.__qualname__}, needs claims besides its subject, {', '.join(needed)}, to sign in to its app on its fakes", fix)]
    try:
        import schemathesis  # noqa: F401, PLC0415 -- the repo's own, from its development dependencies
    except ImportError:
        return [(f"schemathesis is not installed, and basis check --full sends service {service.name} the requests it makes from its own document", INSTALL)]
    from hypothesis import configuration  # noqa: PLC0415 -- Schemathesis's own
    from schemathesis.python.asgi import shutdown_lifespans  # noqa: PLC0415 -- the repo's own, which is installed

    headers = {"Authorization": f"Bearer {call.signed(service, app.settings, user)}"} if user is not None else {}
    with _quiet(), tempfile.TemporaryDirectory(prefix="basis-fuzz-") as home:
        configuration.set_hypothesis_home_dir(home)  # Hypothesis keeps what it learns of the code in .hypothesis/, in the repo but for this
        try:
            return list(_fuzz(service, app, _Sent(headers, user, folder if folder is not None else FAILURES)))
        finally:
            shutdown_lifespans()  # Schemathesis starts the app's lifespan, and would leave it running until the process ends
            configuration.set_hypothesis_home_dir(None)


def _needed(user: type[User]) -> list[str]:
    """The claims `user` needs that `EXAMPLE_USER` does not give, by the claim's name."""
    try:
        user.model_validate(EXAMPLE_USER)
    except ValidationError as error:
        return [".".join(map(str, entry["loc"])) for entry in error.errors(include_url=False, include_input=False)]
    return []


def _app(service: Service) -> App:
    reaches = any(isinstance(resource, DatabaseResource | HttpResource) for resource in service.resources)
    given = dev.fakes(service) if service.fakes is not None or reaches else {}  # a service that reaches nothing needs no fakes declared
    computed = {ask.given for route in service.routes for ask in route.asks if isinstance(ask.given, ComputeFunction)}
    faked: dict[fakes.Faked, fakes.Fake] = {**{function: fakes.Compute() for function in computed if function not in given}, **given}
    return build(service, settings=_settings(service), fakes=faked)


def _settings(service: Service) -> Settings:
    try:
        given = service.settings.given()
    except ValidationError as error:
        names = ", ".join(".".join(str(part) for part in entry["loc"]) for entry in error.errors(include_url=False, include_input=False))
        msg = f"basis check builds the app from its settings' defaults alone, and {names} {'has' if len(error.errors()) == 1 else 'have'} none: give each a default in the service's Settings"
        raise BasisError(msg) from None
    return dev.settings(service, given)


@dataclass(frozen=True)
class _Sent:
    """What every request carries, and where one that fails is written."""

    headers: Mapping[str, str]
    user: Mapping[str, JsonValue] | None
    folder: Path


@dataclass(frozen=True)
class _Failed:
    case: Request | None
    error: BaseException


def _fuzz(service: Service, app: App, sent: _Sent) -> Generator[Problem]:
    import schemathesis  # noqa: PLC0415 -- the repo's own, which fuzz() found
    from hypothesis.errors import Unsatisfiable  # noqa: PLC0415 -- Schemathesis's own
    from schemathesis import GenerationMode  # noqa: PLC0415
    from schemathesis.checks import not_a_server_error  # noqa: PLC0415
    from schemathesis.core.failures import FailureGroup  # noqa: PLC0415
    from schemathesis.core.result import Err  # noqa: PLC0415
    from schemathesis.specs.openapi.checks import content_type_conformance, negative_data_rejection, response_headers_conformance, response_schema_conformance, status_code_conformance  # noqa: PLC0415

    schema = schemathesis.openapi.from_asgi("/openapi.json", app.asgi)
    checks = [not_a_server_error, status_code_conformance, content_type_conformance, response_schema_conformance, response_headers_conformance]
    for result in schema.get_all_operations():
        if isinstance(result, Err):
            yield f"Schemathesis cannot read an operation of service {service.name}'s document: {' '.join(str(result.err()).split())}", "fix the route it names, which its openapi.json holds"
            continue
        operation = result.ok()
        name = str(operation.definition.raw.get("operationId", operation.label))
        for mode, extra in ((GenerationMode.POSITIVE, []), (GenerationMode.NEGATIVE, [negative_data_rejection])):
            failed: list[_Failed] = []
            try:
                _test(operation.as_strategy(generation_mode=mode), [*checks, *extra], sent.headers, failed)()
            except Unsatisfiable:  # nothing to refuse, as for an operation that takes no parameters
                continue
            except (Exception, FailureGroup) as error:  # noqa: BLE001 -- what Schemathesis found, each its line; its failures are a BaseExceptionGroup
                yield _problem(service, operation.label, name, mode.value, failed=failed[-1] if failed else _Failed(None, error), sent=sent)
                break


def _test(strategy: SearchStrategy[Request], checks: list[CheckFunction], headers: Mapping[str, str], failed: list[_Failed]) -> Callable[..., None]:
    """Hypothesis's test of one operation's requests, of one kind, each with `headers`, which keeps the last request
    that failed."""
    from hypothesis import HealthCheck, Phase, Verbosity, given, settings  # noqa: PLC0415 -- Schemathesis's own
    from schemathesis.core.failures import FailureGroup  # noqa: PLC0415

    def run(case: Request) -> None:
        try:
            case.call_and_validate(headers=dict(headers), checks=checks)
        except (Exception, FailureGroup) as error:
            failed[:] = [_Failed(case, error)]  # the last is the smallest, which Hypothesis runs again before it raises
            raise

    configured = settings(
        max_examples=EXAMPLES, deadline=None, derandomize=True, database=None, suppress_health_check=list(HealthCheck), phases=[Phase.explicit, Phase.generate, Phase.shrink],
        verbosity=Verbosity.quiet, print_blob=False, report_multiple_bugs=False,
    )  # fmt: skip
    return given(case=strategy)(configured(run))


def _problem(service: Service, label: str, name: str, mode: str, *, failed: _Failed, sent: _Sent) -> Problem:
    exceptions = getattr(failed.error, "exceptions", [failed.error])
    named = sorted((str(getattr(entry, "code", "")), str(getattr(entry, "title", type(entry).__name__))) for entry in exceptions)
    found = "; ".join(f"{title} ({code})" if code else title for code, title in named)
    kind = "a request its document allows" if mode == "positive" else "a request its document refuses"
    reason = f"Schemathesis found {label} answers {kind} as its document does not say, {found}"
    if failed.case is None:
        return reason, "fix the route, or declare the error it answers with in its errors=[...]"
    written = sent.folder / f"{service.name}-{name}.json"
    sent.folder.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps(_request(failed.case, sent), indent=2), encoding="utf-8")
    again = f"uv run basis call {name} {shlex.quote(f'@{written}')} --fake"
    return f"{reason}, to the request in {written}", f"make it again with `{again}`, which answers as the route does, and fix the route, or declare the error it answers with in its errors=[...]"


def _request(case: Request, sent: _Sent) -> dict[str, object]:
    """`case`'s request as it was sent, as `basis call` reads one: its path and query as the URL carried them, the
    headers Schemathesis made, its body, as JSON where it sent JSON and as its bytes otherwise, and its user's claims."""
    import requests  # noqa: PLC0415 -- Schemathesis's own, which prepares a request as its transport sends it

    given = case.as_transport_kwargs(base_url=BASE)
    prepared = requests.Request(method=given["method"], url=given["url"], params=given.get("params"), json=given.get("json"), data=given.get("data")).prepare()
    split = urlsplit(prepared.url or BASE)
    request: dict[str, object] = {"target": split.path + (f"?{split.query}" if split.query else "")}
    ours = {key.lower() for key in sent.headers}
    headers = {str(key): str(value) for key, value in (case.headers or {}).items() if str(key).lower() not in ours}
    if "json" in given:
        request["body"] = given["json"]
    elif isinstance(body := prepared.body, bytes | str):  # a prepared body is its bytes or its text, never a stream here
        raw = body if isinstance(body, bytes) else body.encode()
        if isinstance(kind := prepared.headers.get("Content-Type"), str):
            headers.setdefault("content-type", kind)
        try:
            request["content"] = raw.decode()
        except UnicodeDecodeError:
            request["content_base64"] = base64.b64encode(raw).decode()
    if headers:
        request["headers"] = headers
    if sent.user is not None:
        request["user"] = dict(sent.user)
    return request


@contextmanager
def _quiet() -> Generator[None]:
    """No log records while the app answers what basis check sends it: one per request would be thousands of lines."""
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)
