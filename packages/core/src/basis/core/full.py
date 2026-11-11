"""`basis check --full`, which the push hook runs: `basis check`, then `regress` where the run store is reachable, the QA
tier where the repo declares one and this machine has its settings file (`basis.core.qa`), each part's contract
diff, each part's checks too slow for every commit (`Part.full_checks`), which run first, and mutation testing of the domain functions the change touches, in WSL and on Linux (`basis.core.mutation`).
Each piece that does not run prints one `SKIPPED:` line that says why, and each that runs prints one line of what it
found. `regress` seeing an output change fails nothing: the change may be meant, and `basis review` shows it to the
reviewer; nor does a mutant that survives, or mutmut failing. A failure to regress, a QA tier that refuses or a QA run
that fails, fails the check.

`regress` reads the store `basis regress` reads, the process's `BASIS_SNAPSHOT_ROOT`, and writes nothing. When the
push hook checks a commit in a worktree of its own, it names the clone's own folder (`folder`), where the files git
does not track are: the QA settings file, and a run store a relative path names. What the check found is recorded for
`basis review` when the repo's folder holds no change its commit does not. A failure in a piece, whatever it raises, is
its `ERROR` line, and the pieces after it still run and are recorded. `basis upgrade` leaves the QA tier out, since
its relock is not yet a commit: the push runs it."""

import logging
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from basis.core import check, clone, hygiene, mutation, qa, review
from basis.core.check import Runner
from basis.core.errors import BasisError
from basis.core.settings import Settings, locate
from basis.core.storage import Root
from basis.core.sync import AgentFiles
from basis.core.verbs import Part, Regress, fail

LAST = 10
"""How many of the newest published runs `regress` replays of each job, or of whatever else a part runs by name, as
`basis regress` does."""

type Report = Callable[[Exception], str]
"""Prints a failure as its one line, with its traceback first if it is a defect, and returns the line."""


log = logging.getLogger(__name__)

QA_LATER = "it runs when you push, once the change is committed"
"""Why `basis upgrade`'s check leaves the QA tier out."""


def full(  # noqa: PLR0913 -- the repo, its parts, and how the check reaches the world, by keyword
    repo: Path, files: Sequence[AgentFiles], parts: Mapping[str, Part], *, run: Runner, report: Report, folder: Path, clock: Callable[[], datetime], with_qa: bool = True
) -> int:
    """Run `basis check --full` on the repo at `repo`, whose untracked files are in `folder`, and with `with_qa` the QA
    tier; 0 when every piece that ran passed."""
    found = clone.of(repo)
    commit = found.head() if found is not None else None
    clean = found is not None and found.clean()  # before the check, which may write what git does not ignore
    checked = check.check(repo, files, run=run, checks=check.checks_of(parts)) == 0
    if commit is not None:  # what the commit hook checks of a staged file, of each file a commit made with --no-verify adds
        checked = not sum(fail(*problem) for problem in hygiene.pushing(repo, commit)) and checked
    deeper, slow = check.checks_of(parts, full=True), 0
    if checked:
        slow = sum(check.part_checks(repo, deeper))
        if deeper and not slow:
            _said(f"the {', '.join(name for name, _ in deeper)} part's checks for --full found nothing")
    elif deeper:
        _skipped("each part's checks for --full", "basis check failed, as above")
    if checked:
        regressed = _regress(parts, folder, report)
        tier = _qa(repo, parts, commit=commit, folder=folder, report=report) if with_qa else _skip_qa(QA_LATER)
        contracts, broken, unoffered = _contracts(repo, parts, report)
        mutated = _mutation(repo)
    else:
        why = "basis check failed, as above"
        regressed, tier, contracts, broken, unoffered = _skip_regress(why), _skip_qa(why), {}, 0, why
        _skipped("the contract diffs", why)
        mutated = _skip_mutation(why)
    passed = checked and not slow and not regressed.failures and not tier.refused and not any(run.failure for run in tier.runs) and not broken
    if found is None or commit is None:
        _skipped("the record for basis review", "the repo is in no clone with a commit")
    elif not clean:
        _skipped("the record for basis review", "the repo's folder has changes no commit holds, so no commit is what was checked: commit them, and check again")
    else:
        record = review.Record(
            commit=commit,
            folder=found.prefix,
            checked_at=clock(),
            checked=checked,
            passed=passed,
            versions=review.versions(),
            regress=regressed,
            qa=tier,
            contracts=contracts,
            contracts_skipped=unoffered,
            mutation=mutated,
        )
        review.save(found, record)
        _said(f"recorded that commit {commit} {'passed' if passed else 'failed'}, for `uv run basis review`")
    return 0 if passed else 1


def _regress(parts: Mapping[str, Part], folder: Path, report: Report) -> review.RegressRecord:
    saving = {name: part.runs for name, part in parts.items() if part.runs is not None and part.runs.regress is not None}
    if not saving:
        return _skip_regress("no part of this repo saves runs")
    try:
        configured = Settings().snapshot_root
    except (ValidationError, BasisError) as error:
        return review.RegressRecord(failures=[report(error)])
    if configured is None:
        return _skip_regress("BASIS_SNAPSHOT_ROOT is not set, so there is no run store to replay: set it, where git runs the push hook, to the store whose published runs to replay")
    location = locate(configured, folder)
    try:
        reachable = Root.at(location).is_dir()
    except (OSError, ValueError) as error:  # pyarrow's errors for a store it cannot reach are OSError and ValueError
        return _skip_regress(f"the run store {location}, BASIS_SNAPSHOT_ROOT, is not reachable ({' '.join(str(error).split())})")
    if not reachable:
        return _skip_regress(f"the run store {location}, BASIS_SNAPSHOT_ROOT, does not exist here")
    replayed: list[review.RegressedRecord] = []
    failures: list[str] = []
    for name, runs in saving.items():
        if runs.regressions is None:
            _skipped(f"regress of the {name} part", f"it gives `basis check --full` no regressions to record: `uv run basis regress` replays its {runs.noun}s")
            continue
        try:
            found = runs.regressions(Regress(None, LAST, {}, location))
        except Exception as error:  # noqa: BLE001 -- one part's failure is its own line, and the check goes on
            error.add_note(f"while regressing the {name} part's runs")
            failures.append(report(error))
            continue
        failures += [report(error) for error in found.failures]
        replayed += [
            review.RegressedRecord(
                name=entry.name, run_id=entry.run_id, reasons=list(entry.reasons), differences=[review.DifferenceRecord(output=diff.output, summary=diff.summary) for diff in entry.differences]
            )
            for entry in found.replayed
        ]
    changed = [entry for entry in replayed if entry.reasons or entry.differences]
    count = f"{len(replayed)} published run{'' if len(replayed) == 1 else 's'}"
    differ = f"{len(changed)} {'differs' if len(changed) == 1 else 'differ'}, as `uv run basis review` lists" if changed else "none differs"
    _said(f"regress replayed {count} from {location}: {differ}")
    for entry in changed:
        how = [*(f"{diff.output}: {diff.summary}" for diff in entry.differences), *(f"would not write: {reason}" for reason in entry.reasons)]
        _said(f"  {entry.name} {entry.run_id}: {'; '.join(how)}")
    return review.RegressRecord(store=location, replayed=replayed, failures=failures)


def _skip_regress(why: str) -> review.RegressRecord:
    _skipped("regress", why)
    return review.RegressRecord(skipped=why)


def _skip_qa(why: str) -> review.QaRecord:
    _skipped("the QA tier", why)
    return review.QaRecord(skipped=why)


def _qa(repo: Path, parts: Mapping[str, Part], *, commit: str | None, folder: Path, report: Report) -> review.QaRecord:
    try:
        outcome = qa.tier(repo, parts, commit=commit, folder=folder)
    except Exception as error:  # noqa: BLE001 -- the tier's failure is its line; what regress found is still recorded
        error.add_note("while running the QA tier")
        return review.QaRecord(refused=[report(error)])
    if outcome.skipped is not None:
        return _skip_qa(outcome.skipped)
    for problem in outcome.refused:
        fail(*problem)
    if outcome.refused:
        fail("the QA tier ran nothing, for the reasons above", "fix each, and check again")
    runs: list[review.QaRunRecord] = []
    for part, entry in outcome.runs:
        if entry.failure is not None:
            fail(*entry.failure)
        else:
            _said(f"the QA tier ran {part} {entry.name}{f' as run {entry.run_id}' if entry.run_id is not None else ''}: {entry.summary}")
        failure = f"{entry.failure[0]}: {entry.failure[1]}" if entry.failure is not None else None
        runs.append(review.QaRunRecord(part=part, name=entry.name, run_id=entry.run_id, summary=entry.summary, failure=failure))
    return review.QaRecord(store=outcome.store, refused=[f"{reason}: {fix}" for reason, fix in outcome.refused], runs=runs)


NO_CONTRACT = "no part of this repo offers a contract, such as an API's openapi.json, so there is no diff to show"


def _contracts(repo: Path, parts: Mapping[str, Part], report: Report) -> tuple[dict[str, str | None], int, str | None]:
    """Each part's contract diff, by the part's name, how many parts failed to give theirs, and why there are none
    when no part offers one."""
    if not any(part.contract is not None for part in parts.values()):
        _skipped("the contract diffs", NO_CONTRACT)
        return {}, 0, NO_CONTRACT
    found: dict[str, str | None] = {}
    broken = 0
    for name, part in parts.items():
        if part.contract is None:
            continue
        try:
            found[name] = part.contract(repo)
        except Exception as error:  # noqa: BLE001 -- one part's failure is its own line, and the check goes on
            error.add_note(f"while diffing the {name} part's contract")
            report(error)
            broken += 1
            continue
        diff = found[name]
        _said(f"the {name} part's contract is unchanged" if diff is None else f"the {name} part's contract: {_summary(diff)}, as `uv run basis review` shows")
    return found, broken, None


def _mutation(repo: Path) -> review.MutationRecord:
    try:
        found = mutation.mutate(repo)
    except Exception as error:
        log.exception("mutation testing failed")
        failure = f"{type(error).__name__}: {' '.join(str(error).split())}, a defect in basis; the log record above holds its traceback"
        _said(f"mutation testing failed, which fails nothing: {failure}")
        return review.MutationRecord(failure=failure)
    if found.skipped is not None:
        return _skip_mutation(found.skipped, found)
    count = f"{found.mutants} mutant{'' if found.mutants == 1 else 's'}"
    functions = f"{len(found.functions)} domain function{'' if len(found.functions) == 1 else 's'}"
    survived = f"{found.survived} survived, as `uv run basis review` lists" if found.survived else "none survived"
    failed = f"; mutmut failed, which fails nothing: {found.failure}" if found.failure is not None else ""
    _said(f"mutation testing made {count} of the {functions} the change touches: {survived}{failed}")
    return found


def _skip_mutation(why: str, found: review.MutationRecord | None = None) -> review.MutationRecord:
    _skipped("mutation testing", why)
    return found if found is not None else review.MutationRecord(skipped=why)


def _summary(diff: str) -> str:
    """A contract diff's summary, its first line: without a Markdown heading's or a list item's mark, or a full stop,
    and `changed` where the line holds nothing else."""
    first = re.sub(r"^(?:#+|>|[-*+]|[0-9]+[.)])\s+", "", diff.partition("\n")[0].strip()).strip().rstrip(".").strip()
    return first or "changed"


def _skipped(piece: str, why: str) -> None:
    sys.stderr.write(f"SKIPPED: {piece}: {why}\n")


def _said(line: str) -> None:
    sys.stderr.write(f"basis check --full: {line}\n")
