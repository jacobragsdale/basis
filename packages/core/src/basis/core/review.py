"""`basis review`: a pull request's summary, in Markdown, of what `basis check --full` found on a commit, so a reviewer
reads what the change does rather than lines of agent-written code: the outputs `regress` saw change, what the QA tier
ran, each part's contract diff, the basis versions, and the commit that was checked.

`check --full` records what it found as a `Record`, once per commit, in the git folder every worktree of the clone
shares, at `basis/checks/PREFIX/COMMIT.json`, where PREFIX is the repo's folder in the clone. The push hook checks
each pushed commit, in place or in a worktree of its own, so the record is there when the pull request is opened. A
check of a folder with changes no commit holds is not recorded, since no commit is what was checked."""

import sys
from datetime import datetime
from importlib.metadata import entry_points, version
from pathlib import Path

from pydantic import Field

from basis.core import clone
from basis.core.model import Model
from basis.core.verbs import GROUP, fail


class DifferenceRecord(Model):
    output: str
    summary: str


class RegressedRecord(Model):
    name: str
    run_id: str
    reasons: list[str] = Field(default_factory=list)
    differences: list[DifferenceRecord] = Field(default_factory=list)


class RegressRecord(Model):
    """What `regress` did, as `check --full` ran it."""

    skipped: str | None = None
    """Why it did not run, when it did not."""
    store: str | None = None
    replayed: list[RegressedRecord] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    """Each failure's line."""


class QaRunRecord(Model):
    part: str
    name: str
    run_id: str | None = None
    summary: str
    failure: str | None = None


class QaRecord(Model):
    """What the QA tier did, as `check --full` ran it."""

    skipped: str | None = None
    store: str | None = None
    refused: list[str] = Field(default_factory=list)
    """Each reason it ran nothing."""
    runs: list[QaRunRecord] = Field(default_factory=list)


class Record(Model):
    """What `basis check --full` found on one commit."""

    commit: str
    folder: str
    """The repo's folder in its clone, `/`-separated with a trailing `/`, or empty at its top."""
    checked_at: datetime
    checked: bool
    """Whether `basis check` passed."""
    passed: bool
    """Whether every piece of `check --full` passed: `check`, `regress` and the QA tier ran without a failure, though
    `regress` may have seen outputs change."""
    versions: dict[str, str]
    regress: RegressRecord
    qa: QaRecord
    contracts: dict[str, str | None] = Field(default_factory=dict)
    """Each part's contract diff, by the part's name, or None when its contract has not changed."""
    contracts_skipped: str | None = None
    """Why there are no contract diffs, when there are none: no part offers a contract, or `basis check` failed."""


def versions() -> dict[str, str]:
    """basis-core's version and each installed part's package's, by distribution."""
    found = {"basis-core": version("basis-core")}
    for entry in entry_points(group=GROUP):
        if entry.dist is not None:
            found.setdefault(entry.dist.name, entry.dist.version)
    return found


def path(found: clone.Clone, commit: str) -> Path:
    """Where `found`'s record of `commit` is kept."""
    return found.common / "basis" / "checks" / found.prefix / f"{commit}.json"


def save(found: clone.Clone, record: Record) -> Path:
    """Keep `record`, replacing any earlier record of its commit, and return where."""
    target = path(found, record.commit)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def review(repo: Path, ref: str) -> int:
    """Print the summary of what `check --full` found on the commit `ref` names, in the clone that holds `repo`."""
    found = clone.of(repo)
    if found is None:
        return fail(f"{repo} is in no git clone, so no commit of it was checked", "run basis review in a clone of the repo")
    commit = found.commit(ref)
    if commit is None:
        return fail(f"{ref} names no commit in this clone", "give a commit, a branch or a tag, or leave --commit out for HEAD")
    kept = path(found, commit)
    if not kept.is_file():
        return fail(f"commit {commit} has no `basis check --full` recorded in this clone", "push it, which runs the check, or run `uv run basis check --full` with every change committed")
    record = Record.model_validate_json(kept.read_bytes())
    sys.stdout.write(markdown(record))
    if not record.passed:
        return fail(f"commit {commit} failed basis check --full, as the summary says", "fix what it says, commit, and push again")
    return 0


def markdown(record: Record) -> str:
    """The summary of `record`, for a pull request's description."""
    outcome = "passed" if record.passed else "failed"
    at = record.checked_at.strftime("%Y-%m-%d %H:%M UTC")
    installed = ", ".join(f"{name} {number}" for name, number in record.versions.items())
    lines = ["## What basis checked", "", f"`basis check --full` {outcome} on commit `{record.commit}`, on {at}, with {installed}."]
    if not record.checked:
        lines += ["", "`basis check` failed, so neither regress nor the QA tier ran."]
    lines += ["", "### What regress saw change", "", *_regress(record.regress)]
    lines += ["", "### The QA tier", "", *_qa(record.qa)]
    if record.contracts_skipped is not None:
        lines += ["", "### Contracts", "", f"Skipped: {record.contracts_skipped}."]
    for part, diff in record.contracts.items():
        lines += ["", f"### The {part} contract", "", diff if diff is not None else "Unchanged since the last release."]
    lines += ["", "### Versions", "", *(f"- {name} {number}" for name, number in record.versions.items())]
    return "\n".join(lines) + "\n"


def _regress(found: RegressRecord) -> list[str]:
    if found.skipped is not None:
        return [f"Skipped: {found.skipped}."]
    lines = [f"Failed: {failure}" for failure in found.failures]
    changed = [run for run in found.replayed if run.reasons or run.differences]
    store = f" from `{found.store}`" if found.store is not None else ""
    replayed = f"regress replayed {_runs(len(found.replayed))}{store} with this commit's code"
    if not changed:
        return [*lines, f"{replayed}, and none differs."]
    rows = [
        f"| {_cell(run.name)} | `{run.run_id}` | {_cell('; '.join([*(f'{entry.output}: {entry.summary}' for entry in run.differences), *(f'would not write: {reason}' for reason in run.reasons)]))} |"
        for run in changed
    ]
    return [*lines, f"{replayed}, and {len(changed)} {'differs' if len(changed) == 1 else 'differ'}:", "", "| Name | Run | What differs |", "|---|---|---|", *rows]


def _qa(found: QaRecord) -> list[str]:
    if found.skipped is not None:
        return [f"Skipped: {found.skipped}."]
    if found.refused:
        return ["The QA tier refused to run, and ran nothing:", "", *(f"- {_cell(reason)}" for reason in found.refused)]
    store = f", saved in `{found.store}`" if found.store is not None else ""
    rows = [f"| {_cell(run.part)} | {_cell(run.name)} | {f'`{run.run_id}`' if run.run_id is not None else '-'} | {_cell(run.failure or run.summary)} |" for run in found.runs]
    failed = sum(run.failure is not None for run in found.runs)
    result = f"{failed} failed" if failed else "it did what it was asked" if len(found.runs) == 1 else "each did what it was asked"
    made = f"{len(found.runs)} run{'' if len(found.runs) == 1 else 's'}"
    return [f"The QA tier made {made} against QA{store}, and {result}:", "", "| Part | Name | Run | What it did |", "|---|---|---|---|", *rows]


def _runs(count: int) -> str:
    return f"{count} published run{'' if count == 1 else 's'}"


def _cell(text: str) -> str:
    """`text` as one cell of a Markdown table: on one line, with its pipes escaped."""
    return " ".join(text.split()).replace("|", "\\|")
