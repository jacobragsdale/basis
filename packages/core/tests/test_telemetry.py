import logging

import pytest

from basis.core import logs, telemetry
from basis.core.telemetry import TelemetryError

BERTH = {"OTEL_SERVICE_NAME": "quote-board-api", "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=qa,service.version=3f1c2a9e,service.namespace=quote-board"}
"""What berth sets in a container of the app quote-board's service api, deployed to qa."""


def test_the_platform_s_names_replace_basis_s() -> None:
    assert telemetry.resource("quote-board", version="abc123", environment="qa", environ=BERTH) == {
        "service.name": "quote-board-api",
        "service.version": "3f1c2a9e",
        "deployment.environment.name": "qa",
        "service.namespace": "quote-board",
    }


def test_without_them_a_process_carries_basis_s_own_names() -> None:
    assert telemetry.resource("quote-board", version="abc123", environment="qa", environ={}) == {"service.name": "quote-board", "service.version": "abc123", "deployment.environment.name": "qa"}
    assert telemetry.service_name({"OTEL_SERVICE_NAME": "  "}) is None


def test_values_are_percent_decoded_and_blank_pairs_skipped() -> None:
    assert telemetry.attributes({"OTEL_RESOURCE_ATTRIBUTES": " team=fixed%20income , ,owner=a%2Cb"}) == {"team": "fixed income", "owner": "a,b"}


def test_an_attribute_that_is_not_key_value_is_refused_with_the_fix() -> None:
    with pytest.raises(TelemetryError, match=r"^OTEL_RESOURCE_ATTRIBUTES holds 'qa', which is not key=value: write it as key=value pairs, comma separated, as service.version=1.2.3$"):
        telemetry.attributes({"OTEL_RESOURCE_ATTRIBUTES": "qa"})


def test_a_process_deployed_to_one_environment_that_acts_in_another_is_refused() -> None:
    with pytest.raises(
        TelemetryError, match=r"^OTEL_RESOURCE_ATTRIBUTES says this process was deployed to qa, and it acts in prod: give the deployment prod's BASIS_ENV and settings, or deploy it to prod$"
    ):
        telemetry.resource("quote-board", version=None, environment="prod", environ=BERTH)


def test_a_record_that_names_its_service_names_the_platform_s(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    logs.setup()
    monkeypatch.setattr(logs, "_bound", {})
    logs.bind(service="quote-board")
    logging.getLogger("basis.test").info("served")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "quote-board-api")
    logging.getLogger("basis.test").info("served")
    with logs.fields(job="positions"):
        monkeypatch.setattr(logs, "_bound", {})
        logging.getLogger("basis.test").info("unnamed")
    records = [line for line in capsys.readouterr().err.splitlines() if '"basis.test"' in line]
    assert ['"service":"quote-board"' in records[0], '"service":"quote-board-api"' in records[1], '"service"' in records[2]] == [True, True, False]
