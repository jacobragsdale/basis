# How basis-job is put together

basis-job owns the run loop that every ETL job shares, so that a job holds only what is particular to it: what it
reads, what it writes and how, and the transform between them. Reading and writing happen in the framework, around a
transform that is a pure function of frames. Most of the design follows from that split, and from refusing to assume
anything about a job's tables.

## Why the framework owns the loop

Without one, every job repeats the same plumbing: connections, queries, retries, logging, writes, and the checks
someone remembered to add. Each copy differs a little, which is why jobs fail on bad code and bad checks, are hard to
test, and are hard to see into. A library of helpers would not fix that, because the guarantees come from the order
of the steps, not from the helpers. Because the framework loads every input, job code does no I/O, and a run can be
replayed from what it read. Because every output passes the same checks before anything is written, a job cannot
forget one. Because every run logs the same records and saves the same snapshot, one dashboard and one set of
monitors cover every job.

The no-I/O rule is enforced rather than asked for. Each job's contract tests build its import graph and fail on any
chain from `job.py` to a database driver, an HTTP client, storage or the run loop, and the framework holds its own
modules to the same rule with an import contract. `main.py` is outside it, since it only calls `etl.run`.

## Nothing about a table is assumed

ETL jobs write every shape of table: appends by date, appends with no date, upserts, full refreshes, history tables,
tables in two databases at once. A framework that picked a default for any of these would be right for most jobs and
quietly wrong for the rest. So every output names its write mode, every `Decimal` its precision, every `datetime` the
zone its naive values are in, and every input what an empty result means. A declaration is a few more characters than a
default, and it is the one place a reader learns how a table is written.

The write modes are helpers over four operations, delete, upsert, insert of new keys and insert, which is all a
transaction against one table ever needs. `Manual()` hands the operations to the transform, for whatever the helpers do
not cover. A job that needs the table's current rows reads the table as an input, so they are in the snapshot, and a
replay sees what the run saw. The alternative, letting a job run its own SQL against the destination, would be the one
write a replay and a regression could not see.

## Reruns and the ledger

A run is retried after a failure, rerun after a fix, and sometimes run twice by mistake. Each database's writes commit
in one transaction, so a failed attempt leaves nothing behind, and a retry starts clean. A rerun of a run that succeeded
leaves the table as it was for every write mode but a plain append: a delete that covers the interval, an upsert and an
insert of new keys all converge. The checks hold that up: an insert that a `ReplaceWhere` delete does not cover would
be duplicated by a rerun, so the run refuses it.

A plain append cannot be made safe to repeat by any framework, since nothing in the row says which run inserted it.
The ledger handles it instead. Each database a job writes holds one ledger table, and every publish adds a row per
table in the same transaction as the data, so the ledger is never wrong about whether a publish happened. A second
append for an overlapping interval finds the first in the ledger and is refused, unless the run says `--again`. The
ledger is also the only DDL the framework asks for, once per database: runs never create or alter a table, and
`basis ddl` prints the statements for whoever has the rights.

## All or nothing, across databases

Within one database, a run is all or nothing: every check passes for every output before any database is touched, and
then each database's tables and ledger rows commit together. Across databases, true atomicity would need distributed
transactions, which the drivers do not make dependable. Instead, databases commit one at a time, in name order, and a
run that fails between two commits is `partial`. A run saves itself as `publishing` before its first commit, so one
stopped between two commits, as a killed process is, says so too. The ledgers say which databases have the run, and
`basis resume` writes the rest from the snapshot: the same rows, not a fresh extract, so the databases end up as if one
transaction had written them. Before it loads anything, a run that publishes checks that every ledger it writes
exists, so a missing one stops it before any database commits. Each publish of an output holds that output's lock
until it commits, an application lock on SQL Server and a row of the ledger's lock table on Oracle, so two runs of one
output take turns, and neither deletes or inserts beside the other.

## Exact values

Data that moves between systems has to arrive as it left. Frames are pandas, and every column is backed by Arrow, of
the type its row declares: integers with nulls stay integers, decimals keep their precision and scale, and datetimes
carry their zone, where numpy's types would make a nullable integer a float and a decimal an untyped object. The data
arrives as Arrow, from the drivers and Parquet, and leaves as Arrow, so a frame shares its buffers both ways. A row's
types are checked at every boundary, in both directions, and a conversion is allowed only where nothing is lost:
Arrow's safe casts refuse a rounded or truncated value, and the framework checks the rest itself, such as a time of
day that a cast to a date would drop. Floats never become decimals implicitly; the transform rounds, as the data
needs. The destination is checked too, against what the database says the table is: a string longer than its column,
a time finer than `DATE` or `DATETIME2(3)` keeps, a float a `REAL` would round, an identifier's text an integer column
would store as other text, such as `'00123'`, or an empty string, which Oracle stores as NULL, stops the run before the
database would change it or fail halfway.

## Snapshots, replay and regression

Every run saves what it read, what it would write, its params and its outcome, before it writes anything. A run that
fails keeps its inputs, so it can be replayed on a laptop with production's data and the code of the day, without
production credentials beyond reading the snapshot. A replay with other params reads the same snapshot, which is why a
query binds only the run's interval and never a param: a param that changed a query would need data the snapshot does
not hold. Each dataset is stored once, named by the hash of its content, so the inputs that a day's runs share, and
those a replay reruns, take no more space.

Regression is replay at scale: `basis regress` replays a job's recent published runs with a branch's code and diffs each
table's changes by key. A change that alters production output shows up as rows added, removed and changed, by
column, before it merges.

## Reading at one moment

A job that reads several tables from one database usually needs them consistent with each other: positions and the
trades behind them, read at the same moment. Each database's queries run in one transaction that only reads, and its
isolation is a setting rather than a default, because the trade-off differs by database. SQL Server's snapshot
isolation must be enabled on the database. Oracle's read-only transactions fail on a table whose definition changed in
the seconds before, which a `TRUNCATE` of a staging table does. `read_committed` gives up the single moment for either
problem.

For what each piece is called and every option, see the [reference](reference.md).
