---
name: job-new
description: Make a new ETL job in a basis-job repo, from its tables to its first passing contract tests. Use when the user wants a new job, pipeline or load added to the repo.
---

# Make a job

1. Run `uv run basis new job NAME`. It copies the example into `jobs/NAME/` and lists it in `pyproject.toml`.
2. Read `uv run basis docs job/reference` once: it is the contract.
3. In `jobs/NAME/rows.py`, declare one row per table the job reads or writes. Ask for the real column types: every
   `Decimal` needs its precision and scale, every `datetime` its zone, every nullable column `| None`.
4. In `jobs/NAME/job.py`: a `Query` per input from a database, with its SQL in `sql/`, or a `Fetch` per input from
   another service, each binding only the interval; a `Clock` or `Ids` where the transform needs the time or new ids,
   never `datetime.now()` or `uuid`; a `Table` per
   output, with its key and its write mode, which the user must choose (see the reference's table of write modes). Never pick a
   write mode for them by guessing.
5. Write the transform and its tests in `jobs/NAME/tests/`, with `basis.job.testing.frame` and `inputs`.
6. Replace the fixture in `jobs/NAME/tests/fixture/` with a few real-looking rows per dataset, and set its interval
   in `fixture.json`.
7. Run `uv run basis check`, and fix every `ERROR` it prints.
8. Run `uv run basis ddl NAME --dialect mssql` (or oracle) and show the user the tables the job needs, if they do not
   exist yet.
