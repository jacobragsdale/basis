"""basis's own surface, the names a repo and its stored runs rely on, as JSON: what `docs/surface/` keeps for each
release, and what `scripts/tests/test_surface.py` holds the code to.

    uv run python scripts/surface.py                  # the surface of the installed code, on stdout
    uv run python scripts/surface.py write VERSION    # docs/surface/VERSION/, as a release does

The surface is:

- `python`: each part's `__all__`, `basis.job`'s and `basis.api`'s, the names a repo imports;
- `verbs`: each part's verbs, its own and its side of those core shares, with each one's options;
- `settings`: every `BASIS_*` variable the settings read, with `NAME` where a repo names its own, as a database;
- `formats`: the fields of a run's manifest, of `basis check --full`'s record and of the ledger's row, which runs and
  checks of every release past are stored in.

`write` keeps the release's surface beside a corpus of what it stores, made by its own code, which every later
release must read: a run's store, the example job's run on its fixture; a `basis check --full` record; and the ledger's
`CREATE TABLE` in each dialect (`packages/job/tests/test_corpus.py`, `packages/core/tests/test_corpus.py`).

A name may leave the surface only after one release in which using it is refused with its replacement
(`RENAMED` in `scripts/tests/test_surface.py`, and `renamed` for a variable). It reads only what basis's first
release, 0.1.0, already had, so it runs on the code of any release since."""

import argparse
import importlib
import json
import sys
import types
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "docs" / "surface"
PARTS = ("job", "api")
SHARED = ("run", "describe", "resume", "replay", "regress", "runs", "ddl")


def surface() -> dict[str, Any]:
    return {"python": _python(), "verbs": _verbs(), "settings": _settings(), "formats": _formats()}


def _python() -> dict[str, list[str]]:
    return {name: sorted(importlib.import_module(name).__all__) for name in ("basis.job", "basis.api")}


def _options(arguments: Callable[[argparse.ArgumentParser], None]) -> list[str]:
    parser = argparse.ArgumentParser(add_help=False)
    arguments(parser)
    return sorted(option for action in parser._actions for option in action.option_strings)  # noqa: SLF001 -- argparse names no public list of a parser's options


def _verbs() -> dict[str, dict[str, list[str]]]:
    found: dict[str, dict[str, list[str]]] = {}
    for part in PARTS:
        made = importlib.import_module(f"basis.{part}.cli").part()
        verbs = {verb.name: _options(verb.arguments) for verb in made.verbs}
        runs = made.runs
        if runs is not None:
            given = getattr(runs, "verbs", None)
            sides = dict(given) if given is not None else {name: side for name in SHARED if (side := getattr(runs, name, None)) is not None}
            verbs |= {name: _options(side.arguments) for name, side in sides.items()}
        found[part] = dict(sorted(verbs.items()))
    command = importlib.import_module("basis.core.command")
    core = {verb: _options(arguments) for verb in command.CORE if verb != "new" and (arguments := getattr(command, f"_{verb}_arguments", None)) is not None}
    return {"core": dict(sorted(core.items())), **found}


def _model(annotation: object) -> type[BaseModel] | None:
    return annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None


def _variables(model: type[BaseModel], prefix: str) -> Iterator[str]:
    for name, field in model.model_fields.items():
        yield from _of(field.annotation, f"{prefix}{'_' if prefix == 'BASIS' else '__'}{name.upper()}")


def _of(annotation: object, at: str) -> Iterator[str]:
    if isinstance(annotation, typing.TypeAliasType):
        annotation = annotation.__value__
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin is typing.Annotated:
        yield from _of(arguments[0], at)
    elif origin in {typing.Union, types.UnionType}:
        models = [found for argument in arguments if (found := _model(argument)) is not None]
        if not models:
            yield at
        for model in models:
            yield from _variables(model, at)
    elif origin is dict and len(arguments) == 2:  # noqa: PLR2004 -- a mapping's key and value
        yield from _of(arguments[1], f"{at}__NAME")
    elif (model := _model(annotation)) is not None:
        yield from _variables(model, at)
    else:
        yield at


def _settings() -> list[str]:
    from basis.api.settings import Settings as ApiSettings  # noqa: PLC0415 -- each part's, as it is installed
    from basis.core.settings import Settings  # noqa: PLC0415

    return sorted({*_variables(Settings, "BASIS"), *_variables(ApiSettings, "BASIS")})


def _formats() -> dict[str, list[str]]:
    from basis.core import review  # noqa: PLC0415
    from basis.core.databases.base import LEDGER_COLUMNS  # noqa: PLC0415
    from basis.core.snapshots import Manifest  # noqa: PLC0415

    return {"manifest": sorted(Manifest.model_fields), "check": sorted(review.Record.model_fields), "ledger": list(LEDGER_COLUMNS)}


def corpus(folder: Path) -> None:
    """What this release stores, as its own code writes it, in `folder`."""
    from datetime import UTC, datetime  # noqa: PLC0415 -- the corpus's alone

    from basis.core import ddl, review  # noqa: PLC0415
    from basis.job import run  # noqa: PLC0415
    from basis.job.example import job  # noqa: PLC0415

    run(job, params="params/default.json", data="tests/fixture", snapshot_root=folder / "store")
    record = review.Record(
        commit="0" * 40, folder="", checked_at=datetime(2026, 11, 11, 12, tzinfo=UTC), checked=True, passed=True, versions=review.versions(),
        regress=review.RegressRecord(skipped="no part of this repo saves runs"), qa=review.QaRecord(skipped="the repo declares no QA tier"), contracts={"api": None},
    )  # fmt: skip
    (folder / "check.json").write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for manifest in (folder / "store").rglob("runs/**/*.json"):  # with the newline the repo's hooks give every text file
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")
    for dialect in ("mssql", "oracle"):
        (folder / f"ledger.{dialect}.sql").write_text(ddl.ledger("etl.publishes", dialect), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="basis's own surface, as JSON")
    commands = parser.add_subparsers(dest="command")
    write = commands.add_parser("write", help="write docs/surface/VERSION.json")
    write.add_argument("version")
    given = parser.parse_args()
    text = json.dumps(surface(), indent=2) + "\n"
    if given.command == "write":
        (FOLDER / given.version).mkdir(parents=True, exist_ok=True)
        (FOLDER / given.version / "surface.json").write_text(text, encoding="utf-8")
        corpus(FOLDER / given.version)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
