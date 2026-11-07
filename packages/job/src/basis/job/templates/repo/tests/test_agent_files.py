from pathlib import Path

from basis.job.testing import check_agent_files


def test_the_agent_files_match_the_installed_basis_job() -> None:
    check_agent_files(Path(__file__).parents[1])
