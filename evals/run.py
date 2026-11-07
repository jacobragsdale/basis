# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Run the eval through Harbor, one trial at a time: this host's memory is tight, and parallel trials skew loop times.

    uv run evals/run.py validate        # every task with the nop agent (must fail) and the oracle (must pass)
    uv run evals/run.py trials          # Claude Code on the subscription, three trials per task

Each trial is a Harbor job of its own under evals/jobs/, named AGENT__TASK__N. A finished trial is skipped, so a
stopped run resumes where it left off. `uv run evals/report.py` turns the jobs into the report.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

ROOT = Path(__file__).parent
TASKS = ROOT / "tasks"
JOBS = ROOT / "jobs"
HARBOR = "harbor==0.25.0"
CLAUDE_CODE = "2.1.319"
TOKEN = Path.home() / ".config/harness-evals/claude-oauth-token"
"""A `claude setup-token` token: Claude Code on the subscription, never an API key. harness-evals reads the same file."""


@dataclass(frozen=True)
class Validate:
    tasks: list[str]


@dataclass(frozen=True)
class Trials:
    tasks: list[str]
    trials: int
    model: str
    effort: str
    token: Path


def tasks() -> list[str]:
    return sorted(path.name for path in TASKS.iterdir() if (path / "task.toml").exists())


def harbor_env(token: str | None) -> dict[str, str]:
    """This process's environment without any Anthropic or Claude credentials or session state, so nothing but the
    token reaches Harbor. The token goes in Harbor's own environment, never `--ae`, which Harbor writes to its job
    config on disk."""
    env = {name: value for name, value in os.environ.items() if not name.startswith(("ANTHROPIC_", "CLAUDE"))}
    if token is not None:
        env |= {"CLAUDE_FORCE_OAUTH": "1", "CLAUDE_CODE_OAUTH_TOKEN": token}
    return env


def finished(job: Path) -> bool:
    return any(job.glob("*/result.json"))


def reward(job: Path) -> dict[str, object]:
    for result in job.glob("*/result.json"):
        data: object = json.loads(result.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            verifier = data.get("verifier_result")
            if isinstance(verifier, dict) and isinstance(rewards := verifier.get("rewards"), dict):
                return {str(name): value for name, value in rewards.items()}
            exception = data.get("exception_info")
            if isinstance(exception, dict):
                return {"exception": exception.get("exception_type")}
    return {}


def run(task: str, name: str, agent: list[str], env: dict[str, str]) -> dict[str, object]:
    """One Harbor job of one trial. A job left unfinished by an earlier run is set aside and run again."""
    job = JOBS / name
    if finished(job):
        print(f"{name}: done already, {reward(job)}")
        return reward(job)
    if job.exists():
        job.rename(job.with_name(f"{name}.unfinished-{time.strftime('%Y%m%dT%H%M%S')}"))
    uvx = shutil.which("uvx")
    if uvx is None:
        sys.exit("ERROR uvx is not on PATH: install uv, https://docs.astral.sh/uv/")
    command = [uvx, "--from", HARBOR, "harbor", "run", "--path", str(TASKS / task), "--jobs-dir", str(JOBS), "--job-name", name, "-n", "1", "-k", "1", "-y", "-q", *agent]
    started = time.monotonic()
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    job.mkdir(parents=True, exist_ok=True)
    (job / "harbor-output.txt").write_text(done.stdout + done.stderr, encoding="utf-8")
    outcome = reward(job)
    print(f"{name}: harbor exit {done.returncode} in {time.monotonic() - started:.0f}s, {outcome or 'no result: see ' + str(job / 'harbor-output.txt')}", flush=True)
    return outcome


def validate(args: Validate) -> int:
    """Each task's nop run must fail and its oracle run must pass, or the task cannot be trusted."""
    broken: list[str] = []
    for task in args.tasks:
        nop = run(task, f"nop__{task}", ["--agent", "nop"], harbor_env(None))
        oracle = run(task, f"oracle__{task}", ["--agent", "oracle"], harbor_env(None))
        for agent, outcome, expected in (("nop", nop, 0), ("oracle", oracle, 1)):
            if "reward" not in outcome:
                broken.append(f"{task}: the {agent} run ended without a reward ({outcome.get('exception', 'no result')}): read evals/jobs/{agent}__{task}, delete it, and run again")
            elif outcome["reward"] != expected:
                broken.append(f"{task}: the {agent} run's reward is {outcome['reward']}, not {expected}: fix the task's tests or solution before trusting it")
    for problem in broken:
        print(f"ERROR {problem}")
    return 1 if broken else 0


def trials(args: Trials) -> int:
    if not args.token.exists():
        print(f"ERROR no subscription token at {args.token}: run `claude setup-token`, which signs in to the subscription and prints a long-lived token, and save it there, mode 600")
        return 1
    env = harbor_env(args.token.read_text(encoding="utf-8").strip())
    agent = ["--agent", "claude-code", "--model", f"anthropic/{args.model}", "--ak", f"version={CLAUDE_CODE}", "--ak", f"reasoning_effort={args.effort}", "--ak", "disable_web_search=true"]
    for trial in range(1, args.trials + 1):
        for task in args.tasks:
            run(task, f"claude-code__{task}__{trial}", agent, env)
    return 0


def parse(argv: list[str]) -> Validate | Trials:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (commands.add_parser("validate"), commands.add_parser("trials")):
        command.add_argument("--tasks", nargs="+", choices=tasks(), default=tasks())
    trial = commands.choices["trials"]
    trial.add_argument("--trials", type=int, default=3, help="trials per task (default 3)")
    trial.add_argument("--model", default="claude-sonnet-5-5", help="the model Claude Code runs (default claude-sonnet-5-5)")
    trial.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"], help="Claude Code's effort (default high)")
    trial.add_argument("--token", type=Path, default=TOKEN, help=f"the file holding a `claude setup-token` token (default {TOKEN})")
    parsed = parser.parse_args(argv)
    if parsed.command == "validate":
        return Validate(tasks=parsed.tasks)
    return Trials(tasks=parsed.tasks, trials=parsed.trials, model=parsed.model, effort=parsed.effort, token=parsed.token)


def main() -> int:
    JOBS.mkdir(exist_ok=True)
    match parse(sys.argv[1:]):
        case Validate() as args:
            return validate(args)
        case Trials() as args:
            return trials(args)
        case never:
            assert_never(never)


if __name__ == "__main__":
    sys.exit(main())
