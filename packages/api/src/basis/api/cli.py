"""basis-api's part of the `basis` command, in a repo whose `[tool.basis] parts` include `api`: `basis serve`, which
runs the repo's API as `api.serve` does, until SIGTERM. A failure prints as one line, as every `basis` failure does:
a service that declares too little, or settings that name no value the API needs. uvicorn loads when the verb runs,
not for every `basis --help`."""

import argparse
from dataclasses import dataclass

from basis.core.verbs import Part, Verb


@dataclass(frozen=True)
class ServeArgs:
    service: str | None
    """The service's name in the `basis.apis` entry points, or its module path; None for the one the repo lists."""


def part() -> Part:
    """basis-api's verbs, which the `basis.verbs` entry point named `api` gives the `basis` command."""
    return Part(verbs=(Verb("serve", "serve the API with uvicorn until SIGTERM, which drains it first", _serve_arguments, lambda n: _serve(ServeArgs(n.service))),))


def _serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("service", nargs="?", help="the service's name in the basis.apis entry points, or its module path, as basis.api.example.service; by default the one the repo lists")


def _serve(args: ServeArgs) -> int:
    from basis.api.server import serve  # noqa: PLC0415 -- the server loads when the verb runs

    serve(args.service)
    return 0
