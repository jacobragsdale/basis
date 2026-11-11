"""One trial, and the record of each: its task, model and number, what the agent did and how it was graded, with the
basis commit and Claude Code version it ran on. Records are JSON lines in `evals/results/trials.jsonl`, appended as
each trial ends, so a stopped run resumes at the trials it has not recorded."""

import json
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from basis_eval import agent, grade, repo
from basis_eval.agent import Run
from basis_eval.grade import Grade
from basis_eval.tasks import Task

RESULTS = Path(__file__).resolve().parent.parent / "results"
TRIALS = RESULTS / "trials.jsonl"
TRANSCRIPTS = RESULTS / "transcripts"
_WRITING = threading.Lock()


class Trial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    model: str
    trial: int
    """Its number, from 1, among the task's trials with this model."""
    started_at: datetime
    basis_commit: str
    claude_version: str | None
    adversarial: bool
    agent: Run
    grade: Grade | None
    """None when the repo could not be made or graded, which `failure` says."""
    failure: str | None = None

    @property
    def passed(self) -> bool:
        return self.grade is not None and self.grade.passed


def recorded(path: Path = TRIALS) -> list[Trial]:
    if not path.is_file():
        return []
    return [Trial.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record(trial: Trial, path: Path = TRIALS) -> None:
    with _WRITING:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(trial.model_dump(mode="json"), separators=(",", ":")) + "\n")


def scratch(prefix: str) -> Path:
    """A folder of its own under `$TMPDIR`, which is on disk on this host, not in memory."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def run(task: Task, model: str, number: int, *, commit: str, version: str | None) -> Trial:
    """One trial: a fresh repo, the agent, the grade, and the repo deleted."""
    folder = scratch(f"eval-{task.name}-{number}-")
    started = datetime.now(UTC)
    trial_env = repo.environment(folder / "tmp")
    (folder / "tmp").mkdir()
    ran = Run()
    try:
        made = repo.make(task, folder / "repo", trial_env)
        ran = agent.run(task.instruction, model, made, trial_env, TRANSCRIPTS / model / f"{task.name}-{number}.jsonl")
        graded, failure = grade.grade(task, made, trial_env), None
    except (repo.RepoError, OSError, ValueError) as error:
        graded, failure = None, str(error)[-2000:]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return Trial(
        task=task.name,
        model=model,
        trial=number,
        started_at=started,
        basis_commit=commit,
        claude_version=ran.version or version,
        adversarial=task.spec.adversarial,
        agent=ran,
        grade=graded,
        failure=failure,
    )


def validate(task: Task) -> list[str]:
    """Why `task` cannot be trusted, if it cannot: its untouched repo must fail its grade, and its reference must pass."""
    problems = []
    for solved in (False, True):
        folder = scratch(f"eval-validate-{task.name}-")
        trial_env = repo.environment(folder / "tmp")
        (folder / "tmp").mkdir()
        try:
            made = repo.make(task, folder / "repo", trial_env)
            if solved:
                repo.solve(task, made, trial_env)
            graded = grade.grade(task, made, trial_env)
        except (repo.RepoError, OSError, ValueError) as error:
            problems.append(f"{'its reference' if solved else 'its untouched repo'} could not be graded: {error}")
            continue
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if solved and not graded.passed:
            problems.append(f"its reference fails: {_why(graded)}")
        if not solved and graded.passed:
            problems.append("its untouched repo passes, so the task asks for nothing the grade can see")
    return problems


def _why(graded: Grade) -> str:
    steps = {"honest": graded.honest, "protected": graded.protected, "check": graded.check, "hidden": graded.hidden}
    return "; ".join(f"{name}: {', '.join(step.details) or 'failed'}\n{step.tail}" for name, step in steps.items() if not step.ok)
