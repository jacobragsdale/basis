"""basis-dash's part of the `basis` command, in a repo whose `[tool.basis] parts` include `dash`:

- `basis serve [APP] --env ENV` runs the repo's Dash app as `bd.serve` does, until SIGTERM, and is its image's
  command; it never touches git;
- `basis dev [APP] [--env ENV]` runs it on this machine alone, with Dash's dev tools and hot reload, until Ctrl+C:
  on the app's fakes, or with `--env` on that environment's resources, never on both.

`serve` reads the settings of the environment `--env` or `BASIS_ENV` names (`basis.core.environments`). A repo serves
one thing, so it declares `dash` or `api`, never both: each adds `serve` and `dev`, and `basis` refuses two declared
parts that add one verb, in one line. uvicorn loads when a verb runs, not for every `basis --help`."""

import argparse
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError as SourceError

from basis.core import command, environments, hooks
from basis.core.verbs import Part, Verb
from basis.dash.loading import load
from basis.dash.settings import unreadable


@dataclass(frozen=True)
class ServeArgs:
    app: str | None
    """The app's name in the `basis.dash_apps` entry points, or its module path; None for the one the repo lists."""
    env: str | None = None


@dataclass(frozen=True)
class DevArgs:
    app: str | None
    repo: Path
    """The repo's root, as `basis` found it."""
    env: str | None = None
    """The environment whose resources it serves the app on, or None for its fakes: never `BASIS_ENV`'s, so a shell's
    variable never turns a fake run real."""


def part() -> Part:
    """basis-dash's verbs, which the `basis.verbs` entry point named `dash` gives the `basis` command."""
    return Part(
        verbs=(
            Verb("serve", "serve the Dash app with uvicorn until SIGTERM, which drains it first", _reaching, lambda n: _serve(ServeArgs(n.app, n.env))),
            Verb(
                "dev",
                "serve the Dash app on this machine alone, on its fakes or with --env ENV on that environment's, with Dash's dev tools and hot reload, until Ctrl+C",
                _dev_arguments,
                lambda n: _dev(DevArgs(n.app, Path(n.repo_root), n.env)),
            ),
        )
    )


def _app_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("app", nargs="?", help="the app's name in the basis.dash_apps entry points, or its module path, as positions.app; by default the one the repo lists")


def _dev_arguments(parser: argparse.ArgumentParser) -> None:
    _app_argument(parser)
    names = ", ".join(environments.NAMES)
    parser.add_argument("--env", metavar="ENV", choices=environments.NAMES, help=f"{names}: serve on that environment's resources, read-only, rather than the app's fakes")


def _reaching(parser: argparse.ArgumentParser) -> None:
    _app_argument(parser)
    command.environment_argument(parser)


def _serve(args: ServeArgs) -> int:
    from basis.dash.server import serve  # noqa: PLC0415 -- the server loads when the verb runs

    found = load(args.app)
    try:
        environment = environments.load(args.env, found.settings)
    except (ValidationError, SourceError) as error:
        raise unreadable(error, found.settings) from None  # the validation error would print every value beside the field it names
    serve(found, settings=environment.settings, environment=environment.name)
    return 0


def _dev(args: DevArgs) -> int:
    from basis.dash.server import dev  # noqa: PLC0415 -- Dash and the server load when the verb runs

    hooks.ensure(args.repo)
    dev(args.app, repo=args.repo, env=args.env)
    return 0
