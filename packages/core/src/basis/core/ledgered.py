"""An effect a run records in a database's ledger, such as a message sent or a partition archived: one that cannot be
undone, so the ledger, not the effect, says whether it happened. Each goes in one transaction of that database:

1. Lock the output's ledger rows, so two runs of it never record one at once.
2. Unless the run is told `--again`, make nothing where another run recorded the output for an overlapping interval.
3. Prepare it, where it has anything to prepare (a lake writes the partition under a hidden name), and record it. A
   row this run recorded already means it was made: a resume that finds one makes it no second time.
4. Make it, then commit. A failure before the commit rolls the row back, so the effect is made again on the next
   attempt; one that undoes what it made puts it back first.

A part says what its run is (`Run`), and what each step is for its effect (`Steps`)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from basis.core.databases import Database, LedgerEntry
from basis.core.errors import Conflict
from basis.core.interval import Interval

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Run:
    """What a part's run says of the effects it records."""

    run_id: str
    job: str
    """The part's name for what it ran, such as the job's, as its ledger rows name it."""
    interval: Interval | None
    version: str
    again: bool
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class Steps:
    """What the effect is: its verb, as `send` and `sent`; the ledger row it records, once anything it prepares is
    ready; what makes it, between the record and the commit; and what undoes that, where a commit that fails must."""

    verb: str
    done: str
    entry: Callable[[], LedgerEntry]
    make: Callable[[], None]
    undo: Callable[[], None] | None = None


type Outcome = tuple[Literal["made", "already"], None] | tuple[Literal["skipped"], str]
"""`made`; `already`, made by this run before; or `skipped`, with the reason: another run made it for the interval."""


def ledgered(run: Run, output: str, database: Database, steps: Steps) -> Outcome:
    """Make the run's output `output` as `steps` say, recorded in `database`'s ledger, as the module's docstring says."""
    with database.writing() as writer:
        writer.lock(run.job, output)
        if run.interval is not None and not run.again and (runs := [other for other in writer.overlapping(run.job, output, run.interval) if other != run.run_id]):
            reason = f"runs {', '.join(runs)} {steps.done} {output} for an interval that overlaps {run.interval}: pass --again to {steps.verb} it again"
            log.warning("not %s %s: %s", steps.verb.removesuffix("e") + "ing", output, reason, extra={"output": output})
            return "skipped", reason
        try:
            writer.record(steps.entry())
        except Conflict:
            log.info("%s was %s by this run already", output, steps.done, extra={"output": output})
            return "already", None
        steps.make()
        try:
            writer.commit()
        except BaseException:
            if steps.undo is not None:
                steps.undo()
            raise
    return "made", None
