"""Step 5.7 against Azurite, the storage emulator, and a real SQL Server ledger: a lake signed in to by an account key
and by a SAS token, an interval's partition written under its hidden name and read back through pyarrow's Azure
client, and the rename refused, since Azurite has no hierarchical namespace, as a flat account's would be: the
archive fails loudly, leaves nothing behind in the lake, and its ledger row rolls back. The rename itself needs a real
ADLS Gen2 account with the namespace on, which only the firm has (docs/spikes/data-lake.md, the (work) checklist)."""

import base64
import hashlib
import hmac
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import pyarrow.fs as pafs
import pytest
from integration.scratch import Scratch, configured, missing, mssql

import basis.job as etl
from basis.core import lake
from basis.core.settings import GivenSettings, LakeSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job
from basis.job.testing import frame

pytestmark = pytest.mark.integration

ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
"""Azurite's own account and its published key, which reach nothing but the emulator."""


class Trade(etl.Row):
    trade_id: Annotated[str, etl.MaxLength(12)]
    price: float


@pytest.fixture
def emulator() -> str:
    if (found := configured().azurite) is None:
        missing("set BASIS_TEST_AZURITE to a scratch Azurite's HOST:PORT (see tests/integration/README.md)")
    return found


@pytest.fixture
def container(emulator: str) -> Iterator[tuple[str, pafs.FileSystem, str]]:
    """A scratch container, which basis never creates, made and deleted by the test."""
    name = f"archive-{uuid.uuid4().hex[:8]}"
    fs = lake.filesystem(LakeSettings(account=ACCOUNT, container=name, auth="key", secret=KEY, emulator=emulator))
    fs.create_dir(name)
    yield name, fs, emulator
    fs.delete_dir(name)


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    found = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    yield found
    found.drop(found.ledger())


def _sas() -> str:
    """An account SAS for Azurite, signed as Azure signs one, so the test needs no SDK of its own."""
    expiry = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    version, permissions, services, types, protocols = "2021-08-06", "racwdl", "b", "sco", "https,http"
    signed = f"{ACCOUNT}\n{permissions}\n{services}\n{types}\n\n{expiry}\n\n{protocols}\n{version}\n\n"  # no start, IP or encryption scope
    signature = base64.b64encode(hmac.new(base64.b64decode(KEY), signed.encode(), hashlib.sha256).digest()).decode()
    return f"sv={version}&ss={services}&srt={types}&sp={permissions}&se={quote(expiry)}&spr={quote(protocols)}&sig={quote(signature)}"


@pytest.mark.parametrize("auth", ["key", "sas"])
def test_an_archive_to_a_flat_account_is_refused_at_the_rename_and_leaves_nothing_behind(
    auth: str, container: tuple[str, pafs.FileSystem, str], scratch: Scratch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    name, fs, emulator = container

    class Outputs(etl.Outputs):
        trades = etl.Lake(Trade, lake="history", table="trades", partition="date", ledger="warehouse")

    def transform(inputs: etl.Inputs, params: etl.Params) -> Outputs:
        del inputs, params
        return Outputs(trades=frame(Trade, *({"trade_id": f"T{index}", "price": index / 100} for index in range(1000))))

    job = Job("trades", "tests.trades", etl.Params, etl.Inputs, Outputs, transform, None, Path(__file__).parent)
    secret = KEY if auth == "key" else _sas()
    history = {"account": ACCOUNT, "container": name, "auth": auth, "secret": secret, "emulator": emulator}
    settings = GivenSettings.model_validate({"databases": {"warehouse": scratch.settings}, "lakes": {"history": history}})
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    request = runs.Request(tmp_path / "params.json", etl.Interval.parse("2026-11-05..2026-11-06"), publish=True)
    result = runs.run(job, settings, request, snapshot_root=Root.at(tmp_path / "snapshots"))
    archived = result.record.archived["trades"]
    assert (result.record.status, archived.status) == ("partial", "failed"), result.record
    assert f"LakeError: the lake history's account {ACCOUNT} has no hierarchical namespace, which the archive needs so that a rename is atomic" in (archived.error or "")
    left = [info.path for info in fs.get_file_info(pafs.FileSelector(name, recursive=True, allow_not_found=True)) if info.type == pafs.FileType.File]
    assert [path for path in left if "/." not in path] == []  # no reader of the table sees a file
    if auth == "key":  # Azurite's batch delete answers a SAS with a 500, so the hidden temporary files stay then
        assert left == []
    assert "sig=" not in caplog.text
    assert "sig=" not in (archived.error or "")
    assert scratch.rows(f"SELECT run_id FROM {scratch.ledger()}") == []
