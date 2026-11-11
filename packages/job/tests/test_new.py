import shutil
import subprocess
from pathlib import Path

import pytest

from basis.core.new import UNRELEASED, NoReleaseError
from basis.job import new, sync

GIT = shutil.which("git") or "git"
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in, which holds packages/core and packages/job."""


def test_a_new_repo_holds_its_job_listed_and_its_framework_and_agent_files_current(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    assert (repo / "jobs/prices/job.py").read_text(encoding="utf-8").count("from jobs.prices.rows import") == 1
    assert 'prices = "jobs.prices.job"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert all((repo / name).is_file() for name in (".gitignore", ".gitattributes", ".githooks/pre-commit", ".githooks/pre-push", "azure-pipelines.yml", "CLAUDE.md", ".claude/settings.json"))
    assert "#   a job: run JOB --params jobs/JOB/params/default.json --interval START..END --publish" in (repo / "Dockerfile").read_text(encoding="utf-8")
    assert sorted(path.name for path in (repo / ".claude/skills").iterdir()) == sorted(path.name for path in (repo / ".agents/skills").iterdir()) == ["job-new", "job-port", "job-why-failed"]
    assert not (repo / ".pre-commit-config.yaml").exists()
    assert sync.stale(repo) == []


def test_a_new_job_is_listed_beside_the_first(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    new.job(repo, "rates", install=False)
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'rates = "jobs.rates.job"' in pyproject
    assert 'prices = "jobs.prices.job"' in pyproject


def test_an_edited_block_is_stale_until_synced(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    agents = repo / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8").replace("Rules to keep", "Rules"), encoding="utf-8")
    assert sync.stale(repo) == [agents]
    assert sync.write(repo) == [agents]
    assert sync.stale(repo) == []


def test_a_block_that_etl_framework_wrote_is_replaced_not_joined_by_a_second(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    agents = repo / "AGENTS.md"
    synced = agents.read_text(encoding="utf-8")
    agents.write_text(synced.replace("<!-- basis-job:", "<!-- etl-framework:").replace("on basis-job 0.", "on etl-framework 0."), encoding="utf-8")
    assert sync.write(repo) == [agents]
    assert agents.read_text(encoding="utf-8") == synced


def test_a_repos_first_sync_removes_what_etl_framework_wrote_without_a_marker_and_nothing_of_the_repos_own(tmp_path: Path) -> None:
    """etl-framework 0.1.0 wrote its skills to `.agents/skills/etl-*` with no marker, and a test of the agent files;
    basis-job lists them as `retired`, so the first `basis sync` removes them, and leaves a skill of the repo's own."""
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    written = [repo / ".agents/skills" / name / "SKILL.md" for name in ("etl-new-job", "etl-port-job", "etl-why-failed")]
    own = repo / ".agents/skills/etl-team-notes/SKILL.md"
    for path in [*written, own]:
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {path.parent.name}\ndescription: As etl-framework 0.1.0 wrote it.\n---\n", encoding="utf-8")
    (repo / "tests/test_agent_files.py").write_text("def test_agent_files() -> None: ...\n", encoding="utf-8")
    assert sorted(path.relative_to(repo).as_posix() for path in sync.write(repo)) == [
        ".agents/skills/etl-new-job", ".agents/skills/etl-port-job", ".agents/skills/etl-why-failed", "tests/test_agent_files.py",
    ]  # fmt: skip
    assert own.is_file()
    assert sync.stale(repo) == []


def test_without_a_release_a_repo_needs_a_checkout_or_an_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(new, "VERSION", UNRELEASED)  # basis-job as installed from this checkout, before any release
    with pytest.raises(NoReleaseError, match="pass --framework-path"):
        new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=None, install=False)
    assert not (tmp_path / "pricing-etl").exists()


@pytest.mark.parametrize("name", ["Prices", "1prices", "_prices", "pri-ces"])
def test_a_job_name_is_an_identifier(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="is no job's name: give one of lower-case letters"):
        new.repo(tmp_path / "pricing-etl", name, index=None, framework_path=None, install=False)


@pytest.mark.parametrize("name", ["class", "yield"])
def test_a_job_name_that_is_a_keyword_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="is a Python keyword, which no import of the job's module can name"):
        new.repo(tmp_path / "pricing-etl", name, index=None, framework_path=None, install=False)
    assert list(tmp_path.iterdir()) == []


def test_a_job_copies_the_files_it_names_and_no_cache_or_stray_file_of_the_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = tmp_path / "example"
    shutil.copytree(new.EXAMPLE, example)
    (example / ".ruff_cache").mkdir(exist_ok=True)  # as a checkout's example holds once ruff has run in it
    (example / ".ruff_cache" / "CACHEDIR.TAG").write_text("", encoding="utf-8")
    (example / "notes.txt").write_text("a stray file\n", encoding="utf-8")
    monkeypatch.setattr(new, "EXAMPLE", example)
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    job = repo / "jobs" / "prices"
    assert sorted(path.relative_to(job).as_posix() for path in job.rglob("*") if path.is_file()) == sorted(new.EXAMPLE_FILES)


def test_the_files_a_job_copies_are_every_file_of_the_example_but_its_ruff_settings() -> None:
    tracked = subprocess.run([GIT, "ls-files"], cwd=new.EXAMPLE, capture_output=True, text=True, check=True).stdout.split()
    assert sorted(tracked) == sorted([*new.EXAMPLE_FILES, "ruff.toml"])
