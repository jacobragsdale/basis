"""What `basis check` makes of an API's app, in its own process, with no network: the app builds and starts on the fakes
its service declares (`dev.py`), and survives the requests Schemathesis makes from its own OpenAPI document, through
`from_asgi`, on fakes too. Each failure is one `ERROR` line with its fix.

The app is built from its settings' defaults alone, never the environment, as a test builds one (`Settings.given`);
a service that verifies tokens is given the development key, so a request without one is anonymous, as Schemathesis
sends it. Each compute function runs in the process (`fakes.Compute`), unless the service's fakes give one; a service
that reaches no database and no other service needs no fakes declared.

Schemathesis makes `EXAMPLES` requests to each operation that its document allows, and as many that it refuses, each
the same on every run (`derandomize`), so no check is flaky, and checks that the app never answers 500, answers only
the statuses and content types its document lists, with a body its response's schema holds, and refuses a request
its document refuses. A request its document allows may still be refused with an error the route declares, as one for
a bond that has matured is. What it finds is the smallest request that shows it, which `basis call` makes again on
the fakes. The app's logs of its requests go nowhere while it runs, and nothing is written to the repo: Hypothesis
keeps no examples, and what else it keeps goes to a temporary folder, removed after."""

import json
import logging
import os
import shlex
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient
from pydantic import ValidationError

from basis.api import dev, fakes
from basis.api.app import App, build
from basis.api.compute import ComputeFunction
from basis.api.resources import DatabaseResource, HttpResource
from basis.api.service import Service, load
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
ISSUER = "basis-check"
FUZZ = "BASIS_FUZZ"
"""The variable that turns Schemathesis on, `1`, for a run of `basis check`. It is off by default for now, while the MVP
takes shape, so the commit hook checks only that the app builds and starts on its fakes (docs/roadmap.md, "Later")."""
INSTALL = "add schemathesis to [dependency-groups] dev, and run `uv lock`"


def problems() -> list[Problem]:
    """Each way the repo's one service fails on its fakes: it does not build or start, or Schemathesis finds a request
    it answers as its document does not say. A service that does not load is `openapi.json`'s line already."""
    try:
        service = load()
    except Exception:  # noqa: BLE001 -- basis sync's openapi.json says why, as its one line
        return []
    return check(service)


def check(service: Service) -> list[Problem]:
    """Each way `service` fails on its fakes, as `problems` finds them."""
    try:
        with _quiet(), TestClient(_app(service).asgi):
            pass
    except (BasisError, ValidationError) as error:
        return [(f"service {service.name}'s app does not build on the fakes it declares", " ".join(str(error).split()))]
    except Exception as error:  # noqa: BLE001 -- the repo's own code, its fakes or its app's startup, whatever it raises, which `basis dev` shows in full
        said = f"{type(error).__name__}: {' '.join(str(error).split())}"
        return [(f"service {service.name}'s app does not build on the fakes it declares, since it raised {said}", "fix it, as `uv run basis dev` shows it in full")]
    if os.environ.get(FUZZ) != "1":
        return []
    try:
        import schemathesis  # noqa: F401, PLC0415 -- the repo's own, from its development dependencies
    except ImportError:
        return [(f"schemathesis is not installed, and basis check sends service {service.name} the requests it makes from its own document", INSTALL)]
    from hypothesis import configuration  # noqa: PLC0415 -- Schemathesis's own
    from schemathesis.python.asgi import shutdown_lifespans  # noqa: PLC0415 -- the repo's own, which is installed

    with _quiet(), tempfile.TemporaryDirectory(prefix="basis-fuzz-") as home:
        configuration.set_hypothesis_home_dir(home)  # Hypothesis keeps what it learns of the code in .hypothesis/, in the repo but for this
        try:
            return list(_fuzz(service, _app(service)))
        finally:
            shutdown_lifespans()  # Schemathesis starts the app's lifespan, and would leave it running until the process ends
            configuration.set_hypothesis_home_dir(None)


def _app(service: Service) -> App:
    reaches = any(isinstance(resource, DatabaseResource | HttpResource) for resource in service.resources)
    given = dev.fakes(service) if service.fakes is not None or reaches else {}  # a service that reaches nothing needs no fakes declared
    computed = {ask.given for route in service.routes for ask in route.asks if isinstance(ask.given, ComputeFunction)}
    faked: dict[fakes.Faked, fakes.Fake] = {**{function: fakes.Compute() for function in computed if function not in given}, **given}
    return build(service, settings=_settings(service), fakes=faked)


def _settings(service: Service) -> Settings:
    try:
        if service.user is None:
            return service.settings.given()
        identity = {"issuer": ISSUER, "audience": service.name, "algorithms": ["ES256"], "leeway_s": 0, "keys": {"kind": "development"}}
        return service.settings.given(identity=identity)
    except ValidationError as error:
        names = ", ".join(".".join(str(part) for part in entry["loc"]) for entry in error.errors(include_url=False, include_input=False))
        msg = f"basis check builds the app from its settings' defaults alone, and {names} {'has' if len(error.errors()) == 1 else 'have'} none: give each a default in the service's Settings"
        raise BasisError(msg) from None


@dataclass(frozen=True)
class _Failed:
    case: Request | None
    error: BaseException


def _fuzz(service: Service, app: App) -> Generator[Problem]:
    import schemathesis  # noqa: PLC0415 -- the repo's own, which problems() found
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
                _test(operation.as_strategy(generation_mode=mode), [*checks, *extra], failed)()
            except Unsatisfiable:  # nothing to refuse, as for an operation that takes no parameters
                continue
            except (Exception, FailureGroup) as error:  # noqa: BLE001 -- what Schemathesis found, each its line; its failures are a BaseExceptionGroup
                yield _problem(operation.label, name, mode.value, failed[-1] if failed else _Failed(None, error))
                break


def _test(strategy: SearchStrategy[Request], checks: list[CheckFunction], failed: list[_Failed]) -> Callable[..., None]:
    """Hypothesis's test of one operation's requests, of one kind, which keeps the last request that failed."""
    from hypothesis import HealthCheck, Phase, Verbosity, given, settings  # noqa: PLC0415 -- Schemathesis's own
    from schemathesis.core.failures import FailureGroup  # noqa: PLC0415

    def run(case: Request) -> None:
        try:
            case.call_and_validate(checks=checks)
        except (Exception, FailureGroup) as error:
            failed[:] = [_Failed(case, error)]  # the last is the smallest, which Hypothesis runs again before it raises
            raise

    configured = settings(
        max_examples=EXAMPLES, deadline=None, derandomize=True, database=None, suppress_health_check=list(HealthCheck), phases=[Phase.explicit, Phase.generate, Phase.shrink],
        verbosity=Verbosity.quiet, print_blob=False, report_multiple_bugs=False,
    )  # fmt: skip
    return given(case=strategy)(configured(run))


def _problem(label: str, name: str, mode: str, failed: _Failed) -> Problem:
    exceptions = getattr(failed.error, "exceptions", [failed.error])
    named = sorted((str(getattr(entry, "code", "")), str(getattr(entry, "title", type(entry).__name__))) for entry in exceptions)
    found = "; ".join(f"{title} ({code})" if code else title for code, title in named)
    request = f" to {_call(name, failed.case)}" if failed.case is not None else ""
    kind = "a request its document allows" if mode == "positive" else "a request its document refuses"
    reason = f"Schemathesis found {label} answers {kind} as its document does not say, {found}{request}"
    fix = "make the request with that command, which answers as the route does, and fix the route, or declare the error it answers with in its errors=[...]"
    return reason, fix


def _call(name: str, case: Request) -> str:
    """The `basis call` that makes `case`'s request again, on the fakes."""
    from schemathesis.core import NOT_SET  # noqa: PLC0415 -- the repo's own, which problems() found

    request: dict[str, object] = {}
    for key, attribute in (("path", "path_parameters"), ("query", "query"), ("headers", "headers")):
        if isinstance(value := getattr(case, attribute, None), Mapping) and value:
            request[key] = dict(value)
    if (body := getattr(case, "body", NOT_SET)) is not NOT_SET:
        request["body"] = body
    return f"`uv run basis call {name} {shlex.quote(json.dumps(request, default=str, ensure_ascii=False))} --fake`"


@contextmanager
def _quiet() -> Generator[None]:
    """No log records while the app answers what basis check sends it: one per request would be thousands of lines."""
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)
