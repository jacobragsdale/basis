from pathlib import Path

from etl_framework.testing import check_agent_files


def test_the_agent_files_match_the_installed_etl_framework() -> None:
    check_agent_files(Path(__file__).parents[1])
