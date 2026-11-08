"""basis-api's part of the `basis` command, in a repo whose `[tool.basis] parts` include `api`:

- `basis serve [SERVICE] --env ENV` runs the repo's API as `api.serve` does, until SIGTERM, and is its image's
  command; it never touches git;
- `basis dev [SERVICE]` runs it on the fakes its service declares, on 127.0.0.1 alone, until Ctrl+C, and points the
  clone at its hooks, as the verbs a developer runs do;
- `basis call ROUTE [REQUEST] --fake|--real` runs one route in this process and prints its response (`call.py`),
  `--real` in the environment `--env` names;
- `basis relay [SERVICE] --env ENV` makes again the calls the service's outboxes still hold, and fails when one has
  aged out (`relay.py`), which the repo's scheduler runs on a schedule;

The verbs that reach the firm's systems, `serve`, `call --real` and `relay`, act in the environment `--env` or
`BASIS_ENV` names, with its settings (`basis.core.environments`), and in prod refuse to start where the settings give a
database a write login, unless the deployment sets `BASIS_PROD_WRITES=1`. `dev` and `call --fake` reach nothing, and
take no environment. The library's `api.build`, `api.serve`, `api.call_route` and `api.serve_on_fakes` take their
settings in code, and never need one.
- `basis ddl [SERVICE] --dialect DIALECT`, basis-core's verb, prints the CREATE TABLE of each outbox the service's
  databases declare (`outboxes.py`).
- `basis sync` writes, and `basis check` checks, the repo's `openapi.json` with the API's other files (`sync.py`), and
  `basis check --full` compares it with the last release's (`contract.py`);
- `basis check` holds the repo's code to its layers (`layers.py`), and builds the app on its fakes, which Schemathesis
  then sends the requests it makes from its document (`fuzz.py`).

A failure prints as one line, as every `basis` failure does: a service that declares too little, settings that name no
value the API needs, or a call whose response is an error. uvicorn loads when a verb runs, not for every
`basis --help`."""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_settings import SettingsError as SourceError

from basis.api import sync
from basis.api.resources import DatabaseResource
from basis.api.service import GROUP, Service, load
from basis.api.settings import Settings, unreadable
from basis.core import command, environments, hooks
from basis.core.destination import Dialect
from basis.core.environments import Environment
from basis.core.sync import Problem
from basis.core.verbs import Part, Runs, Shared, Verb, fail

RELAY = "make the calls the API's outboxes still hold, once each, and fail when one has aged out: run it on a schedule"
LOCAL = "127.0.0.1"
"""Where `basis dev` serves: this machine alone, whatever the settings say."""


@dataclass(frozen=True)
class ServeArgs:
    service: str | None
    """The service's name in the `basis.apis` entry points, or its module path; None for the one the repo lists."""
    env: str | None = None
    """The environment `--env` names, or None for `BASIS_ENV`'s."""


@dataclass(frozen=True)
class DevArgs:
    service: str | None
    repo: Path
    """The repo's root, as `basis` found it."""


@dataclass(frozen=True)
class CallArgs:
    route: str
    request: str
    """The request's JSON, or `-` for stdin's."""
    resources: Literal["real", "fake"]
    service: str | None
    env: str | None = None


@dataclass(frozen=True)
class DdlArgs:
    service: str | None
    dialect: Dialect


def part() -> Part:
    """basis-api's verbs, which the `basis.verbs` entry point named `api` gives the `basis` command; its files, its
    block of `AGENTS.md`, its line of the Dockerfile and the repo's `openapi.json`; its contract, the breaking-change
    check of `openapi.json`; and its checks of the repo's code, its layers and its app on its fakes."""
    return Part(
        verbs=(
            Verb("serve", "serve the API with uvicorn until SIGTERM, which drains it first", _reaching(_service_argument), lambda n: _serve(ServeArgs(n.service, n.env))),
            Verb("dev", "serve the API on its declared fakes, on this machine alone, until Ctrl+C", _service_argument, lambda n: _dev(DevArgs(n.service, Path(n.repo_root)))),
            Verb("call", "run one route in this process, on the API's fakes or its real resources, and print its response as JSON", _call_arguments, lambda n: _call(_call_args(n))),
            Verb("relay", RELAY, _reaching(_service_argument), lambda n: _relay(ServeArgs(n.service, n.env))),
        ),
        files=sync.agent_files,
        runs=Runs(
            noun="service",
            names=_services,
            ddl=Shared("print the CREATE TABLE of each outbox the service's databases declare", lambda n: _ddl(DdlArgs(n.name, "mssql" if n.dialect == "mssql" else "oracle")), _ddl_arguments),
        ),
        contract=_contract,
        smoke=_smoke,
        checks=_checks,
    )


def _checks(repo: Path) -> list[Problem]:
    from basis.api import fuzz, layers  # noqa: PLC0415 -- grimp, FastAPI and Schemathesis load when the check runs, not for every `basis --help`

    return [*layers.problems(repo), *fuzz.problems()]


def _smoke(repo: Path, env: str, commit: str | None) -> list[Problem]:
    from basis.api import smoke  # noqa: PLC0415 -- the client loads when the verb runs

    return smoke.smoke(repo, env, commit=commit)


def _contract(repo: Path) -> str | None:
    from basis.api import contract  # noqa: PLC0415 -- FastAPI and pyarrow load when the check runs, not for every `basis --help`

    return contract.contract(repo)


def _services() -> dict[str, str]:
    """Each service the installed packages list in the `basis.apis` entry points, by name, with its module path."""
    return {entry.name: entry.value for entry in entry_points(group=GROUP)}


def _ddl_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dialect", choices=["mssql", "oracle"], required=True)


def _service_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("service", nargs="?", help="the service's name in the basis.apis entry points, or its module path, as basis.api.example.service; by default the one the repo lists")


def _call_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("route", help="the route's name, its handler's, as accrued_interest")
    parser.add_argument("request", nargs="?", default="{}", help='the request as JSON, {"path": {...}, "query": {...}, "headers": {...}, "body": ...}, or - to read it from stdin; by default {}')
    resources = parser.add_mutually_exclusive_group(required=True)
    resources.add_argument("--fake", dest="resources", action="store_const", const="fake", help="run on the fakes the service declares, as basis dev does")
    resources.add_argument("--real", dest="resources", action="store_const", const="real", help="run on the real resources, as the settings configure them")
    parser.add_argument("--service", help="the service's name in the basis.apis entry points, or its module path; by default the one the repo lists")
    command.environment_argument(parser)


def _reaching(arguments: Callable[[argparse.ArgumentParser], None]) -> Callable[[argparse.ArgumentParser], None]:
    """`arguments`, and `--env`, which a verb that reaches the firm's systems takes."""

    def both(parser: argparse.ArgumentParser) -> None:
        arguments(parser)
        command.environment_argument(parser)

    return both


def _environment(given: str | None, service: Service, what: str) -> Environment[Settings]:
    """The environment `given` names, or `BASIS_ENV`, with the service's settings for it; in prod, refused where those
    settings give a database a write login and the deployment does not allow writes."""
    try:
        environment = environments.load(given, service.settings)
    except (ValidationError, SourceError) as error:
        raise unreadable(error, service.settings) from None  # the validation error would print every value beside the field it names
    if writing := sorted(name for name, database in environment.settings.databases.items() if database.write is not None):
        environment.refuse_writes(f"{what}, with write logins to {', '.join(writing)},")
    return environment


def _call_args(namespace: argparse.Namespace) -> CallArgs:
    resources: Literal["real", "fake"] = "fake" if namespace.resources == "fake" else "real"
    request = str(namespace.request)
    return CallArgs(str(namespace.route), sys.stdin.read() if request == "-" else request, resources, namespace.service, namespace.env)


def _serve(args: ServeArgs) -> int:
    from basis.api.server import serve  # noqa: PLC0415 -- the server loads when the verb runs

    found = load(args.service)
    environment = _environment(args.env, found, f"basis serve {found.name}")
    serve(found, settings=environment.settings, environment=environment.name)
    return 0


def _dev(args: DevArgs) -> int:
    from basis.api import dev  # noqa: PLC0415 -- the fakes and the server load when the verb runs
    from basis.api.server import serve  # noqa: PLC0415

    hooks.ensure(args.repo)
    found = load(args.service)
    serve(found, fakes=dev.fakes(found), host=LOCAL)
    return 0


def _call(args: CallArgs) -> int:
    from basis.api.call import call  # noqa: PLC0415 -- the app loads when the verb runs

    found = load(args.service)
    if args.resources == "fake":
        return call(found, args.route, args.request, resources="fake")
    environment = _environment(args.env, found, f"basis call {args.route} --real")
    return call(found, args.route, args.request, resources="real", settings=environment.settings)


def _relay(args: ServeArgs) -> int:
    from basis.api import relay  # noqa: PLC0415 -- the clients and the drivers load when the verb runs
    from basis.core import logs, snapshots, traces  # noqa: PLC0415

    found = load(args.service)
    environment = _environment(args.env, found, f"basis relay {found.name}")
    planned = relay.plan(found, environment.settings, {})
    run_id = snapshots.new_run_id()
    settings = planned.settings
    with (
        logs.fields(run_id=run_id, relay=found.name),  # on each of the run's records, as a job's run has its id
        traces.trace("relay", service=found.name, settings=settings.traces, version=settings.git_sha, environment=environment.name, **{"basis.run_id": run_id}) as (tracer, _),
    ):  # one trace, its spans exported when it ends
        relayed = relay.relay(planned, tracer)
    sys.stdout.write(json.dumps({"service": found.name, "outboxes": [entry.summary() for entry in relayed]}, indent=2) + "\n")
    return fail(*failed) if (failed := relay.failure(relayed, found.name)) is not None else 0


def _ddl(args: DdlArgs) -> int:
    from basis.api import outboxes  # noqa: PLC0415 -- the DDL loads when the verb runs

    found = load(args.service)
    declared = [(resource.name, resource.outbox) for resource in found.resources if isinstance(resource, DatabaseResource) and resource.outbox is not None]
    if not declared:
        return fail(
            f"service {found.name} keeps no outbox, so it has no table to create",
            "declare one where a route sends a call after its commit, as api.database(NAME, ..., outbox=api.outbox(TABLE, ...)); an API's requests write no ledger",
        )
    sys.stdout.write("\n".join(outboxes.ddl(database, declared_outbox.table, args.dialect) for database, declared_outbox in declared))
    return 0


log = logging.getLogger(__name__)

FIRST_JOB = "positions"
"""The job `basis-api new app` makes beside the API, a copy of basis-job's example."""


@dataclass(frozen=True)
class NewArgs:
    name: str
    job: str | None
    directory: Path
    index: str | None
    framework_path: Path | None


def scaffold(argv: Sequence[str] | None = None) -> int:
    """The `basis-api` console script, which `uvx basis-api new app NAME` runs."""
    return command.standalone("basis-api", "Make a repo of a basis API and its jobs.", [Verb("new", "make an API repo: basis-api new app NAME [JOB]", _new_arguments, _new_from)], argv)


def _new_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("what", metavar="app", choices=["app"], help="what to make: an app")
    parser.add_argument("name", metavar="NAME", help="the app's name, and its repo's folder")
    parser.add_argument("job", metavar="JOB", nargs="?", default=FIRST_JOB, help=f"the first job beside it, a copy of basis-job's example (default: {FIRST_JOB})")
    parser.add_argument("--dir", type=Path, default=Path(), help="where to make the repo")
    parser.add_argument("--index", help="the package index basis is published to, written into the repo")
    parser.add_argument("--framework-path", type=Path, help="a checkout of basis, whose packages the repo installs editable")


def _new_from(namespace: argparse.Namespace) -> int:
    from basis.api import new  # noqa: PLC0415 -- the generator loads when the verb runs

    args = NewArgs(namespace.name, namespace.job, namespace.dir, namespace.index, namespace.framework_path)
    path = new.app(args.directory / args.name, args.name, args.job, index=args.index, framework_path=args.framework_path)
    log.info("made %s: run `uv run basis check` in it", path)
    return 0
