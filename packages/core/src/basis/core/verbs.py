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
| `check` | always | lints, type checks and tests the repo, and compares each part's `files` with the repo's |
| `sync` | `files` | writes each part's `files` into the repo |
| `docs [PAGE]` | `docs` | lists every part's pages as `PART/PAGE`, or prints one |
| `new KIND NAME` | `new` | calls `new[KIND](repo, NAME)` |
| `replay NAME RUN_ID`, `regress [NAME]` | `runs` | calls `runs.replay` or `runs.regress`, of the part whose `runs.names` holds NAME |

No two declared parts may add the same verb, nor a verb core has, nor the same kind to `new`. A verb that several
kinds of part share is core's, and reaches each part through a field of `Part`, as `replay` does.

A handler reads its `argparse.Namespace` into a frozen dataclass before anything else reads it, and returns the exit
code: 0 when the verb did what it says, and 1 when it did not. It reports a failure as one line, with `fail`, or by
raising a `ValueError`, `LookupError`, `TypeError` or `OSError` whose message gives the reason and ends with the fix,
which `basis` prints as the line. Any other exception is a defect, and `basis` logs its traceback before its line.
A result that a caller reads, such as `describe`'s, is JSON on stdout, and logs are JSON lines on stderr."""

import argparse
import sys
from collections.abc import Callable, Collection, Mapping
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


@dataclass(frozen=True)
class Replay:
    """`basis replay NAME RUN_ID [--params FILE]`."""

    name: str
    """The job or model whose run it is."""
    run_id: str
    params: Path | None
    """Other params to replay with, instead of the run's own."""


@dataclass(frozen=True)
class Regress:
    """`basis regress [NAME] [--last N] [--label KEY=VALUE]... [--snapshots ROOT]`."""

    name: str | None
    """The job or model whose runs to replay, or None for every one the part has."""
    last: int
    """How many published runs of each, newest first."""
    labels: dict[str, str]
    """Labels a run must have, all of them, to be replayed."""
    snapshots: str | None
    """The snapshot root to read, instead of the one the settings name."""


@dataclass(frozen=True)
class Runs:
    """How `basis replay` and `basis regress` rerun a part's saved runs."""

    names: Callable[[], Collection[str]]
    """What the part runs, by name, such as a repo's jobs: how `basis` finds the part a NAME belongs to, when more
    than one declared part saves runs."""
    replay: Callable[[Replay], int]
    regress: Callable[[Regress], int]


@dataclass(frozen=True)
class Part:
    """What one part adds to `basis`; each field but `verbs` gives a core verb something to do."""

    verbs: tuple[Verb, ...] = ()
    """The part's own verbs, such as basis-job's `run`."""
    files: AgentFiles | None = None
    """The part's block of `AGENTS.md` and its skills, as the installed version writes them."""
    docs: Path | None = None
    """A folder of Markdown pages, each `PAGE.md`, for the installed version."""
    new: Mapping[str, Callable[[Path, str], Path]] = field(default_factory=dict[str, Callable[[Path, str], Path]])
    """What `basis new KIND NAME` adds, by KIND: each makes NAME in the repo at the path it is given, and returns
    what it made."""
    runs: Runs | None = None


def fail(reason: str, fix: str) -> int:
    """Print a failure as `basis` prints every one, `ERROR: REASON: FIX` on one line of stderr, and return 1."""
    sys.stderr.write(f"ERROR: {reason}: {fix}\n")
    return 1
