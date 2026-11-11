"""The example's `openapi.json`, which this repo publishes as the sample basis-ui generates its client from and tests
against: it is the document the example's app serves, and it keeps its contract with the last release's."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from basis import api
from basis.api import contract, fakes, openapi
from fastapi.testclient import TestClient

from basis.api.example import dev, resources, service
from basis.api.example.__main__ import SAMPLE
from basis.api.example.tests.test_handlers import IDENTITY

ROOT = Path(__file__).parents[7]
"""This repo's root, whose release tags are basis's: `v0.1.0` and on."""


def test_the_sample_is_the_examples_document() -> None:
    assert SAMPLE.read_text(encoding="utf-8") == openapi.text(service.service), "the sample is out of date: run `uv run python -m basis.api.example`, which writes it"


def test_the_sample_is_what_the_examples_app_serves() -> None:
    app = api.build(service.service, settings=resources.Settings.given(identity=IDENTITY), fakes=dev.fakes() | {resources.SOLVE: fakes.Compute()})
    with TestClient(app.asgi) as client:
        served = client.get("/openapi.json").json()
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert {"x-basis": sample["x-basis"], **served} == sample


def test_the_sample_keeps_its_contract_with_the_last_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI checks this repo out with its tags, so the last `v*` release is found; with none yet, there is none to break."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("this checkout is no git clone")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"):  # a hook's, or `git rebase --exec`'s
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S607 -- git from the PATH
    if shallow.stdout.strip() == "true":
        pytest.skip("this clone is shallow, so the last release may be missing from it: the checks job, which fetches every commit and tag, runs this")
    contract.check(ROOT, SAMPLE.relative_to(ROOT).as_posix(), "v*")  # raises when the sample breaks a UI built against that release
