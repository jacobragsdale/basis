"""A trial's repo: generated afresh from this checkout's basis (`--framework-path`), the task's `setup/` laid over it,
and committed and tagged `eval-base`, so that the grader can compare what the agent left with where it began."""

import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from basis_eval.tasks import Task

FRAMEWORK = Path(__file__).resolve().parents[2]
"""The basis checkout every trial's repo is generated from, and depends on, as editable paths."""
BASE = "eval-base"
NAMES = {"job": ("basis-job", "repo", "pricing-etl"), "api": ("basis-api", "app", "quotes")}
"""Each kind of repo's generator, its noun, and the repo's name."""
UNSET = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_FROZEN", "UV_LOCKED", "PYTHONPATH", "PYTEST_ADDOPTS", "COVERAGE_FILE", "COVERAGE_PROCESS_START", "CLAUDECODE")
"""What would point a command in the trial's repo at this workspace's environment, or tell Claude Code it runs inside
another session."""
IDENTITY = {"GIT_AUTHOR_NAME": "basis eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid", "GIT_COMMITTER_NAME": "basis eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}


class RepoError(Exception):
    pass


def environment(scratch: Path, variables: Mapping[str, str] = os.environ) -> dict[str, str]:
    """`variables` for a command in a trial's repo: without `UNSET`, git's own and the calling Claude Code session's,
    with its scratch in `scratch`, which is deleted with the trial."""
    kept = {name: value for name, value in variables.items() if name not in UNSET and not name.startswith(("GIT_", "CLAUDE_"))}
    return kept | {"TMPDIR": str(scratch)}


def run(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float = 600) -> tuple[int, str, float]:
    """`argv` in `cwd`: its exit code, its stdout and stderr together, and its seconds."""
    start = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=cwd, env=dict(env), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as expired:
        given: object = expired.output
        output = given.decode("utf-8", "replace") if isinstance(given, bytes) else given if isinstance(given, str) else ""
        return 124, output + f"\ntimed out after {timeout:.0f} s", time.monotonic() - start
    return done.returncode, done.stdout + done.stderr, time.monotonic() - start


def must(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> str:
    code, output, _ = run(argv, cwd, env)
    if code != 0:
        msg = f"`{' '.join(argv)}` in {cwd} exited {code}:\n{output[-2000:]}"
        raise RepoError(msg)
    return output


def lay(source: Path, files: list[Path], repo: Path) -> None:
    for relative in files:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def commit(repo: Path, env: Mapping[str, str], message: str) -> None:
    """Commit everything in `repo`, its hooks off: the grader's commits are not the agent's."""
    must(["git", "add", "--all"], repo, env)
    must(["git", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "--no-verify", "-m", message], repo, {**env, **IDENTITY})


def make(task: Task, folder: Path, env: Mapping[str, str]) -> Path:
    """The task's repo, made in `folder`, which must be empty or absent, and tagged `eval-base`."""
    folder.mkdir(parents=True, exist_ok=True)
    tool, noun, name = NAMES[task.spec.repo]
    must(["uv", "run", "--project", str(FRAMEWORK), tool, "new", noun, name, "--dir", str(folder), "--framework-path", str(FRAMEWORK)], FRAMEWORK, env)
    repo = folder / name
    for argv in task.spec.setup:
        must(argv, repo, env)
    lay(task.folder / "setup", task.files("setup"), repo)
    must(["uv", "sync", "--quiet"], repo, env)
    commit(repo, env, "The task's repo, as the agent finds it")
    must(["git", "tag", BASE], repo, env)
    return repo


def solve(task: Task, repo: Path, env: Mapping[str, str]) -> None:
    """The reference solution: its commands, its files, and the commands after."""
    for argv in task.spec.solve:
        must(argv, repo, env)
    lay(task.folder / "solution", task.files("solution"), repo)
    for argv in task.spec.after:
        must(argv, repo, env)
