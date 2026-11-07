"""optim's pytest plugin, which registers itself when `optim` is installed: the e2e suite's `optim_qa` fixture and its
safeguards. A model's contract tests are not here: they are `optim.testing.ContractTests`, which each model's
`tests/test_contract.py` subclasses, so they appear in the IDE like any test.

For the repo's e2e suite, marked `e2e`, the `optim_qa` fixture runs `basis run` against the QA environment. It refuses
to run unless every configured source and destination carries a QA marker (`optim_qa_markers`, default `qa`) and every
write goes to the allowlist (`optim_e2e_writes`: the QA snapshot root and orders table, and for a run given `--publish`,
the orders database's ledger, which a publish writes). `QA.listed` picks a model's
first portfolios from its `portfolios` dataset, and `QA.portfolios` picks portfolios by what they hold, with a
read-only query, for a model that lists none."""

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from basis.core.databases.mssql import target_of
from basis.core.settings import MssqlSettings
from optim import models, snapshots
from optim.context import open_context
from optim.contract import LISTING, declared
from optim.runs import today
from optim.settings import Settings, refuse_renamed
from optim.snapshots import Saved
from optim.sources import Param, SourceName


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("optim_qa_markers", "text every QA source and destination contains, such as qa", type="linelist", default=["qa"])
    parser.addini("optim_e2e_writes", "the only snapshot roots, cache roots, orders tables and ledgers the e2e suite may write to", type="linelist", default=[])
    parser.addini("optim_e2e_command", "the command that runs the deployed optim (default: this interpreter's)", type="args", default=[sys.executable, "-m", "optim"])


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: runs the deployed optim against the QA environment; refuses to run anywhere else")


def qa_problems(settings: Settings, *, markers: Sequence[str], writes: Sequence[str], publishing: bool = False) -> list[str]:
    """Why the environment is not safe for the e2e suite: a configured source or destination without a QA marker, or
    a write outside the allowlist. None when it is. Every database the settings configure is checked, read and write
    logins both, whether or not a model reads it; the orders database's ledger only when `publishing`, for a run given
    `--publish`, which writes it."""
    named = {"BASIS_SNAPSHOT_ROOT": settings.snapshot_root or ""}
    named |= {"BASIS_OPTIM__CACHE_ROOT": settings.optim.cache_root} if settings.optim.cache_root is not None else {}
    for name, database in settings.databases.items():
        block = f"BASIS_DATABASES__{name.upper()}__"
        if isinstance(database, MssqlSettings):
            named[f"{block}CONNECTION_STRING"] = target_of(database.connection_string.get_secret_value())
            if database.write is not None:
                named[f"{block}WRITE__CONNECTION_STRING"] = target_of(database.write.connection_string.get_secret_value())
        else:
            named[f"{block}DSN"] = database.dsn
    named |= {"BASIS_OPTIM__REST__URL": str(settings.optim.rest.url)} if settings.optim.rest is not None else {}
    writes_to = ["BASIS_SNAPSHOT_ROOT", "BASIS_OPTIM__CACHE_ROOT", "BASIS_OPTIM__ORDERS__TABLE"]
    if (orders := settings.optim.orders) is not None:
        named["BASIS_OPTIM__ORDERS__TABLE"] = orders.table
        if publishing and (write := settings.database(orders.database).write) is not None and write.ledger is not None:
            writes_to.append(ledger := f"BASIS_DATABASES__{orders.database.upper()}__WRITE__LEDGER")
            named[ledger] = write.ledger
    problems = [f"{name} ({value}) contains none of the QA markers {list(markers)}" for name, value in named.items() if not any(m.lower() in value.lower() for m in markers)]
    written = {name: value for name, value in named.items() if name in writes_to}
    problems += [f"{name} ({value}) is not in optim_e2e_writes {list(writes)}" for name, value in written.items() if value not in writes]
    return problems


_READ_ONLY = re.compile(r"^\s*(--[^\n]*\n\s*|/\*.*?\*/\s*)*(SELECT|WITH)\b", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class E2ERun:
    run_id: str
    exit_code: int
    snapshot: Saved


@dataclass(frozen=True)
class QA:
    """The QA environment, checked safe: what the e2e suite runs `optim` against."""

    settings: Settings
    command: Sequence[str]
    root: Path
    """The repo, where the command runs, so params paths relative to it resolve in an image whose workdir holds it."""
    markers: Sequence[str] = ("qa",)
    writes: Sequence[str] = ()
    """The allowlist, which a run given `--publish` is checked against again, with what a publish writes."""

    def listed(self, model: str, count: int = 3, *, as_of_date: date | None = None) -> list[str]:
        """The first `count` portfolios `model` lists in its `portfolios` dataset on `as_of_date`, today unless given, as
        the run the smoke case starts takes it, read from QA in a read-only transaction."""
        if (listing := declared(models.load(model).data).get(LISTING)) is None:
            msg = f"{model} does not list its portfolios in a `portfolios` dataset: pick them with QA.portfolios and a SELECT"
            raise LookupError(msg)
        with open_context(self.settings, cache=False) as ctx, ctx.loader.read_only(listing.source) as scoped:
            frame = scoped.load(listing, as_of_date=as_of_date or today())
        return list(dict.fromkeys(str(portfolio_id) for portfolio_id in frame["portfolio_id"]))[:count]

    def portfolios(self, query: Path, *, source: SourceName = "mssql", **params: Param) -> list[str]:
        """Portfolio ids, picked by what they hold with a SELECT in a SQL file that returns `portfolio_id`, read in a
        read-only transaction and rolled back, rather than by fixed ids that drift."""
        if not _READ_ONLY.match(query.read_text(encoding="utf-8")):
            msg = f"{query} is not a SELECT: the e2e suite only reads"
            raise ValueError(msg)
        with open_context(self.settings, cache=False) as ctx, ctx.loader.read_only(source) as scoped:
            columns = scoped.sources[source].fetch(str(query), params)
        return list(dict.fromkeys(str(portfolio_id) for portfolio_id in columns["portfolio_id"]))

    def run(self, model: str, params: Path, portfolios: Sequence[str], *args: str) -> E2ERun:
        """`basis run` as the deployed image runs it, labelled `purpose=e2e`, from the repo: `params` is best relative
        to it, as `models/MODEL/params/default.json`."""
        if "--publish" in args and (problems := qa_problems(self.settings, markers=self.markers, writes=self.writes, publishing=True)):
            pytest.fail("refusing to publish outside QA:\n" + "\n".join(problems), pytrace=False)
        command = [*self.command, "run", model, "--params", params.as_posix(), "--portfolios", *portfolios, "--label", "purpose=e2e", *args]
        ran = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=False)  # noqa: S603 -- the configured command
        if not (found := re.search(r"^run (\S+)$", ran.stdout, re.MULTILINE)):
            msg = f"basis run did not complete (exit {ran.returncode}):\n{ran.stderr[-4000:]}"
            raise AssertionError(msg)
        return E2ERun(found.group(1), ran.returncode, snapshots.find([self.settings.snapshots()], found.group(1)))

    def replay(self, run: E2ERun) -> E2ERun:
        ran = subprocess.run([*self.command, "replay", run.run_id, "--label", "purpose=e2e"], cwd=self.root, capture_output=True, text=True, check=False)  # noqa: S603 -- the configured command
        if not (found := re.search(r"^run (\S+)$", ran.stdout, re.MULTILINE)):
            msg = f"basis replay did not complete (exit {ran.returncode}):\n{ran.stderr[-4000:]}"
            raise AssertionError(msg)
        return E2ERun(found.group(1), ran.returncode, snapshots.find([self.settings.snapshots()], found.group(1)))

    def smoke(self, model: str, params: Path, portfolios: Sequence[str], *args: str) -> E2ERun:
        """Every model's smoke case: the run completes, each portfolio is accepted or rejected with reasons, and its
        replay gives the same trades for the solves that did not stop at a time limit."""
        ran = self.run(model, params, portfolios, *args)
        results = ran.snapshot.results()
        decisions = results["decisions"]
        if set(decisions["portfolio_id"]) != set(portfolios):
            msg = f"the run decided {sorted(decisions['portfolio_id'])}, not {sorted(portfolios)}"
            raise AssertionError(msg)
        if silent := set(decisions.loc[decisions["status"] == "rejected", "portfolio_id"]) - set(results["reasons"]["portfolio_id"]):
            msg = f"rejected without reasons: {sorted(silent)}"
            raise AssertionError(msg)
        timed_out = set(decisions.loc[decisions["stop"] == "limit", "portfolio_id"])
        again = self.replay(ran).snapshot.results()["trades"]
        key = ["portfolio_id", "security_id", "side", "quantity"]
        before, after = (frame.loc[~frame["portfolio_id"].isin(timed_out), key].sort_values(key).reset_index(drop=True) for frame in (results["trades"], again))
        pd.testing.assert_frame_equal(before, after, obj="trades, run then replayed")
        return ran


@pytest.fixture(scope="session")
def optim_qa(pytestconfig: pytest.Config) -> QA:
    """The QA environment, from `BASIS_*` settings, or a failure that says why it is not safe to run against."""
    refuse_renamed(os.environ)
    settings = Settings()
    markers, writes = pytestconfig.getini("optim_qa_markers"), pytestconfig.getini("optim_e2e_writes")
    if problems := qa_problems(settings, markers=markers, writes=writes):
        pytest.fail("refusing to run the e2e suite outside QA:\n" + "\n".join(problems), pytrace=False)
    return QA(settings, pytestconfig.getini("optim_e2e_command"), pytestconfig.rootpath, markers, writes)
