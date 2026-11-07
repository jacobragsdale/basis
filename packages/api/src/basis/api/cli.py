"""basis-api's part of the `basis` command, in a repo whose `[tool.basis] parts` include `api`:

- `basis serve [SERVICE]` runs the repo's API as `api.serve` does, until SIGTERM, and is its image's command; it
  never touches git;
- `basis dev [SERVICE]` runs it on the fakes its service declares, on 127.0.0.1 alone, until Ctrl+C, and points the
  clone at its hooks, as the verbs a developer runs do;
- `basis call ROUTE [REQUEST] --fake|--real` runs one route in this process and prints its response (`call.py`);
- `basis relay [SERVICE]` makes again the calls the service's outboxes still hold, and fails when one has aged out
  (`relay.py`), which the repo's scheduler runs on a schedule;
- `basis ddl [SERVICE] --dialect DIALECT`, basis-core's verb, prints the CREATE TABLE of each outbox the service's
  databases declare (`outboxes.py`).

A failure prints as one line, as every `basis` failure does: a service that declares too little, settings that name no
value the API needs, or a call whose response is an error. uvicorn loads when a verb runs, not for every
`basis --help`."""

import argparse
import json
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal

from basis.api import sync
from basis.api.resources import DatabaseResource
from basis.api.service import GROUP, load
from basis.core import hooks
from basis.core.destination import Dialect
from basis.core.verbs import Part, Runs, Shared, Verb, fail

LOCAL = "127.0.0.1"
"""Where `basis dev` serves: this machine alone, whatever the settings say."""


@dataclass(frozen=True)
class ServeArgs:
    service: str | None
    """The service's name in the `basis.apis` entry points, or its module path; None for the one the repo lists."""


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


@dataclass(frozen=True)
class DdlArgs:
    service: str | None
    dialect: Dialect


def part() -> Part:
    """basis-api's verbs, which the `basis.verbs` entry point named `api` gives the `basis` command, and its files:
    its block of `AGENTS.md` and its line of the Dockerfile."""
    return Part(
        verbs=(
            Verb("serve", "serve the API with uvicorn until SIGTERM, which drains it first", _service_argument, lambda n: _serve(ServeArgs(n.service))),
            Verb("dev", "serve the API on its declared fakes, on this machine alone, until Ctrl+C", _service_argument, lambda n: _dev(DevArgs(n.service, Path(n.repo_root)))),
            Verb("call", "run one route in this process, on the API's fakes or its real resources, and print its response as JSON", _call_arguments, lambda n: _call(_call_args(n))),
            Verb("relay", "make the calls the API's outboxes still hold, once each, and fail when one has aged out: run it on a schedule", _service_argument, lambda n: _relay(ServeArgs(n.service))),
        ),
        files=sync.agent_files,
        runs=Runs(
            noun="service",
            names=_services,
            ddl=Shared("print the CREATE TABLE of each outbox the service's databases declare", lambda n: _ddl(DdlArgs(n.name, "mssql" if n.dialect == "mssql" else "oracle")), _ddl_arguments),
        ),
    )


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


def _call_args(namespace: argparse.Namespace) -> CallArgs:
    resources: Literal["real", "fake"] = "fake" if namespace.resources == "fake" else "real"
    request = str(namespace.request)
    return CallArgs(str(namespace.route), sys.stdin.read() if request == "-" else request, resources, namespace.service)


def _serve(args: ServeArgs) -> int:
    from basis.api.server import serve  # noqa: PLC0415 -- the server loads when the verb runs

    serve(args.service)
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

    return call(load(args.service), args.route, args.request, resources=args.resources)


def _relay(args: ServeArgs) -> int:
    from basis.api import relay, traces  # noqa: PLC0415 -- the clients and the drivers load when the verb runs
    from basis.api.settings import read  # noqa: PLC0415
    from basis.core import logs, snapshots  # noqa: PLC0415

    found = load(args.service)
    planned = relay.plan(found, read(found.settings), {})
    provider = traces.provider(found.name)
    if (exporter := traces.exporter(planned.settings.traces)) is not None:
        provider.add_span_processor(exporter)
    try:
        with logs.fields(run_id=snapshots.new_run_id(), relay=found.name):  # on each of the run's records, as a job's run has its id
            relayed = relay.relay(planned, provider.get_tracer("basis.api"))
    finally:
        provider.shutdown()  # exports the spans still queued
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
