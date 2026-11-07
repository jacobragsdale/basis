# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Turn the Harbor jobs under evals/jobs/ into the eval's report: evals/results/report.md and trials.json.

    uv run evals/report.py

A trial passes when its reward is 1: every check in its task's tests/test.sh passed. Its loop time runs from the
agent's start to the end of the checks that passed, the wall-clock time from a task to a change every check accepts.
Its check suite is the repo's own checks alone (pytest, ruff check, ruff format and ty), the loop a commit hook runs.
Tokens are Claude Code's own count, as Harbor records it; the cost is what the same tokens would cost on the API,
which the subscription does not charge.
"""

import json
import re
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
JOBS = ROOT / "jobs"
RESULTS = ROOT / "results"
SUITE = ("pytest", "ruff_check", "ruff_format", "ty")
CHECKS = ("lock", *SUITE, "task", "untouched")
CHECK_COMMAND = re.compile(r"\b(pytest|ruff|ty check|pre-commit)\b")


@dataclass(frozen=True)
class Trial:
    agent: str
    task: str
    number: int
    reward: float | None
    failed: list[str]
    exception: str | None
    agent_s: float | None
    verifier_s: float | None
    loop_s: float | None
    suite_ms: int | None
    prompt_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    turns: int | None
    check_runs: int | None
    agent_version: str | None
    model: str | None

    @property
    def passed(self) -> bool:
        return self.reward == 1


def at(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def moment(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def span(start: object, end: object) -> float | None:
    begun, ended = moment(start), moment(end)
    return (ended - begun).total_seconds() if begun is not None and ended is not None else None


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def checks(trial: Path) -> dict[str, tuple[int, int]]:
    """Each check's exit status and milliseconds, from the verifier's checks.tsv."""
    path = trial / "verifier" / "checks.tsv"
    if not path.exists():
        return {}
    rows = (line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {name: (int(status), int(ms)) for name, status, ms in rows}


def stream(trial: Path) -> tuple[int | None, int | None]:
    """The agent's turns and how many of its shell commands ran a check, from Claude Code's stream-json output."""
    path = trial / "agent" / "claude-code.txt"
    if not path.exists():
        return None, None
    turns: int | None = None
    runs = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if at(event, "type") == "result":
            turns = integer(at(event, "num_turns"))
        content = at(event, "message", "content")
        if at(event, "type") == "assistant" and isinstance(content, list):
            commands = (text(at(block, "input", "command")) for block in content if at(block, "type") == "tool_use" and at(block, "name") == "Bash")
            runs += sum(1 for command in commands if command is not None and CHECK_COMMAND.search(command))
    return turns, runs


def trial(job: Path) -> Trial | None:
    agent, task, *rest = [*job.name.split("__"), ""][:3]
    found = sorted(job.glob("*/result.json"))
    if not task or not found:
        return None
    directory = found[0].parent
    result = read_json(found[0])
    rewards = at(result, "verifier_result", "rewards")
    timed = checks(directory)
    turns, runs = stream(directory)
    verifier_end = at(result, "verifier", "finished_at")
    reward = number(at(rewards, "reward"))
    suite = [timed[name][1] for name in SUITE if name in timed]
    return Trial(
        agent=agent,
        task=task,
        number=int(rest[0]) if rest and rest[0] else 1,
        reward=reward,
        failed=[name for name in CHECKS if isinstance(rewards, dict) and rewards.get(name) == 0],
        exception=text(at(result, "exception_info", "exception_type")),
        agent_s=span(at(result, "agent_execution", "started_at"), at(result, "agent_execution", "finished_at")),
        verifier_s=span(at(result, "verifier", "started_at"), verifier_end),
        loop_s=span(at(result, "agent_execution", "started_at"), verifier_end) if reward == 1 else None,
        suite_ms=sum(suite) if len(suite) == len(SUITE) else None,
        prompt_tokens=integer(at(result, "agent_result", "n_input_tokens")),
        cache_read_tokens=integer(at(result, "agent_result", "n_cache_tokens")),
        cache_write_tokens=integer(at(read_json(directory / "agent" / "trajectory.json"), "final_metrics", "extra", "total_cache_creation_input_tokens")),
        output_tokens=integer(at(result, "agent_result", "n_output_tokens")),
        cost_usd=number(at(result, "agent_result", "cost_usd")),
        turns=turns,
        check_runs=runs,
        agent_version=text(at(result, "agent_info", "version")),
        model=text(at(result, "config", "agent", "model_name")),
    )


def median(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def seconds(ms: float | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.1f}s"


def score(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def failures(t: Trial) -> str:
    return ", ".join([*t.failed, *([t.exception] if t.exception is not None else [])]) or "-"


def count(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def summary(trials: list[Trial]) -> list[str]:
    tasks = sorted({t.task for t in trials})
    passed = [t for t in trials if t.passed]
    every = [task for task in tasks if all(t.passed for t in trials if t.task == task)]
    never = [task for task in tasks if not any(t.passed for t in trials if t.task == task)]
    first = trials[0]
    return [
        f"Claude Code {first.agent_version or '?'}, model `{first.model or '?'}`, {plural(len(tasks), 'task')}, {plural(len(trials), 'trial')}.",
        "",
        "| | |",
        "|---|---|",
        f"| Pass rate | {len(passed)} of {len(trials)} trials ({len(passed) / len(trials):.0%}) |",
        f"| Tasks passed in every trial | {len(every)} of {len(tasks)} |",
        f"| Tasks passed in some trials only | {len(tasks) - len(every) - len(never)} of {len(tasks)} |",
        f"| Tasks never passed | {len(never)} of {len(tasks)}{': ' + ', '.join(never) if never else ''} |",
        f"| Median loop time, passing trials | {duration(median(t.loop_s for t in passed))} |",
        f"| Median check suite (pytest, ruff, ty) | {seconds(median(t.suite_ms for t in trials))} |",
        f"| Median prompt tokens per trial (cache reads) | {count(median(t.prompt_tokens for t in trials))} ({count(median(t.cache_read_tokens for t in trials))}) |",
        f"| Median output tokens per trial | {count(median(t.output_tokens for t in trials))} |",
        f"| Tokens in all trials, prompt and output | {count(sum(t.prompt_tokens or 0 for t in trials))} and {count(sum(t.output_tokens or 0 for t in trials))} |",
        f"| API price of those tokens, not charged on the subscription | ${sum(t.cost_usd or 0 for t in trials):.2f} |",
    ]


def per_task(trials: list[Trial]) -> list[str]:
    lines = [
        "| Task | Passed | Loop time, median (range) | Agent time, median | Prompt tokens, median | Output tokens, median | Checks the agent ran, median | Failed checks |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for task in sorted({t.task for t in trials}):
        mine = [t for t in trials if t.task == task]
        loops = [t.loop_s for t in mine if t.loop_s is not None]
        spread = f" ({duration(min(loops))} to {duration(max(loops))})" if len(loops) > 1 else ""
        failed = sorted({name for t in mine for name in failures(t).split(", ") if name != "-"})
        lines.append(
            f"| {task} | {sum(t.passed for t in mine)}/{len(mine)} | {duration(median(loops))}{spread} | {duration(median(t.agent_s for t in mine))} | "
            f"{count(median(t.prompt_tokens for t in mine))} | {count(median(t.output_tokens for t in mine))} | {count(median(t.check_runs for t in mine))} | {', '.join(failed) or '-'} |"
        )
    return lines


def row(t: Trial) -> str:
    return (
        f"| {t.task} #{t.number} | {score(t.reward)} | {failures(t)} | {duration(t.agent_s)} | "
        f"{duration(t.verifier_s)} | {duration(t.loop_s)} | {seconds(t.suite_ms)} | {count(t.prompt_tokens)} | {count(t.cache_read_tokens)} | "
        f"{count(t.cache_write_tokens)} | {count(t.output_tokens)} | {'-' if t.cost_usd is None else f'${t.cost_usd:.2f}'} | {count(t.turns)} | {count(t.check_runs)} |"
    )


def table(trials: list[Trial]) -> list[str]:
    header = [
        "| Trial | Reward | Failed | Agent | Verifier | Loop | Suite | Prompt | Cache read | Cache write | Output | API price | Turns | Check runs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    return header + [row(t) for t in sorted(trials, key=lambda t: (t.task, t.number))]


def outcome(t: Trial | None) -> str:
    if t is None:
        return "not run"
    return f"{score(t.reward)} ({failures(t)} failed)" if t.failed or t.exception is not None else score(t.reward)


def validation(runs: list[Trial]) -> list[str]:
    lines = ["| Task | nop (must fail) | oracle (must pass) | Oracle's check suite |", "|---|---|---|---|"]
    for task in sorted({t.task for t in runs}):
        nop = next((t for t in runs if t.task == task and t.agent == "nop"), None)
        oracle = next((t for t in runs if t.task == task and t.agent == "oracle"), None)
        lines.append(f"| {task} | {outcome(nop)} | {outcome(oracle)} | {seconds(oracle.suite_ms if oracle else None)} |")
    return lines


def main() -> None:
    found = [t for job in sorted(JOBS.iterdir()) if job.is_dir() for t in [trial(job)] if t is not None] if JOBS.exists() else []
    runs = [t for t in found if t.agent in {"nop", "oracle"}]
    trials = [t for t in found if t.agent not in {"nop", "oracle"}]
    lines = ["# Eval report", ""]
    if trials:
        lines += ["## Summary", "", *summary(trials), "", "## Tasks", "", *per_task(trials), "", "## Trials", "", *table(trials), ""]
    else:
        lines += ["No agent trials have run yet: `uv run evals/run.py trials`.", ""]
    lines += ["## Task validation", "", *validation(runs), ""]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (RESULTS / "trials.json").write_text(json.dumps([asdict(t) for t in found], indent=1) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
