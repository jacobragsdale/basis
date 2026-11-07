"""What a basis part gives the `basis` command, which `basis.core.command` runs.

A part is a package that adds to the `basis` namespace, such as basis-job. It lists one entry point in the
`basis.verbs` group, named after the part, and a repo names the parts it has in its `pyproject.toml`:

    # the part's pyproject.toml
    [project.entry-points."basis.verbs"]
    job = "basis.job.cli:part"

    # a repo's pyproject.toml
    [tool.basis]
    parts = ["job"]

The entry point is a callable that takes nothing and returns the part's `Part`: its own verbs, and what core's verbs
need from it. `basis` loads only the parts its repo declares, and `basis --help` lists their verbs and each core verb
that one of them gives something to do:

| Core verb | Listed when a declared part has | What it does with them |
|---|---|---|
| `check` | always | lints, type checks and tests the repo, checks its clone runs its hooks, and compares the files `sync` owns with what basis and `files` give |
| `sync` | always | writes basis's own files and what every part's `files` give, and removes what basis wrote that none of them gives now |
| `docs [PAGE]` | `docs` | lists every part's pages as `PART/PAGE`, or prints one |
| `new KIND NAME` | `new` | calls `new[KIND](repo, NAME)` |
| `run`, `describe`, `resume`, `replay`, `regress`, `ddl` | that field of `runs` | parses NAME, and RUN_ID for `resume` and `replay`; NAME's part's `Shared` adds and reads the rest |
| `check --full` | always | `check`, then each part's `runs.regressions` where the run store is reachable, and its `qa` where the repo declares a QA tier and this machine has its settings |
| `review` | always | prints what `check --full` found for a commit, with each part's `contract` diff |
| `doctor`, `upgrade` | always | check the desktop's setup; relock basis, `sync` and `check --full` |

No two declared parts may add the same verb, nor a verb core has, nor the same kind to `new`. A verb that several
kinds of part share is core's, and reaches each part through a field of `Part`, as `runs` does for the verbs of every
part that runs things by name, such as jobs.

A shared verb reaches the only declared part that has it, whatever NAME is, so a module path names what it runs
there too. Where several do, NAME comes first, and is a name one of them lists in `Runs.names`; two of them listing the
same name is refused, with the fix. Without NAME, there, `describe` prints what every part runs; `regress` replays
every part's runs, taking only the options every part's takes, and prints one JSON object of what each part printed,
by part, so a part's `regress` without NAME prints JSON; `ddl` prints the ledger's CREATE TABLE itself, from
`--ledger TABLE --dialect DIALECT`, since every part's ledger is core's; and `run`, `resume` and `replay` are refused,
with the fix. `basis VERB NAME --help` gives the arguments of NAME's part.

`renamed` refuses, before any verb runs, the variables an earlier version of the part read under names it no longer
reads, as `basis` refuses its own `ETL_*`.

A handler reads its `argparse.Namespace` into a frozen dataclass before anything else reads it, and returns the exit
code: 0 when the verb did what it says, and 1 when it did not. `basis` logs `finished VERB`, with the exit code, after
it. A handler reports a failure the caller can fix as one line, with `fail`, which `basis` holds back until that
record so that it comes last, or by raising a `basis.core.errors.BasisError` whose message gives the reason and ends with the fix, which
`basis` prints as the line; a pydantic `ValidationError` of the caller's input prints as one line too. Any other
exception is a defect, and `basis` logs its traceback before its line; `basis.core.errors.deliberate` marks a builtin
error that a call raises on purpose, such as a lookup that finds no run. A handler that needs its repo reads its root
from the namespace, as `repo_root`, the folder of the `pyproject.toml` `basis` found, and never looks for it again.
A result that a caller reads, such as `describe`'s, is JSON on stdout, and logs are JSON lines on stderr."""

import argparse
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from basis.core.databases import Database
from basis.core.settings import Settings
from basis.core.storage import Root
from basis.core.sync import AgentFiles, Problem

GROUP = "basis.verbs"
"""The entry point group each part lists its `Part` in, by the part's name."""


@dataclass(frozen=True)
class Verb:
    name: str
    """What follows `basis`, such as `run`."""
    help: str
    """One line, which `basis --help` lists beside the name."""
    arguments: Callable[[argparse.ArgumentParser], None]
    """Adds the verb's arguments to its parser."""
    handle: Callable[[argparse.Namespace], int]
    """Runs the verb on its parsed arguments, and returns the exit code."""


def _no_arguments(_: argparse.ArgumentParser) -> None:
    """A shared verb's arguments, for a part that adds none of its own."""


@dataclass(frozen=True)
class Shared:
    """One part's side of a verb that core shares among the parts that run things by name, such as `run`."""

    help: str
    """What the verb does with what the part runs, in one line, such as `run a job, and with --publish write its
    changes to its databases`, which `basis --help` lists beside the verb."""
    handle: Callable[[argparse.Namespace], int]
    """Runs the verb on its parsed arguments, and returns the exit code. The namespace holds core's arguments, `name`
    (None where NAME was left out) and, for `resume` and `replay`, `run_id`, beside the part's own."""
    arguments: Callable[[argparse.ArgumentParser], None] = _no_arguments
    """Adds the part's own arguments to the verb's parser, after core's NAME and RUN_ID."""


@dataclass(frozen=True)
class Difference:
    """How one output of a replayed run differs from what the run computed."""

    output: str
    summary: str
    """In a few words, as `basis review` lists it, such as `upsert: 2 rows added, 1 changed (price 1)`."""
    detail: Mapping[str, object]
    """Everything the part knows of it, which `basis regress` prints as JSON."""


@dataclass(frozen=True)
class Regressed:
    """One published run that `regress` replayed with today's code."""

    name: str
    """What the part runs, by name, such as a job, whose run it is."""
    run_id: str
    reasons: tuple[str, ...]
    """Every reason the replay would not write."""
    differences: tuple[Difference, ...]

    @property
    def changed(self) -> bool:
        return bool(self.reasons or self.differences)


@dataclass(frozen=True)
class Regressions:
    """What `runs.regressions` found: each run replayed, and each failure to replay one name's runs, which `basis` prints
    as its one line while the other names still replay."""

    replayed: tuple[Regressed, ...] = ()
    failures: tuple[Exception, ...] = ()


@dataclass(frozen=True)
class Regress:
    """Which published runs `Runs.regressions` replays, as `basis check --full` asks for them."""

    name: str | None
    """What the part runs whose runs to replay, by name, or None for every one the part has."""
    last: int
    """How many published runs of each, newest first."""
    labels: dict[str, str]
    """Labels a run must have, all of them, to be replayed."""
    snapshots: str | None
    """The snapshot root to read, instead of the one the settings name."""


@dataclass(frozen=True)
class Runs:
    """What a part runs by name, such as a repo's jobs, and its side of each verb core shares among the parts that run
    things by name; a verb left None is one the part does not have."""

    noun: str
    """What the part runs, in the singular, such as `job`, as `basis --help` and its errors name one."""
    names: Callable[[], Mapping[str, str]]
    """Each one the part runs, by name, with its module path: how `basis` finds the part a NAME belongs to, when
    more than one declared part runs things, and what `basis describe` prints without NAME."""
    run: Shared | None = None
    """`basis run NAME`, with the part's options, such as a job's `--interval`."""
    describe: Shared | None = None
    """`basis describe NAME`, which prints NAME's params, inputs and outputs as JSON; it is never handed None."""
    resume: Shared | None = None
    """`basis resume [NAME] RUN_ID`, which finishes publishing a run that failed partway."""
    replay: Shared | None = None
    """`basis replay [NAME] RUN_ID`, which reruns a saved run with today's code."""
    regress: Shared | None = None
    """`basis regress [NAME]`, which replays recent published runs and lists what changed."""
    ddl: Shared | None = None
    """`basis ddl [NAME]`, which prints the CREATE TABLE statements for NAME's tables, or without NAME, the ledger's."""
    regressions: Callable[[Regress], Regressions] | None = None
    """What `basis check --full` replays, as data it records for `basis review`: the runs `Regress` chooses, from
    `Regress.snapshots`, writing nothing. `check --full` skips a part that has `regress` but not this, with a
    `SKIPPED:` line."""


@dataclass(frozen=True)
class Qa:
    """What the QA tier gives a part's QA runs, once it has checked that every place `settings` points carries the QA
    marker: see `basis.core.qa`."""

    repo: Path
    commit: str | None
    """The commit being checked, which each run records, or None before the first."""
    settings: Settings
    """Read from the QA settings file alone, never the process's environment."""
    store: Root
    """The QA store, where each run saves its snapshot: one of `[tool.basis.qa] stores`."""
    writes: Mapping[str, frozenset[str]]
    """The tables the tier may write, by database, each in lower case: `[tool.basis.qa] writes`."""
    declared: Mapping[str, object]
    """The part's own table, such as `[tool.basis.qa.job]`, as TOML reads it, or empty when the repo gives none: the
    part validates it."""
    databases: Callable[[str], Database]
    """Each database `settings` configures, opened so that a write to a table outside `writes` raises before it
    writes anything."""

    def may_write(self, database: str, table: str) -> bool:
        """Whether `writes` lists `table` in `database`: as the part names it, ignoring case, which neither database
        gives an unquoted name."""
        return table.lower() in self.writes.get(database, frozenset())


@dataclass(frozen=True)
class QaRun:
    """One run the QA tier made."""

    name: str
    """What it ran, by name, such as a job."""
    run_id: str | None
    """The run's id, or None for one that never started."""
    summary: str
    """What it did, in a few words, as `basis review` lists it, such as `published: daily_positions 3 rows`."""
    failure: Problem | None = None
    """Why it did not do what the tier asked, and the fix, or None when it did."""


@dataclass(frozen=True)
class QaPlan:
    """A part's QA runs, worked out before any part's run."""

    problems: Sequence[Problem]
    """Each reason a run would be unsafe or could not run, such as a table it writes that `writes` does not list: while
    any part has one, the tier runs nothing."""
    run: Callable[[], Sequence[QaRun]]
    """Makes the runs, each against QA, publishing only to the tables `writes` lists; one that fails leaves the rest to
    run."""


@dataclass(frozen=True)
class Part:
    """What one part adds to `basis`: its own verbs, and what core's verbs do with it."""

    verbs: tuple[Verb, ...] = ()
    """The part's own verbs, such as basis-api's `serve`."""
    files: Callable[[], AgentFiles] | None = None
    """The part's block of `AGENTS.md`, its skills and its lines of the Dockerfile, as the installed version writes
    them; read only by the verbs that use them."""
    docs: Path | None = None
    """A folder of Markdown pages, each `PAGE.md`, for the installed version."""
    new: Mapping[str, Callable[[Path, str], Path]] = field(default_factory=dict[str, Callable[[Path, str], Path]])
    """What `basis new KIND NAME` adds, by KIND: each makes NAME in the repo at the path it is given, and returns
    what it made."""
    runs: Runs | None = None
    """What the part runs by name, and its side of the verbs core shares among the parts that do."""
    renamed: Callable[[Mapping[str, str]], None] | None = None
    """Refuses an environment that sets a variable an earlier version of the part read, under a name it no longer
    reads, by raising a `basis.core.errors.BasisError` that names its replacement."""
    qa: Callable[[Qa], QaPlan] | None = None
    """The part's runs against QA, which `basis check --full` makes where the repo declares `[tool.basis.qa]` and this
    machine has its settings file, such as each job's, published to QA's tables."""
    contract: Callable[[Path], str | None] | None = None
    """How the contract the part's code offers others, such as an API's `openapi.json`, differs from the last
    release's, as Markdown, or None when it does not; called on the repo `basis check --full` checks, and printed by
    `basis review`. A job offers none."""


_held: ContextVar[list[str] | None] = ContextVar("basis.core.verbs.held", default=None)
"""The ERROR lines printed while a verb runs, which `basis` prints after the verb's last log record; None outside one."""


def fail(reason: str, fix: str) -> int:
    """Print a failure as `basis` prints every one, `ERROR: REASON: FIX` on one line of stderr, and return 1."""
    error_line(f"{reason}: {fix}")
    return 1


def error_line(message: str) -> None:
    """Print `ERROR: MESSAGE` on one line of stderr: held back, while a verb runs, until `basis` has logged that it
    finished, so that the ERROR lines come last."""
    line = f"ERROR: {message}\n"
    if (held := _held.get()) is not None:
        held.append(line)
    else:
        sys.stderr.write(line)


@contextmanager
def holding() -> Generator[list[str]]:
    """Hold back the ERROR lines printed in the block, in the list it gives, for the caller to print."""
    held: list[str] = []
    token = _held.set(held)
    try:
        yield held
    finally:
        _held.reset(token)
