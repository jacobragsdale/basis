from pathlib import Path

import pytest

from basis.job import new, sync

CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in, which holds packages/core and packages/job."""


def test_a_new_repo_holds_its_job_listed_and_its_agent_files_current(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    assert (repo / "jobs/prices/job.py").read_text(encoding="utf-8").count("from jobs.prices.rows import") == 1
    assert 'prices = "jobs.prices.job"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert (repo / ".gitignore").exists()
    assert sync.stale(repo) == []


def test_a_new_job_is_listed_beside_the_first(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=CHECKOUT, install=False)
    new.job(repo, "rates", install=False)
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'rates = "jobs.rates.job"' in pyproject
    assert 'prices = "jobs.prices.job"' in pyproject


def test_an_edited_block_is_stale_until_synced(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "pricing-etl", "prices", index=None, framework_path=None, install=False)
    agents = repo / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8").replace("Rules to keep", "Rules"), encoding="utf-8")
    assert sync.stale(repo) == [agents]
    assert sync.write(repo) == [agents]
    assert sync.stale(repo) == []


@pytest.mark.parametrize("name", ["Prices", "1prices", "_prices", "pri-ces"])
def test_a_job_name_is_an_identifier(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="a job's name is lower-case"):
        new.repo(tmp_path / "pricing-etl", name, index=None, framework_path=None, install=False)
