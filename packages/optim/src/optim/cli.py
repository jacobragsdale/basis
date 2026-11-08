"""optim's part of the `basis` command, which its `basis.verbs` entry point names, and the `optim` command, which is
`basis` itself under the name quants know, except that `optim new repo` makes a model repo wherever it runs. An analyst
runs a model's `main.py` instead, which calls the same code (`optim.api`). Settings come from `BASIS_*` environment
variables; choose the settings file explicitly:

uv run --env-file .env basis run income --params models/income/params/default.json
uv run --env-file .env basis run income --params models/income/params/default.json --portfolios P1 P2 --as-of 2026-10-16
uv run --env-file .env basis run income --params models/income/params/default.json --data models/income/tests/fixture
uv run --env-file .env.prod basis run income --params models/income/params/default.json --publish
uv run --env-file .env.prod basis resume income RUN_ID
uv run --env-file .env.prod basis ddl income --dialect mssql
uv run --env-file .env basis replay RUN_ID --params models/income/params/wider.json --portfolios P1
uv run --env-file .env basis sweep RUN_ID models/income/params/sweep --out sweep.csv
uv run --env-file .env basis regress income --snapshots //share/optim/snapshots --label purpose=production
uv run --env-file .env basis warm income
uv run basis describe income
uvx --index INDEX_URL optim new repo sma-optimizer income --index INDEX_URL
uv run basis new model ladder

`run`, `describe`, `resume`, `replay`, `regress` and `ddl` are the verbs core shares with jobs: core parses the model's
NAME and the RUN_ID, and these add the rest. `sweep` and `warm` are optim's own. Each handler reads its arguments as a
frozen dataclass. `run` exits 0 only when every portfolio was accepted and its publish, if asked for, committed; with
`--publish`, it and `resume` are the only things that write orders. `regress` exits 1 when anything changed. A failure
prints one `ERROR` line after the logs; results are on stdout."""

import argparse
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from basis.core import command, environments, logs
from basis.core.ddl import create_table, ledger
from basis.core.destination import Dialect
from basis.core.errors import BasisError, deliberate
from basis.core.publish import refused_overlap
from basis.core.snapshots import PublishRecord
from basis.core.storage import Root
from basis.core.verbs import Part, Runs, Shared, Verb, fail
from optim import models, new, research, snapshots, sync
from optim.context import open_context, open_orders, orders_settings
from optim.contract import LISTING, declared
from optim.orders import Table
from optim.runs import Run, fixture_date, published, read_snapshot, replay_run, resume_run, run_model, today
from optim.settings import Settings, refuse_renamed
from optim.snapshots import Saved

log = logging.getLogger(__name__)

DOCS = Path(__file__).parent / "docs"
"""The docs for quants, which ship with the package: `basis docs` lists them as `optim/PAGE`."""


@dataclass(frozen=True)
class RunArgs:
    model: str
    params: Path
    portfolios: tuple[str, ...] | None
    as_of: date | None
    data: Path | None
    labels: dict[str, str]
    workers: int | None
    cache: bool
    publish: bool
    again: bool
    """Publish even though another run of the model published orders for the as-of date."""
    env: str | None = None
    """The environment `--env` names, or None for `BASIS_ENV`'s."""


@dataclass(frozen=True)
class ResumeArgs:
    model: str | None
    run: str
    """A run's id, or the path or URL of its manifest."""
    again: bool
    env: str | None = None


@dataclass(frozen=True)
class DdlArgs:
    model: str | None
    ledger: str | None
    dialect: Dialect


@dataclass(frozen=True)
class ReplayArgs:
    model: str | None
    run: str
    """A run's id, or the path or URL of its manifest."""
    params: Path | None
    portfolios: tuple[str, ...] | None
    labels: dict[str, str]
    workers: int | None
    env: str | None = None


@dataclass(frozen=True)
class SweepArgs:
    run: str
    variants: Path
    out: Path | None
    workers: int


@dataclass(frozen=True)
class RegressArgs:
    model: str | None
    """The model whose runs to replay, or None for every model the repo lists."""
    snapshots: str | None
    last: int
    labels: dict[str, str]
    out: Path | None
    workers: int
    env: str | None = None


@dataclass(frozen=True)
class WarmArgs:
    models: tuple[str, ...]
    as_of: date | None


@dataclass(frozen=True)
class NewRepoArgs:
    name: str
    model: str
    directory: Path
    index: str | None
    framework_path: Path | None


def part() -> Part:
    """optim's side of the verbs core shares with jobs, its own verbs, and what core's verbs do with its models:
    `basis new model`, its docs and its agent files."""
    return Part(
        verbs=(
            Verb("sweep", "decide a saved run once per params file in a folder, into one comparison table; reads only the run", _sweep_arguments, _optim(_sweep_from)),
            Verb("warm", "fill the cache with every cached dataset the models read, before the day's first run", _warm_arguments, _optim(_warm_from)),
        ),
        files=sync.agent_files,
        docs=DOCS,
        new={"model": new.model},
        runs=Runs(
            "model",
            models.installed,
            run=Shared("decide portfolios with a model, save the run, and with --publish write its orders", _optim(_run_from), _run_arguments),
            describe=Shared("print a model's params schema, datasets and metrics as JSON, or every model's name", _describe_from),
            resume=Shared("publish a run's orders that it did not commit, from the run", _optim(_resume_from), _resume_arguments),
            replay=Shared("decide a saved run again with today's code, and other params if given; reads only the run", _optim(_replay_from), _replay_arguments),
            regress=Shared("replay a model's recent runs with today's code, and list every trade that changed", _optim(_regress_from), _regress_arguments),
            ddl=Shared("print the CREATE TABLE for the orders table the settings name, or for the ledger", _ddl_from, _ddl_arguments),
        ),
        renamed=refuse_renamed,
    )


def main() -> int:
    """The `optim` console script: `optim new repo NAME MODEL` makes a model repo wherever it runs, as `uvx optim new
    repo` does from the feed; anything else is `basis`, in the repo it runs in."""
    if sys.argv[1:3] == ["new", "repo"]:
        return scaffold(sys.argv[1:])
    return command.main()


def scaffold(argv: Sequence[str]) -> int:
    """`optim new repo NAME MODEL`, which runs before a repo exists."""
    return command.standalone("optim", "Make a model repo.", [Verb("new", "make a model repo: optim new repo NAME MODEL", _new_arguments, _new_from)], argv)


def _optim(handle: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    """`handle`, logging optim's records as basis's are, and a scheduler's SIGTERM before dying by it."""

    def handled(namespace: argparse.Namespace) -> int:
        logs.include("optim")
        signal.signal(signal.SIGTERM, _stopped)
        return handle(namespace)

    return handled


def _stopped(signum: int, _frame: object) -> None:
    """A scheduler stops a run with SIGTERM, on a deadline or a cancellation: log it, then die by it as usual."""
    log.error("stopped by %s before finishing", signal.Signals(signum).name)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--params", type=command.existing_file, required=True, help="the params JSON file")
    parser.add_argument("--portfolios", nargs="+", help="portfolio ids, or @FILE with one per line (default: every portfolio the model lists, or with --data, every one in its holdings)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="the business date (default: the date in --data's fixture.json, where it has one, or today in New York)")
    parser.add_argument("--data", type=command.existing_folder, help="read each dataset that has a file here, NAME.parquet or NAME.csv, instead of its source")
    _deciding(parser)
    parser.add_argument("--no-cache", action="store_true", help="read every dataset live")
    parser.add_argument("--publish", action="store_true", help="write the accepted portfolios' orders to the orders table BASIS_OPTIM__ORDERS__* configures")
    parser.add_argument("--again", action="store_true", help="with --publish: publish even though another run of the model published orders for the as-of date")


def _deciding(parser: argparse.ArgumentParser) -> None:
    """The options of a verb that decides portfolios and saves a run."""
    parser.add_argument("--label", type=command.label, action="append", default=[], dest="labels", metavar="KEY=VALUE", help="recorded in the run's manifest, such as purpose=production; repeatable")
    parser.add_argument("--workers", type=command.positive, help="worker processes (default: BASIS_OPTIM__WORKERS, or 1)")


def _run_from(n: argparse.Namespace) -> int:
    return _run(RunArgs(n.name, n.params, _portfolios(n.portfolios), n.as_of, n.data, dict(n.labels), n.workers, cache=not n.no_cache, publish=n.publish, again=n.again, env=n.env))


def _run(args: RunArgs) -> int:
    """With `--publish`, one of the two things that write orders, to the table the environment configures. Without it,
    where a table is configured, a line says that no orders were written, so a scheduled run that leaves `--publish`
    out cannot go unnoticed."""
    if args.again and not args.publish:
        return fail("--again publishes a run's orders even though another run published the date's, and the run was not given --publish", "add --publish, or leave --again out")
    environment = environments.load(args.env, Settings)
    if args.publish:
        environment.refuse_writes(f"basis run {args.model} --publish")
    settings, model = environment.settings, _model(args.model)
    with open_context(settings, cache=args.cache, publish=args.publish) as ctx:
        as_of_date, workers = args.as_of or fixture_date(args.data) or today(), args.workers or settings.optim.workers
        ran = run_model(
            ctx.loader,
            settings.snapshots(),
            model,
            args.params,
            as_of_date=as_of_date,
            portfolio_ids=args.portfolios,
            data_dir=args.data,
            workers=workers,
            labels=args.labels,
            orders=ctx.orders,
            again=args.again,
        )
    code = _report(ran, model.name, again=args.again)
    if not args.publish and (configured := settings.optim.orders) is not None:
        sys.stdout.write(f"orders: none written to {configured.table}, since the run was not given --publish: run it with --publish to write them\n")
    return code


def _resume_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--again", action="store_true", help="publish even though another run of the model published orders for its as-of date, as a run given --again does")


def _resume_from(n: argparse.Namespace) -> int:
    return _resume(ResumeArgs(n.name, n.run_id, again=n.again, env=n.env))


def _resume(args: ResumeArgs) -> int:
    """The other: a run's orders from the run, to the table it publishes to."""
    environment = environments.load(args.env, Settings)
    environment.refuse_writes(f"basis resume {args.run}")
    settings = environment.settings
    saved = _snapshot(args.run, args.model, settings)
    resumed = resume_run(saved, open_orders(settings), again=args.again)
    sys.stdout.write(published(resumed.records, earlier=resumed.earlier) or "orders: none to publish\n")
    if all(record.status == "committed" for record in resumed.records.values()):
        return 0
    told = args.again or (saved.info.publishing is not None and saved.info.publishing.again)
    command = _resume_command(saved.info.model.name, saved.info.run_id, resumed.records, again=told)
    return fail(f"run {saved.info.run_id}'s orders are still not published", f"fix what the orders line above says, then `{command}` again")


def _ddl_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", metavar="TABLE", help="print the ledger's CREATE TABLE instead, for TABLE, as BASIS_DATABASES__NAME__WRITE__LEDGER names it")
    parser.add_argument("--dialect", choices=["mssql", "oracle"], required=True)


def _ddl_from(n: argparse.Namespace) -> int:
    return _ddl(DdlArgs(n.name, n.ledger, "mssql" if n.dialect == "mssql" else "oracle"))


def _ddl(args: DdlArgs) -> int:
    """The orders table, as the environment names it, or the ledger, for whoever may create them. A model is found by
    its name or module path among those the environment lists, and its code is not imported: every model publishes to
    the one table."""
    if args.ledger is not None:
        sys.stdout.write(ledger(args.ledger, args.dialect))
        return 0
    if args.model is None:
        return fail("basis ddl prints the orders table or the ledger, and was given neither", "give a model, as `basis ddl income --dialect mssql`, or --ledger TABLE")
    listed = models.installed()
    if args.model not in listed and args.model not in listed.values():
        return fail(f"no model {args.model!r} is an entry point in {models.GROUP}", f"give one of {sorted(listed)}, or its module path")
    configured = orders_settings(Settings())
    sys.stdout.write(create_table(Table(configured.database, configured.table), args.dialect))
    return 0


def _replay_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--params", type=command.existing_file, help="a params JSON file (default: the params the run used)")
    parser.add_argument("--portfolios", nargs="+", help="only these portfolios, or @FILE with one per line (default: every portfolio in the run)")
    _deciding(parser)


def _replay_from(n: argparse.Namespace) -> int:
    return _replay(ReplayArgs(n.name, n.run_id, n.params, _portfolios(n.portfolios), dict(n.labels), n.workers, n.env))


def _replay(args: ReplayArgs) -> int:
    """Reads a run and saves one: no sources or cache."""
    settings = environments.load(args.env, Settings).settings
    workers = args.workers or settings.optim.workers
    saved = _snapshot(args.run, args.model, settings)
    ran = replay_run(saved, settings.snapshots(), params_file=args.params, portfolio_ids=args.portfolios, workers=workers, labels=args.labels)
    return _report(ran, saved.info.model.name)


def _sweep_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run", metavar="RUN", help="a run's id under BASIS_SNAPSHOT_ROOT, or the path or URL of its manifest")
    parser.add_argument("variants", metavar="VARIANTS", type=command.existing_folder, help="a folder of params JSON files, one per variant")
    _tabled(parser)


def _tabled(parser: argparse.ArgumentParser) -> None:
    """The options of a verb whose result is a table."""
    parser.add_argument("--out", type=Path, help="write the table to this .csv or .parquet file (default: CSV on stdout)")
    parser.add_argument("--workers", type=command.positive, default=1, help="worker processes (default: 1)")


def _sweep_from(n: argparse.Namespace) -> int:
    return _sweep(SweepArgs(n.run, n.variants, n.out, n.workers))


def _sweep(args: SweepArgs) -> int:
    """Reads a run alone, and writes only the table."""
    if not (variants := sorted(args.variants.glob("*.json"))):
        return fail(f"{args.variants} has no params files (*.json) to sweep", "add one per variant, such as a copy of the model's params/default.json with a value changed")
    _write(research.sweep(read_snapshot(_snapshot(args.run, None, Settings())), variants, workers=args.workers), args.out)
    return 0


def _regress_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshots", help="the run store to read, such as production's (default: the environment's BASIS_SNAPSHOT_ROOT)")
    parser.add_argument("--last", type=command.positive, default=20, help="how many of each model's most recent runs (default: 20)")
    parser.add_argument("--label", type=command.label, action="append", default=[], dest="labels", metavar="KEY=VALUE", help="only runs labelled so, such as purpose=production; repeatable")
    _tabled(parser)


def _regress_from(n: argparse.Namespace) -> int:
    return _regress(RegressArgs(n.name, n.snapshots, n.last, dict(n.labels), n.out, n.workers, n.env))


def _regress(args: RegressArgs) -> int:
    """A row per change, for the model named, as a table, CSV on stdout or `--out`; or for every model the repo
    lists, with a `model` column, as JSON on stdout, as every part's `regress` without a name prints, or `--out`.
    Exits 1 when anything changed."""
    store = Root.at(args.snapshots) if args.snapshots is not None else environments.load(args.env, Settings).settings.snapshots()
    if args.model is not None:
        changes = research.regress(_model(args.model), store, last=args.last, labels=args.labels, workers=args.workers)
        _write(changes, args.out)
    else:
        found = [research.regress(_model(name), store, last=args.last, labels=args.labels, workers=args.workers).assign(model=name) for name in sorted(models.installed())]
        changes = pd.concat(found, ignore_index=True)[["model", *research.CHANGES]] if found else pd.DataFrame(columns=pd.Index(["model", *research.CHANGES]))
        if args.out is not None:
            _write(changes, args.out)
        else:
            sys.stdout.write(json.dumps(json.loads(changes.to_json(orient="records")), indent=2) + "\n")
    if len(changes):
        where = args.out if args.out is not None else "stdout"
        fix = f"{where} lists each; fix the code, or say in the pull request why the change is meant"
        return fail(f"{len(changes)} portfolios or trades changed when the runs were replayed with today's code", fix)
    return 0


def _describe_from(n: argparse.Namespace) -> int:
    sys.stdout.write(json.dumps(research.describe(_model(n.name)), indent=2) + "\n")
    return 0


def _warm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("models", metavar="MODEL", nargs="+", help="the models' names, or module paths")
    parser.add_argument("--as-of", type=date.fromisoformat, help="the business date (default: today in New York)")


def _warm_from(n: argparse.Namespace) -> int:
    return _warm(WarmArgs(tuple(n.models), n.as_of))


def _warm(args: WarmArgs) -> int:
    settings = Settings()
    if settings.optim.cache_root is None:
        return fail("warm fills the cache, and no cache is configured", "set BASIS_OPTIM__CACHE_ROOT, as .env.example shows")
    read_whole = (dataset for name in args.models for attribute, dataset in declared(_model(name).data).items() if attribute == LISTING or not dataset.per_portfolio)
    datasets = {dataset.name: dataset for dataset in read_whole}
    with open_context(settings) as ctx:
        if ctx.loader.warm(datasets.values(), args.as_of or today()):
            return 0
    return fail("the cache is not warm: a dataset did not load, as the log records above say", "fix what they say, and warm it again")


def _new_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("what", metavar="repo", choices=["repo"], help="what to make: a repo")
    parser.add_argument("name", metavar="NAME", help="the repo's folder")
    parser.add_argument("model", metavar="MODEL", help="its first model, a copy of the example, which names its folder in models/ and its entry point")
    parser.add_argument("--dir", type=Path, default=Path(), help="where to make the repo")
    parser.add_argument("--index", help="a package index that proxies PyPI and holds optim, which no basis release publishes yet; written into the repo")
    parser.add_argument("--framework-path", type=Path, help="a checkout of basis, whose packages the repo installs editable")


def _new_from(n: argparse.Namespace) -> int:
    args = NewRepoArgs(n.name, n.model, n.dir, n.index, n.framework_path)
    logs.include("optim")
    path = new.repo(args.directory / args.name, args.model, index=args.index, framework_path=args.framework_path)
    log.info("made %s: run `uv run basis check` in it", path)
    return 0


def _model(name: str) -> models.LoadedModel:
    """The model `name` names, a name the repo lists or a module path."""
    with deliberate(LookupError):  # no model by that name or module path
        return models.load(name)


def _snapshot(run: str, model: str | None, settings: Settings) -> Saved:
    """A run by the path or URL of its manifest, or by its id under the `BASIS_SNAPSHOT_ROOT` of `settings`: a run of
    `model`, where it is given."""
    with deliberate(LookupError):  # no such run, or a manifest that is not optim's
        saved = snapshots.read(Root.at(run)) if "/" in run or "\\" in run else snapshots.find([settings.snapshots()], run)
    if model is not None and model != saved.info.model.name and models.installed().get(model, model) not in {saved.info.model.name, saved.info.model.module}:
        msg = f"run {saved.info.run_id} is a run of {saved.info.model.name}, not {model}: give {saved.info.model.name}, or leave the model out"
        raise BasisError(msg)
    return saved


def _portfolios(given: Sequence[str] | None) -> tuple[str, ...] | None:
    """Portfolio ids as given, each `@FILE` replaced by the ids in that file, one per line."""
    if given is None:
        return None
    ids: list[str] = []
    for value in given:
        if value.startswith("@"):
            with deliberate(OSError):  # a file that is not there, or cannot be read
                ids += [line.strip() for line in Path(value[1:]).read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            ids.append(value)
    return tuple(ids)


def _write(table: pd.DataFrame, out: Path | None) -> None:
    """A table as CSV on stdout, or to `out` as Parquet or CSV by its suffix."""
    if out is None:
        sys.stdout.write(table.to_csv(index=False))
    elif out.suffix == ".parquet":
        table.to_parquet(out, index=False)
    else:
        table.to_csv(out, index=False)


def _report(ran: Run, model: str, *, again: bool = False) -> int:
    """`ran`'s summary, and a failure, naming `model`, as the run named it, if a portfolio was rejected or its orders'
    publish did not commit."""
    sys.stdout.write(ran.summary())
    code = 0
    if not ran.ok:
        rejected = sum(not outcome.accepted for outcome in ran.outcomes)
        fix = f"fix what each says; `basis replay {model} {ran.run_id}` decides them again on what the run read"
        code = fail(f"run {ran.run_id} rejected {rejected} of its {len(ran.outcomes)} portfolios, for the reasons above", fix)
    if not ran.committed:
        fix = f"fix what the orders line above says, then `{_resume_command(model, ran.run_id, ran.publishes, again=again)}`"
        code = fail(f"run {ran.run_id} saved its orders and did not publish them", fix)
    return code


def _resume_command(model: str, run_id: str, publishes: Mapping[str, PublishRecord], *, again: bool) -> str:
    """The command that finishes a run's publish: told `--again` as the run was, or where the database refused it for
    an as-of date its ledger has, since the next attempt needs it then."""
    return f"basis resume {model} {run_id}{' --again' if again or refused_overlap(publishes) else ''}"
