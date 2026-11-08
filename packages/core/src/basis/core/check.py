"""`basis check`: what a repo's code must pass before a commit, with no network, database or Docker. With `--staged`,
which the commit hook gives, the staged files are checked first (`basis.core.hygiene`). The files `basis sync` owns
are compared with what basis and the declared parts write, the clone is pointed at the repo's hooks if it names none
and checked to run them, each declared part's own checks of the repo's code run (`Part.checks`), and the tests are
held to basis's gates (`basis.core.gates`): no mocks, a coverage floor, and no sockets in unit tests. Then the
lockfile, ruff's lint and format, ty and pytest run through uv, in the repo's own locked environment, pytest measuring
coverage. Every step runs, so one check finds every problem: a file basis cannot read, or one of the repo's own at a
path basis writes, is one line, and every other file is still compared. Each tool lists its own findings, and each
step that fails ends with one `ERROR` line that says how to fix it."""

import logging
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from basis.core import gates, hooks, hygiene, sync
from basis.core.errors import BasisError
from basis.core.sync import AgentFiles, Problem
from basis.core.verbs import Part, error_line, fail

type Runner = Callable[[Sequence[str], Path], int]
"""Runs a command in a folder, its output going where the caller's does, and returns its exit code."""
type Checks = Sequence[tuple[str, Callable[[Path], Sequence[Problem]]]]
"""Each declared part's own checks of the repo's code, by the part's name."""

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    reason: str
    """What a nonzero exit means."""
    fix: str


LOCK = Step(("uv", "lock", "--check", "--quiet"), "uv.lock does not match pyproject.toml", "run `uv lock`, and commit uv.lock")
"""Runs first: every other step runs in the environment the lockfile pins, so none of them runs without it."""
STEPS = (
    Step(
        ("uv", "run", "--locked", "--quiet", "ruff", "check", "--quiet", "--output-format", "concise", "."),
        "ruff check found the problems listed above",
        "fix each; `uv run ruff check --fix .` fixes those marked [*]",
    ),
    Step(("uv", "run", "--locked", "--quiet", "ruff", "format", "--check", "--quiet", "."), "ruff format would reformat the files listed above", "run `uv run ruff format .`"),
    # ty's --quiet hides its diagnostics as well as its progress, so it prints "All checks passed!" when it passes
    Step(("uv", "run", "--locked", "--quiet", "ty", "check", "--output-format", "concise"), "ty check found the type errors listed above", "fix each"),
)
PYTEST = Step(("uv", "run", "--locked", "--quiet", "pytest", "-q"), "pytest failed, as above", "fix the code, or the test, that each failure names")
"""Runs last, with what the gates add: coverage, measured as basis measures it, and the file basis's plugin writes."""


def check(repo: Path, files: Sequence[AgentFiles], *, run: Runner, staged: bool = False, checks: Checks = ()) -> int:
    """Run every check on the repo at `repo`, whose parts' agent files are `files` and whose parts' own checks are
    `checks`, and with `staged` on the files staged for a commit; 0 when every one passes."""
    hooks.ensure(repo)
    failures = sum(fail(*problem) for problem in hygiene.problems(repo)) if staged else 0
    found = sync.plan(repo, files)
    failures += sum(fail(*problem) for problem in found.problems())
    if stale := found.stale():
        names = ", ".join(path.relative_to(repo).as_posix() for path in stale)
        failures += fail(f"{names} {'does' if len(stale) == 1 else 'do'} not match what the installed basis writes", "run `uv run basis sync`, which rewrites or removes each")
    if (found := hooks.problem(repo)) is not None:
        failures += fail(*found)
    failures += sum(_part_checks(repo, checks))
    failures += sum(fail(*problem) for problem in gates.mocks(repo))
    failures += sum(fail(*problem) for problem in gates.rounding(repo))
    floor, missing = gates.floor(repo), gates.installed()
    if isinstance(floor, tuple):
        failures += fail(*floor)
    elif missing is not None:
        failures += fail(*missing)
    if run(LOCK.argv, repo) != 0:
        return fail(LOCK.reason, LOCK.fix)
    for step in STEPS:
        if run(step.argv, repo) != 0:
            failures += fail(step.reason, step.fix)
    with gates.measured(floor if isinstance(floor, int) and missing is None else None) as measured:
        code = run((*PYTEST.argv, *measured.argv()), repo)
        tested = measured.found(code)
    failures += sum(fail(*problem) for problem in tested.problems)
    if code != 0 and not tested.explained:
        failures += fail(PYTEST.reason, PYTEST.fix)
    return 1 if failures else 0


def checks_of(parts: Mapping[str, Part]) -> list[tuple[str, Callable[[Path], Sequence[Problem]]]]:
    """Each of `parts`' own checks, by the part's name."""
    return [(name, part.checks) for name, part in parts.items() if part.checks is not None]


def _part_checks(repo: Path, checks: Checks) -> list[int]:
    """Each part's checks' problems, each printed as its line, and one line for a part whose checks raised."""
    failed: list[int] = []
    for name, part_checks in checks:
        try:
            problems = part_checks(repo)
        except BasisError as error:
            error_line(" ".join(str(error).split()))
            failed.append(1)
            continue
        except Exception as error:
            log.exception("the %s part's checks failed", name)
            failed.append(fail(f"the {name} part's checks could not run, {type(error).__name__}: {' '.join(str(error).split())}", "a defect; the log record above holds its traceback"))
            continue
        failed += [fail(*problem) for problem in problems]
    return failed


def run_command(argv: Sequence[str], folder: Path) -> int:
    sys.stdout.flush()  # so what basis printed comes before what the command prints
    sys.stderr.flush()
    try:
        return subprocess.run(argv, cwd=folder, check=False).returncode
    except FileNotFoundError as error:
        msg = f"{argv[0]} is not on the PATH, and basis check runs every tool through it: install it, as https://docs.astral.sh/uv/ says"
        raise BasisError(msg) from error
