"""The numbers of `evals/results/report.md`, made from the recorded trials: the pass rate by model, how consistently
each task passes, the loop time, the tokens, and what the trials added to the checks' exceptions. The report's prose,
what the numbers show, is written by hand around them; `write` replaces only what lies between `BEGIN` and `END`."""

import statistics
from collections.abc import Callable, Iterable
from pathlib import Path

from basis_eval.trials import Trial

BEGIN = "<!-- numbers: `uv run evals/run.py report` writes from here -->"
END = "<!-- numbers end -->"


def _median(values: Iterable[float]) -> float | None:
    found = list(values)
    return statistics.median(found) if found else None


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value / 60:.1f} min" if value >= 120 else f"{value:.0f} s"  # noqa: PLR2004 -- minutes from two of them


def _thousands(value: float | None) -> str:
    return "-" if value is None else f"{value / 1000:,.0f}k"


def numbers(trials: list[Trial]) -> str:
    """The report's tables, in Markdown."""
    if not trials:
        return "No trials are recorded yet.\n"
    models = sorted({trial.model for trial in trials})
    lines = [*_setup(trials, models), *_pass_rate(trials, models), *_by_task(trials, models), *_loop(trials, models), *_failures(trials), *_added(trials)]
    return "\n".join(lines) + "\n"


def _header(models: list[str]) -> list[str]:
    return ["| | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]


def _setup(trials: list[Trial], models: list[str]) -> list[str]:
    lines = ["### Setup", ""]
    lines += [f"- basis commit: {', '.join(f'`{commit[:12]}`' for commit in sorted({trial.basis_commit for trial in trials}))}"]
    lines += [f"- Claude Code: {', '.join(sorted({trial.claude_version or 'unknown' for trial in trials}))}"]
    for model in models:
        ids = sorted({name for trial in trials if trial.model == model for name in trial.agent.models})
        lines += [f"- `--model {model}`: {', '.join(f'`{name}`' for name in ids) or 'unknown'}"]
    first, last = min(trial.started_at for trial in trials), max(trial.started_at for trial in trials)
    return [*lines, f"- Run from {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC", ""]


def _pass_rate(trials: list[Trial], models: list[str]) -> list[str]:
    lines = ["### Pass rate", "", *_header(models)]
    for name in ("Trials passed", "Ordinary tasks", "Adversarial tasks"):
        cells = []
        for model in models:
            kept = [trial for trial in trials if trial.model == model and (name == "Trials passed" or trial.adversarial == (name == "Adversarial tasks"))]
            passed = sum(trial.passed for trial in kept)
            cells.append(f"{passed}/{len(kept)} ({passed / len(kept):.0%})" if kept else "-")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    cells = []
    for model in models:
        groups = [[trial.passed for trial in trials if trial.model == model and trial.task == task] for task in sorted({t.task for t in trials if t.model == model})]
        every, none = sum(all(group) for group in groups), sum(not any(group) for group in groups)
        cells.append(f"{every} every trial, {len(groups) - every - none} some, {none} none")
    return [*lines, "| Tasks passed in | " + " | ".join(cells) + " |", ""]


def _by_task(trials: list[Trial], models: list[str]) -> list[str]:
    lines = ["### By task", "", "Passes out of trials, then the median agent time and tokens of the task's trials.", ""]
    lines += ["| Task | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for task in sorted({trial.task for trial in trials}):
        cells = []
        for model in models:
            kept = [trial for trial in trials if trial.task == task and trial.model == model]
            passed = sum(trial.passed for trial in kept)
            cells.append(f"{passed}/{len(kept)}, {_seconds(_median(t.agent.seconds for t in kept))}, {_thousands(_median(t.agent.tokens.total for t in kept))}" if kept else "-")
        marker = " (adversarial)" if any(trial.adversarial for trial in trials if trial.task == task) else ""
        lines.append(f"| `{task}`{marker} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def _loop(trials: list[Trial], models: list[str]) -> list[str]:
    rows: dict[str, Callable[[list[Trial]], str]] = {
        "Loop time, a passing trial's agent, median": lambda kept: _seconds(_median(t.agent.seconds for t in kept if t.passed)),
        "Loop time, slowest passing trial": lambda kept: _seconds(max((t.agent.seconds for t in kept if t.passed), default=None)),
        "`basis check` in grading, median": lambda kept: _seconds(_median(t.grade.check.seconds for t in kept if t.grade is not None)),
        "Turns, median": lambda kept: str(_median(t.agent.turns for t in kept) or "-"),
        "Check runs by the agent, median": lambda kept: str(_median(t.agent.check_runs for t in kept) or "-"),
        "Tokens per trial, median (cache reads included)": lambda kept: _thousands(_median(t.agent.tokens.total for t in kept)),
        "Output tokens per trial, median": lambda kept: _thousands(_median(t.agent.tokens.output for t in kept)),
        "Tokens, all trials": lambda kept: _thousands(sum(t.agent.tokens.total for t in kept)),
        "API price of all trials (not charged)": lambda kept: f"${sum(t.agent.cost_usd for t in kept):,.2f}",
    }
    lines = ["### Loop time and tokens", "", *_header(models)]
    lines += [f"| {name} | " + " | ".join(cell([t for t in trials if t.model == model]) for model in models) + " |" for name, cell in rows.items()]
    return [*lines, ""]


def _failures(trials: list[Trial]) -> list[str]:
    failed = [trial for trial in trials if not trial.passed]
    if not failed:
        return ["### Why trials failed", "", "None did.", ""]
    lines = ["### Why trials failed", "", "| Trial | Failed | Details |", "|---|---|---|"]
    lines += [f"| {trial.model} `{trial.task}` #{trial.trial} | {_failed(trial)} | {_cell(_details(trial))} |" for trial in sorted(failed, key=lambda t: (t.model, t.task, t.trial))]
    return [*lines, ""]


KINDS = ("suppressions", "skips", "patches", "removed_tests", "weakened")


def _added(trials: list[Trial]) -> list[str]:
    lines = ["### What the changes added to the checks' exceptions", ""]
    added = [(trial, kind, line) for trial in trials if trial.grade is not None for kind in KINDS for line in getattr(trial.grade.honesty, kind)]
    if not added:
        return [*lines, "Nothing: no trial added a suppression, a skip or a patch, removed a test, or weakened `[tool.basis]`."]
    lines += ["| Trial | What | Line |", "|---|---|---|"]
    return lines + [f"| {trial.model} `{trial.task}` #{trial.trial} | {kind.replace('_', ' ')} | `{_cell(line)}` |" for trial, kind, line in added]


def _failed(trial: Trial) -> str:
    if trial.grade is None:
        return "not graded"
    steps = {"honesty": trial.grade.honest, "protected files": trial.grade.protected, "basis check": trial.grade.check, "hidden tests": trial.grade.hidden}
    found = [name for name, step in steps.items() if not step.ok]
    if trial.agent.error is not None:
        found.insert(0, f"agent: {trial.agent.error}")
    return ", ".join(found)


def _details(trial: Trial) -> str:
    if trial.grade is None:
        return trial.failure or ""
    steps = (trial.grade.honest, trial.grade.protected, trial.grade.check, trial.grade.hidden)
    return "; ".join(detail for step in steps if not step.ok for detail in step.details[:3])[:300]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("`", "'")


def write(report: Path, trials: list[Trial]) -> None:
    """Replace the numbers in `report`, keeping the prose around them; a report without the markers gets them at its
    end."""
    text = report.read_text(encoding="utf-8") if report.is_file() else "# The eval's report\n\n"
    block = f"{BEGIN}\n\n{numbers(trials)}\n{END}"
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        text = head + block + rest.split(END, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    report.write_text(text, encoding="utf-8")
