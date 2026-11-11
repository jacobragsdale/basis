"""`uv run python -m basis.api.example` writes the example's `openapi.json` beside its service, as `basis sync` writes an
API repo's: the sample basis-ui generates its client from and tests against, which `tests/test_openapi.py` checks; and
`coupons.arrows`, a sample of a dataset's Arrow stream, which it checks too."""

from pathlib import Path

from basis.api import contract, dev, fakes, openapi
from basis.api.app import build
from fastapi.testclient import TestClient

from basis.api.example import resources, service

SAMPLE = Path(__file__).parent / openapi.FILE
RELEASED = Path(__file__).parent / contract.RELEASED
"""The sample as basis's last release gave it, which `scripts/release.py version` writes, and its tests compare with."""

ARROW = Path(__file__).parent / "coupons.arrows"
"""A sample Arrow stream, the coupons of the fake warehouse's US912810TM64 as the app answers them, with each kind of
display format in its fields' metadata: what parallax, basis-ui's grid, reads, which basis-ui pins with the sample."""


def arrow_sample() -> bytes:
    """The sample stream, as the example's app answers it on its own fakes."""
    app = build(service.service, settings=dev.settings(service.service, resources.Settings.given()), fakes=dev.fakes(service.service) | {resources.SOLVE: fakes.Compute()})
    with TestClient(app.asgi) as client:
        answer = client.get("/bonds/US912810TM64/coupons", headers={"accept": "application/vnd.apache.arrow.stream"})
    answer.raise_for_status()
    return answer.content


if __name__ == "__main__":
    SAMPLE.write_text(openapi.text(service.service), encoding="utf-8", newline="")
    ARROW.write_bytes(arrow_sample())
