"""basis-api's part of the `basis` command, in a repo whose `[tool.basis] parts` include `api`:

- `basis serve [SERVICE]` runs the repo's API as `api.serve` does, until SIGTERM, and is its image's command; it
  never touches git;
- `basis dev [SERVICE]` runs it on the fakes its service declares, on 127.0.0.1 alone, until Ctrl+C, and points the
  clone at its hooks, as the verbs a developer runs do;
- `basis call ROUTE [REQUEST] --fake|--real` runs one route in this process and prints its response (`call.py`).

A failure prints as one line, as every `basis` failure does: a service that declares too little, settings that name no
value the API needs, or a call whose response is an error. uvicorn loads when a verb runs, not for every
`basis --help`."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from basis.api import sync
from basis.api.service import load
from basis.core import hooks
from basis.core.verbs import Part, Verb

LOCAL = "127.0.0.1"
"""Where `basis dev` serves: this machine alone, whatever the settings say."""


@dataclass(frozen=True)
class ServeArgs:
    service: str | None
    """The service's name in the `basis.apis` entry points, or its module path; None for the one the repo lists."""


@dataclass(frozen=True)
class CallArgs:
    route: str
    request: str
    """The request's JSON, or `-` for stdin's."""
    resources: Literal["real", "fake"]
    service: str | None


def part() -> Part:
    """basis-api's verbs, which the `basis.verbs` entry point named `api` gives the `basis` command, and its files:
    its block of `AGENTS.md` and its line of the Dockerfile."""
    return Part(
        verbs=(
            Verb("serve", "serve the API with uvicorn until SIGTERM, which drains it first", _service_argument, lambda n: _serve(ServeArgs(n.service))),
            Verb("dev", "serve the API on its declared fakes, on this machine alone, until Ctrl+C", _service_argument, lambda n: _dev(ServeArgs(n.service))),
            Verb("call", "run one route in this process, on the API's fakes or its real resources, and print its response as JSON", _call_arguments, lambda n: _call(_call_args(n))),
        ),
        files=sync.agent_files,
    )


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


def _dev(args: ServeArgs) -> int:
    from basis.api import dev  # noqa: PLC0415 -- the fakes and the server load when the verb runs
    from basis.api.server import serve  # noqa: PLC0415

    cwd = Path.cwd()
    if (root := next((folder for folder in (cwd, *cwd.parents) if (folder / "pyproject.toml").is_file()), None)) is not None:
        hooks.ensure(root)
    found = load(args.service)
    serve(found, fakes=dev.fakes(found), host=LOCAL)
    return 0


def _call(args: CallArgs) -> int:
    from basis.api.call import call  # noqa: PLC0415 -- the app loads when the verb runs

    return call(load(args.service), args.route, args.request, resources=args.resources)
