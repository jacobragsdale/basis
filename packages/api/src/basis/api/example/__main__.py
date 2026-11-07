"""`uv run python -m basis.api.example` writes the example's `openapi.json` beside its service, as `basis sync` writes an
API repo's: the sample basis-ui generates its client from and tests against, which `tests/test_openapi.py` checks."""

from pathlib import Path

from basis.api import openapi

from basis.api.example import service

SAMPLE = Path(__file__).parent / openapi.FILE

if __name__ == "__main__":
    SAMPLE.write_text(openapi.text(service.service), encoding="utf-8", newline="")
