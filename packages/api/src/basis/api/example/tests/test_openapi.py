"""The example's `openapi.json`, which this repo publishes as the sample basis-ui generates its client from and tests
against: it is the document the example's app serves, and it keeps its contract with the last release's."""

import json

from basis import api
from basis.api import contract, fakes, openapi
from fastapi.testclient import TestClient

from basis.api.example import dev, resources, service
from basis.api.example.__main__ import ARROW, RELEASED, SAMPLE, arrow_sample
from basis.api.example.tests.test_handlers import IDENTITY


def test_the_sample_is_the_examples_document() -> None:
    assert SAMPLE.read_text(encoding="utf-8") == openapi.text(service.service), "the sample is out of date: run `uv run python -m basis.api.example`, which writes it"


def test_the_sample_is_what_the_examples_app_serves() -> None:
    app = api.build(service.service, settings=resources.Settings.given(identity=IDENTITY), fakes=dev.fakes() | {resources.SOLVE: fakes.Compute()})
    with TestClient(app.asgi) as client:
        served = client.get("/openapi.json").json()
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert {"x-basis": sample["x-basis"], **served} == sample


def test_the_sample_keeps_its_contract_with_the_last_release() -> None:
    """The release's document, which `scripts/release.py version` writes beside the sample; with none yet, there is
    none to break."""
    contract.check(SAMPLE, RELEASED)  # raises when the sample breaks a UI built against that release


def test_the_arrow_sample_is_what_the_examples_app_answers() -> None:
    assert ARROW.read_bytes() == arrow_sample(), "the Arrow sample is out of date: run `uv run python -m basis.api.example`, which writes it"
