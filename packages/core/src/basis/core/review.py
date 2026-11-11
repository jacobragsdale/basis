"""`basis review`: a pull request's summary, in Markdown, of what `basis check --full` found on a commit, so a reviewer
reads what the change does rather than lines of agent-written code: the outputs `regress` saw change, what the QA tier
ran, each part's contract diff, the mutants of the domain functions the change touches that no test killed
(`basis.core.mutation`), the basis versions, and the commit that was checked.

`check --full` records what it found as a `Record`, once per commit, in the git folder every worktree of the clone
shares, at `basis/checks/PREFIX/COMMIT.json`, where PREFIX is the repo's folder in the clone. The push hook checks
each pushed commit, in place or in a worktree of its own, so the record is there when the pull request is opened. A
check of a folder with changes no commit holds is not recorded, since no commit is what was checked."""

import re
import sys
from datetime import datetime
from importlib.metadata import entry_points, version
from pathlib import Path

from pydantic import Field

from basis.core import clone, gates, tools
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


class MutantRecord(Model):
    name: str
    """Its name, as mutmut gives it, which `uv run mutmut show NAME` shows in a copy of the repo."""
    function: str
    """The domain function it changed, by its module's dotted name."""
    change: str
    """The lines it changed, before and after, as `BEFORE -> AFTER`."""


class MutationRecord(Model):
    """What mutation testing found, as `check --full` ran it: never a failure of the check."""

    skipped: str | None = None
    """Why it did not run, when it did not."""
    base: str | None = None
    """The commit the change was measured from, or None for a first commit, whose every domain function is new."""
    functions: list[str] = Field(default_factory=list)
    """The domain functions the change touches, by their modules' dotted names."""
    mutants: int = 0
    killed: int = 0
    survived: int = 0
    shown: list[MutantRecord] = Field(default_factory=list)
    """The mutants that survived, as many as `basis.core.mutation.SHOWN`."""
    unsettled: dict[str, int] = Field(default_factory=dict)
    """How many mutants were neither killed nor survived, by what mutmut says of them, such as `timeout`."""
    failure: str | None = None
    """What mutmut said when it failed, which fails nothing."""


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
    mutation: MutationRecord = Field(default_factory=lambda: MutationRecord(skipped="the basis that checked this commit tested no mutants"))


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
    sys.stdout.write(exceptions_markdown(found.root / "pyproject.toml"))
    sys.stdout.write(added_markdown(found.root, commit))
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
    lines += ["", "### Mutation testing", "", *_mutation(record.mutation)]
    lines += ["", "### Versions", "", *(f"- {name} {number}" for name, number in record.versions.items())]
    return "\n".join(lines) + "\n"


_SKIPPING = re.compile(r"pytest\.(?:mark\.(?:skip|skipif|xfail)\b|skip\()")
_TEST = re.compile(r"^\s*(?:async\s+)?def\s+test_\w*")
_BASES = ("@{upstream}", "origin/HEAD", "origin/main", "main", "master")
"""Where a change branched from, tried in turn: its branch's upstream, then the remote's and the clone's main branch."""


def added_markdown(repo: Path, ref: str) -> str:
    """What the change up to `ref` adds against where it branched, for a reviewer to weigh: each suppression of a check,
    each skip of a test and each `monkeypatch.setattr` it adds, and each test it removes, with its line. A change on the branch it would branch
    from is compared with the commit before."""
    lines = ["", "### What the change adds to the checks' exceptions", ""]
    found = clone.of(repo)
    commit = found.commit(ref) if found is not None else None
    if found is None or commit is None:
        return "\n".join([*lines, "Unknown: the repo is in no git clone, or the commit is not there."]) + "\n"
    base = next((merged for name in _BASES if (merged := clone.output(found.git, found.root, "merge-base", commit, name)) not in {None, "", commit}), None)
    base = base or found.commit(f"{commit}^")
    if base is None:
        return "\n".join([*lines, "Nothing to compare with: this is the first commit."]) + "\n"
    printed = clone.output(found.git, found.root, "diff", "--unified=0", "--no-color", "--no-renames", base, commit, "--", ".", raw=True) or ""
    entries: list[tuple[str, str, str]] = []
    path = ""
    for line in printed.splitlines():
        if line.startswith("+++ "):
            path = line.removeprefix("+++ ").removeprefix("b/").removeprefix(found.prefix)
        elif line.startswith("--- "):
            path = path or line.removeprefix("--- ").removeprefix("a/").removeprefix(found.prefix)
        elif line.startswith("+") and not line.startswith("+++"):
            text = line[1:]
            if gates.SUPPRESSION.search(text) or "# basis: allow-mocks" in text:
                entries.append((path, "a suppression", text.strip()))
            elif _SKIPPING.search(text):
                entries.append((path, "a skip", text.strip()))
            elif "monkeypatch.setattr(" in text:  # a test's stand-in for code, which the mock gate does not see: counted, never refused
                entries.append((path, "a patch", text.strip()))
        elif line.startswith("-") and not line.startswith("---") and _TEST.match(line[1:]):
            entries.append((path, "a test removed", line[1:].strip()))
    if not entries:
        return "\n".join([*lines, f"None since {base[:12]}."]) + "\n"
    rows = [f"| `{_cell(where)}` | {what} | `{_cell(text)}` |" for where, what, text in entries]
    return "\n".join([*lines, f"Against {base[:12]}:", "", "| File | What | Line |", "|---|---|---|", *rows]) + "\n"


def exceptions_markdown(pyproject: Path) -> str:
    """The repo's exceptions to basis's checks, each rule at its path with its reason, and a coverage floor under
    basis's, for a reviewer to weigh."""
    lines = ["", "### Exceptions to basis's checks", ""]
    try:
        found = tools.exceptions(pyproject) if pyproject.is_file() else []
    except tools.ToolsError as error:
        return "\n".join([*lines, f"The repo's exceptions cannot be read: {error}."]) + "\n"
    floor = gates.floor(pyproject.parent)
    if isinstance(floor, int) and floor < tools.FLOOR:
        lines.append(f"The coverage floor is {floor}%, under basis's {tools.FLOOR}%.")
        lines.append("")
    if not found:
        lines.append("None.")
    else:
        lines += ["| Rule | Path | Reason |", "|---|---|---|", *(f"| {_cell(item.rule)} | `{_cell(item.path)}` | {_cell(item.reason)} |" for item in found)]
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


def _mutation(found: MutationRecord) -> list[str]:
    if found.skipped is not None:
        return [f"Skipped: {found.skipped}."]
    since = f"since `{found.base}`" if found.base is not None else "in the first commit"
    made = f"mutmut made {found.mutants} mutant{'' if found.mutants == 1 else 's'} of the domain functions the change {since} touches, {', '.join(f'`{name}`' for name in found.functions)}"
    failed = [f"mutmut failed, which fails nothing: {found.failure}", ""] if found.failure is not None else []
    if found.failure is not None and not found.mutants:  # a failure before any mutant ran is no clean run
        return [f"mutmut failed before it tested a mutant, which fails nothing: {found.failure}"]
    outcome = f"the tests killed {found.killed}" + "".join(f", {count} {status}" for status, count in sorted(found.unsettled.items()))
    if not found.survived:
        return [*failed, f"{made}: {outcome}, and none survived."]
    rows = [f"| `{_cell(mutant.function)}` | `{_cell(mutant.name)}` | `{_cell(mutant.change)}` |" for mutant in found.shown]
    more = ["", f"And {found.survived - len(found.shown)} more."] if found.survived > len(found.shown) else []
    return [*failed, f"{made}: {outcome}, and {found.survived} survived, which no test fails:", "", "| Function | Mutant | What it changed |", "|---|---|---|", *rows, *more]


def _runs(count: int) -> str:
    return f"{count} published run{'' if count == 1 else 's'}"


def _cell(text: str) -> str:
    """`text` as one cell of a Markdown table: on one line, with its pipes escaped."""
    return " ".join(text.split()).replace("|", "\\|")
