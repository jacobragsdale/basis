"""basis-dash's part of the `basis` command, in a repo whose `[tool.basis] parts` include `dash`:

- `basis serve [APP] --env ENV` runs the repo's Dash app as `bd.serve` does, until SIGTERM, and is its image's
  command; it never touches git;
- `basis dev [APP] [--env ENV]` runs it on this machine alone, with Dash's dev tools and hot reload, until Ctrl+C:
  on the app's fakes, or with `--env` on that environment's resources, never on both;
- `basis render PAGE [--set ID.PROP=VALUE]... [--env ENV]` and `basis load DATASET [--param NAME=VALUE]... [--env ENV]`
  print what a page shows and what a dataset holds, as JSON, on the app's fakes unless an environment is named
  (`basis.dash.render`);
- `basis check` runs the part's checks of the repo's code (`basis.dash.checks`), and `basis smoke --env ENV` its checks
  of the app as that environment serves it (`basis.dash.smoke`).

`serve` reads the settings of the environment `--env` or `BASIS_ENV` names (`basis.core.environments`). A repo serves
one thing, so it declares `dash` or `api`, never both: each adds `serve` and `dev`, and `basis` refuses two declared
parts that add one verb, in one line. uvicorn loads when a verb runs, not for every `basis --help`."""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError as SourceError

from basis.core import command, environments, hooks
from basis.core.errors import BasisError
from basis.core.sync import Problem
from basis.core.verbs import Part, Verb, fail
from basis.dash.app import App
from basis.dash.loading import load
from basis.dash.settings import Settings, unreadable


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
            Verb(
                "render",
                "run a page's callbacks in this process, on the app's fakes or with --env ENV on that environment's, and print what each output shows as JSON",
                _render_arguments,
                lambda n: _render(RenderArgs(n.page, tuple(n.set or ()), n.app, n.env)),
            ),
            Verb(
                "load",
                "load one of the app's datasets in this process, twice, on its fakes or with --env ENV on that environment's, and print its first rows and timings as JSON",
                _load_arguments,
                lambda n: _load(LoadArgs(n.dataset, tuple(n.param or ()), n.app, n.env)),
            ),
        ),
        checks=_checks,
        smoke=_smoke,
    )


def _smoke(repo: Path, env: str, commit: str | None) -> list[Problem]:
    from basis.dash import smoke  # noqa: PLC0415 -- the client and the app load when the verb runs

    return smoke.smoke(repo, env, commit=commit)


def _checks(repo: Path) -> list[Problem]:
    from basis.dash import checks  # noqa: PLC0415 -- grimp, Dash's app and FastAPI's test client load when the check runs

    return checks.problems(repo)


@dataclass(frozen=True)
class RenderArgs:
    page: str
    sets: tuple[str, ...]
    """Each `ID.PROP=VALUE`, in place of the layout's value."""
    app: str | None = None
    env: str | None = None
    """The environment whose resources it reads, or None for the app's fakes."""


@dataclass(frozen=True)
class LoadArgs:
    dataset: str
    params: tuple[str, ...]
    """Each `NAME=VALUE`, the dataset's params as text."""
    app: str | None = None
    env: str | None = None


def _env_argument(parser: argparse.ArgumentParser, what: str) -> None:
    names = ", ".join(environments.NAMES)
    parser.add_argument("--env", metavar="ENV", choices=environments.NAMES, help=f"{names}: {what} on that environment's resources, read-only, rather than the app's fakes")
    parser.add_argument("--app", help="the app's name in the basis.dash_apps entry points, or its module path; by default the one the repo lists")


def _render_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("page", help="the page's path, as /positions, or its title")
    parser.add_argument(
        "--set",
        action="append",
        metavar="ID.PROP=VALUE",
        help="a component's property in place of the layout's, VALUE as JSON reads it or else as text, as --set as-of.value=2026-11-12; again for more",
    )
    _env_argument(parser, "render it")


def _load_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dataset", help="the dataset's name, as bd.query, bd.file, bd.fetch or bd.view declares it")
    parser.add_argument("--param", action="append", metavar="NAME=VALUE", help="one of its params, as --param as_of=2026-11-13; again for more")
    _env_argument(parser, "load it")


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


def _settings_of(app: App, env: str | None) -> tuple[Settings | None, str | None]:
    """The app's settings for the environment `env` names, and its name, or None for the app's fakes."""
    if env is None:
        return None, None
    try:
        environment = environments.load(env, app.settings)
    except (ValidationError, SourceError) as error:
        raise unreadable(error, app.settings) from None
    return environment.settings, environment.name


def _render(args: RenderArgs) -> int:
    from basis.dash import render  # noqa: PLC0415 -- Dash's app and FastAPI's test client load when the verb runs

    found = load(args.app)
    settings, environment = _settings_of(found, args.env)
    rendered = render.render(found, args.page, render.parsed(args.sets, "--set"), settings=settings, environment=environment)
    sys.stdout.write(json.dumps(rendered.shown, indent=2, ensure_ascii=False, default=str) + "\n")
    for output, why in rendered.failed:
        fail(f"callback {output} failed on page {rendered.shown['page']}", why)
    return 1 if rendered.failed else 0


def _load(args: LoadArgs) -> int:
    from basis.dash import render  # noqa: PLC0415 -- Dash's app and FastAPI's test client load when the verb runs

    found = load(args.app)
    settings, environment = _settings_of(found, args.env)
    shown = render.load(found, args.dataset, _pairs(args.params), settings=settings, environment=environment)
    sys.stdout.write(json.dumps(shown, indent=2, ensure_ascii=False, default=str) + "\n")
    return 0


def _pairs(given: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for one in given:
        name, equals, value = one.partition("=")
        if not equals or not name:
            msg = f"--param {one} names no value: give --param NAME=VALUE"
            raise BasisError(msg)
        found[name] = value
    return found
