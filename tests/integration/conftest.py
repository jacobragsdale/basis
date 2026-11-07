"""The integration tier's fixtures: a scratch database of each dialect, with a ledger of the test's own. Every test
makes its own tables, named with a suffix of its own, and drops them after."""

import uuid
from collections.abc import Iterator

import pytest
from integration.scratch import Scratch, configured, mssql, oracle


@pytest.fixture(params=["mssql", "oracle"])
def scratch(request: pytest.FixtureRequest) -> Iterator[Scratch]:
    """A scratch database of each dialect, with a ledger of the test's own, dropped after it."""
    suffix = uuid.uuid4().hex[:8]
    found = (mssql if request.param == "mssql" else oracle)(configured(), suffix).with_ledger()
    yield found
    found.drop(found.ledger(), *(f"{name}_{suffix}" for name in ("prices", "audit", "other")))


@pytest.fixture
def both() -> Iterator[tuple[Scratch, Scratch]]:
    suffix = uuid.uuid4().hex[:8]
    pair = mssql(configured(), suffix), oracle(configured(), suffix)
    yield pair
    for found in pair:
        found.drop(found.ledger(), f"prices_{suffix}")
