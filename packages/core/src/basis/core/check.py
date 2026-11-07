"""`basis check`: what a repo's code must pass before a commit, with no network, database or Docker. Each declared
part's agent files are compared with the installed version's, then the lockfile, ruff's lint and format, ty and
pytest run through uv, in the repo's own locked environment. Every step runs, so one check finds every problem;
each tool lists its own findings, and each step that fails ends with one `ERROR` line that says how to fix it."""

import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from basis.core import sync
from basis.core.sync import AgentFiles
from basis.core.verbs import fail

type Runner = Callable[[Sequence[str], Path], int]
"""Runs a command in a folder, its output going where the caller's does, and returns its exit code."""


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
    Step(("uv", "run", "--locked", "--quiet", "pytest", "-q"), "pytest failed, as above", "fix the code, or the test, that each failure names"),
)


def check(repo: Path, files: Iterable[AgentFiles], *, run: Runner) -> int:
    """Run every check on the repo at `repo`, whose parts' agent files are `files`; 0 when every one passes."""
    failures = 0
    for part in files:
        if stale := sync.stale(repo, part):
            names = ", ".join(path.relative_to(repo).as_posix() for path in stale)
            failures += fail(f"{names} {'does' if len(stale) == 1 else 'do'} not match the installed {part.part}", "run `uv run basis sync`, which rewrites them")
    if run(LOCK.argv, repo) != 0:
        return fail(LOCK.reason, LOCK.fix)
    for step in STEPS:
        if run(step.argv, repo) != 0:
            failures += fail(step.reason, step.fix)
    return 1 if failures else 0


def run_command(argv: Sequence[str], folder: Path) -> int:
    sys.stdout.flush()  # so what basis printed comes before what the command prints
    sys.stderr.flush()
    try:
        return subprocess.run(argv, cwd=folder, check=False).returncode
    except FileNotFoundError as error:
        msg = f"{argv[0]} is not on the PATH, and basis check runs every tool through it: install it, as https://docs.astral.sh/uv/ says"
        raise FileNotFoundError(msg) from error
