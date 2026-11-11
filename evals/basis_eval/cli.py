"""`uv run evals/run.py`: validate the tasks, run the trials, and write the report. `evals/README.md` is the guide."""

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from basis_eval import repo, report, tasks, trials

MIN_AVAILABLE_GB = 6.0
"""Under this much available memory, no trial starts: two trials take about a gigabyte each, with their checks, and the
host runs other work."""
REPORT = trials.RESULTS / "report.md"


def available_gb(meminfo: Path = Path("/proc/meminfo")) -> float | None:
    """The memory available, as the kernel counts it; None where it does not say."""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        return None
    return None


def pending(chosen: list[tasks.Task], models: list[str], count: int, done: list[trials.Trial]) -> list[tuple[tasks.Task, str, int]]:
    """The trials still to run: each task's, for each model, numbered from 1 to `count`, but those `done` records."""
    seen = {(trial.task, trial.model, trial.trial) for trial in done}
    return [(task, model, number) for model in models for number in range(1, count + 1) for task in chosen if (task.name, model, number) not in seen]


def _output(argv: Sequence[str]) -> str | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=True, cwd=repo.FRAMEWORK).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None


def _say(*lines: str) -> None:
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    sys.stdout.flush()


def _fail(reason: str, fix: str) -> int:
    sys.stderr.write(f"ERROR: {reason}: {fix}\n")
    return 1


def validate(arguments: argparse.Namespace) -> int:
    chosen = tasks.every(names=arguments.tasks)
    failures = []
    with ThreadPoolExecutor(max_workers=arguments.parallel) as pool:
        found = {task.name: pool.submit(trials.validate, task) for task in chosen}
        for name, future in found.items():
            problems = future.result()
            _say(f"{name}: {'ok' if not problems else 'FAILS'}", *(f"  - {problem}" for problem in problems))
            failures += [name] if problems else []
    if failures:
        return _fail(f"tasks {failures} cannot be trusted", "fix each one's hidden tests or reference, as its lines above say, and validate it again")
    return 0


def run_trials(arguments: argparse.Namespace, gauge: Callable[[], float | None] = available_gb) -> int:
    commit = _output(["git", "rev-parse", "HEAD"]) or "unknown"
    if _output(["git", "status", "--porcelain", "--", "packages"]) not in {None, ""}:
        return _fail("packages/ has changes no commit holds, so the trials would measure a basis no commit is", "commit them, or run the trials from a clean checkout")
    version = (_output(["claude", "--version"]) or "").split(" ")[0] or None
    todo = pending(tasks.every(names=arguments.tasks), arguments.models, arguments.trials, trials.recorded())
    _say(f"{len(todo)} trials to run, {arguments.parallel} at a time, on basis {commit[:12]} and Claude Code {version}")
    running: list[Future[trials.Trial]] = []
    stopped = False
    with ThreadPoolExecutor(max_workers=arguments.parallel) as pool:
        for task, model, number in todo:
            while len([each for each in running if not each.done()]) >= arguments.parallel:
                _collect(running, block=True)
            _collect(running, block=False)
            free = gauge()
            if free is not None and free < MIN_AVAILABLE_GB:
                sys.stderr.write(f"ERROR: {free:.1f} GB of memory is available, under {MIN_AVAILABLE_GB:.0f}: no more trials start; run the command again once the host has more\n")
                stopped = True
                break
            running.append(pool.submit(trials.run, task, model, number, commit=commit, version=version))
        while running:
            _collect(running, block=True)
    report.write(REPORT, trials.recorded())
    return 1 if stopped else 0


def _collect(running: list[Future[trials.Trial]], *, block: bool) -> None:
    for future in list(running):
        if block or future.done():
            trial = future.result()
            trials.record(trial)
            running.remove(future)
            outcome = "passed" if trial.passed else "failed"
            _say(f"{trial.model} {trial.task} #{trial.trial}: {outcome} in {trial.agent.seconds:.0f} s, {trial.agent.tokens.total:,} tokens")
            if block:
                return


def write_report(_: argparse.Namespace) -> int:
    report.write(REPORT, trials.recorded())
    _say(str(REPORT))
    return 0


def parser() -> argparse.ArgumentParser:
    made = argparse.ArgumentParser(prog="evals/run.py", description="basis's eval: tasks an agent does in a generated repo, graded by basis's checks.")
    verbs = made.add_subparsers(dest="verb", required=True)
    check = verbs.add_parser("validate", help="prove each task: its untouched repo fails the grade, and its reference passes")
    check.add_argument("--tasks", nargs="+", metavar="TASK")
    check.add_argument("--parallel", type=int, default=2)
    run = verbs.add_parser("trials", help="run the trials not yet recorded, then write the report")
    run.add_argument("--tasks", nargs="+", metavar="TASK")
    run.add_argument("--models", nargs="+", default=["sonnet"], metavar="MODEL", help="Claude Code's --model for each column (default: sonnet)")
    run.add_argument("--trials", type=int, default=3, help="trials of each task per model (default: 3)")
    run.add_argument("--parallel", type=int, default=2, help="trials at once (default: 2, the most this host's memory allows)")
    verbs.add_parser("report", help="write evals/results/report.md from the recorded trials")
    return made


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {"validate": validate, "trials": run_trials, "report": write_report}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        return _HANDLERS[str(arguments.verb)](arguments)
    except tasks.TaskError as error:
        return _fail(str(error), "fix the task's folder under evals/tasks/")
