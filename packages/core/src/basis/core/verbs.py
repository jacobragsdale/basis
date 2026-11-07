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

No two declared parts may add the same verb, nor a verb core has, nor the same kind to `new`. A verb that several
kinds of part share is core's, and reaches each part through a field of `Part`, as `runs` does for the verbs a job
and a model both have.

A shared verb reaches the only declared part that has it, whatever NAME is, so a module path names a job or a model
there too. Where several do, NAME comes first, and is a name one of them lists in `Runs.names`; two of them listing the
same name is refused, with the fix. Without NAME, there, `describe` prints what every part runs, `regress` replays
every part's runs, `ddl` asks the first part, which prints only the ledger without NAME, and `run`, `resume` and
`replay` are refused, with the fix. `basis VERB NAME --help` gives the arguments of NAME's part.

`renamed` refuses, before any verb runs, the variables an earlier version of the part read under names it no longer
reads, as `basis` refuses its own `ETL_*`.

A handler reads its `argparse.Namespace` into a frozen dataclass before anything else reads it, and returns the exit
code: 0 when the verb did what it says, and 1 when it did not. It reports a failure the caller can fix as one line,
with `fail`, or by raising a `basis.core.errors.BasisError` whose message gives the reason and ends with the fix, which
`basis` prints as the line; a pydantic `ValidationError` of the caller's input prints as one line too. Any other
exception is a defect, and `basis` logs its traceback before its line; `basis.core.errors.deliberate` marks a builtin
error that a call raises on purpose, such as a lookup that finds no run.
A result that a caller reads, such as `describe`'s, is JSON on stdout, and logs are JSON lines on stderr."""

import argparse
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from basis.core.sync import AgentFiles


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
class Runs:
    """What a part runs by name, such as a repo's jobs or models, and its side of each verb core shares among such
    parts; a verb left None is one the part does not have."""

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


def fail(reason: str, fix: str) -> int:
    """Print a failure as `basis` prints every one, `ERROR: REASON: FIX` on one line of stderr, and return 1."""
    sys.stderr.write(f"ERROR: {reason}: {fix}\n")
    return 1
