"""The e2e suite: each model's smoke case, run by the deployed image's `optim` against QA. `uv run --env-file .env.qa
pytest -m e2e` runs it; the default run leaves it out. optim's `optim_qa` fixture refuses to run unless every source and
destination is QA's, and the suite writes only to the allowlist in `pyproject.toml`. Each model decides the first three
portfolios it lists in QA; for a model that lists none, pick them with `optim_qa.portfolios` and a SELECT."""

from pathlib import Path

import pytest
from optim.models import listed
from optim.pytest_plugin import QA

REPO = Path(__file__).parents[2]
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("model", sorted(listed(REPO)))
def test_each_model_runs_decides_every_portfolio_and_replays(optim_qa: QA, model: str) -> None:
    folder = Path(*listed(REPO)[model].split(".")[:-1])  # models/MODEL, relative to the repo, as inside the image

    optim_qa.smoke(model, folder / "params" / "default.json", optim_qa.listed(model))
