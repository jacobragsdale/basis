"""Writing a run's changes: each database in one transaction, in name order, which commits only once every table the
run writes there, and a ledger row for each, is written. The first failure stops the rest, and its database rolls
back; a database that committed stays committed, and `basis resume` writes the others from the run's snapshot, the
same rows, not a fresh extract. The ledger, written in the same transaction as the data, says which databases a run
committed to, so a resume never writes one twice.

Inside each transaction, before writing, the table is described again and checked, and a table written by a plain
insert refuses a second publish of an overlapping interval, unless the run says `again`: every other write leaves the
table as one publish would."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from basis.core.databases import Counts, Database, LedgerEntry
from basis.core.destination import check_table, check_values
from basis.core.interval import Interval
from basis.core.logs import timed
from basis.core.snapshots import PublishRecord
from basis.core.tables import Changes, Target

log = logging.getLogger(__name__)


class PublishRefusedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Publication:
    run_id: str
    job: str
    interval: Interval | None
    version: str
    again: bool
    clock: Callable[[], datetime]


def publish(publication: Publication, targets: Mapping[str, Target], changes: Mapping[str, Changes], open_database: Callable[[str], Database]) -> dict[str, PublishRecord]:
    """Each database's outcome, in the order they were written; the first failure ends the list."""
    records: dict[str, PublishRecord] = {}
    for database in sorted({target.database for name, target in targets.items() if name in changes}):
        names = sorted(name for name, target in targets.items() if target.database == database and name in changes)
        try:
            with timed(log, "published", database=database, outputs=names) as fields:
                counts = _publish_database(publication, open_database(database), {name: targets[name] for name in names}, changes)
                fields["counts"] = {name: vars(found) for name, found in counts.items()}
        except Exception as error:
            log.exception("could not publish to %s; nothing there is committed", database)
            records[database] = PublishRecord(status="failed", at=publication.clock(), error=f"{type(error).__name__}: {error}")
            break
        records[database] = PublishRecord(status="committed", at=publication.clock(), counts={name: vars(found) for name, found in counts.items()})
    return records


def _publish_database(publication: Publication, database: Database, targets: Mapping[str, Target], changes: Mapping[str, Changes]) -> dict[str, Counts]:
    counts: dict[str, Counts] = {}
    with database.writing() as writer:
        if writer.published(publication.run_id):
            log.info("already published", extra={"database": database.name})
            return {}
        for name, target in targets.items():
            spec = writer.describe(target.table)
            if spec is None:
                msg = f"{target.table} does not exist in {database.name}: `basis ddl` prints a CREATE TABLE for it"
                raise PublishRefusedError(msg)
            if reasons := check_table(target, spec) + check_values(target, spec, changes[name]):
                raise PublishRefusedError(f"{name}: {target.table} changed since the run checked it:\n" + "\n".join(reasons))
            interval = publication.interval if not changes[name].rerun_safe and not publication.again else None
            if interval is not None and (runs := writer.overlapping(publication.job, name, interval)):
                msg = f"{name} appends, and runs {', '.join(runs)} already published an interval that overlaps {publication.interval}: pass --again to insert its rows a second time"
                raise PublishRefusedError(msg)
            counts[name] = writer.apply(target, changes[name], spec)
            entry = LedgerEntry(publication.run_id, publication.job, name, target.table, publication.interval, changes[name].rerun_safe, counts[name], publication.clock(), publication.version)
            writer.record(entry)
        writer.commit()
    return counts
