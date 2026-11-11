"""What every release of basis stored outside a run's store, which the code of the day must read: the record of
`basis check --full`, which `basis review` reads, and the ledger, whose tables outlive the code that made them
(`docs/surface/`)."""

import json
from pathlib import Path

import pytest

from basis.core import ddl, review, snapshots
from basis.core.errors import BasisError
from basis.core.storage import Root

SURFACE = Path(__file__).resolve().parents[3] / "docs" / "surface"
RELEASES = sorted(path for path in SURFACE.iterdir() if (path / "check.json").is_file())


@pytest.mark.parametrize("release", RELEASES, ids=lambda path: path.name)
def test_a_check_record_every_release_wrote_is_read(release: Path) -> None:
    assert review.read(release / "check.json").commit == "0" * 40


@pytest.mark.parametrize("release", RELEASES, ids=lambda path: path.name)
@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_ledger_is_the_table_every_release_made(release: Path, dialect: str) -> None:
    assert ddl.ledger("etl.publishes", dialect) == (release / f"ledger.{dialect}.sql").read_text(encoding="utf-8")  # ty: ignore[invalid-argument-type] -- each dialect's name


def test_a_check_record_a_newer_basis_wrote_is_refused_with_the_upgrade(tmp_path: Path) -> None:
    kept = tmp_path / "check.json"
    kept.write_text(json.dumps({"format": review.FORMAT + 1, "commit": "x", "something": "new"}), encoding="utf-8")
    with pytest.raises(BasisError, match=rf"is in format {review.FORMAT + 1}, which a newer basis wrote: upgrade basis, as `uv run basis upgrade` does"):
        review.read(kept)


def test_a_manifest_a_newer_basis_wrote_is_refused_with_the_upgrade(tmp_path: Path) -> None:
    [manifest] = (SURFACE / "0.1.0" / "store").glob("runs/job/*/*.json")
    target = tmp_path / "store" / manifest.relative_to(SURFACE / "0.1.0" / "store")
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(json.loads(manifest.read_text(encoding="utf-8")) | {"format": snapshots.FORMAT + 1, "something": "new"}), encoding="utf-8")
    with pytest.raises(BasisError, match=rf"is in format {snapshots.FORMAT + 1}, which a newer basis wrote: upgrade basis, as `uv run basis upgrade` does"):
        snapshots.find(Root.at(tmp_path / "store"), manifest.stem)
