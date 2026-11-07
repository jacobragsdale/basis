"""`CREATE TABLE` statements for a job's tables, from their rows, and for the ledger: basis-core's (`basis.core.ddl`),
which every part that publishes shares, given a job's `Target`."""

from basis.core.ddl import create_table, ledger

__all__ = ["create_table", "ledger"]
