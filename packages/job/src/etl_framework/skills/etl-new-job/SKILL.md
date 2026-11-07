---
name: etl-new-job
description: Make a new ETL job in an etl-framework repo, from its tables to its first passing contract tests. Use when the user wants a new job, pipeline or load added to the repo.
---

# Make a job

1. Run `uv run etl new job NAME`. It copies the example into `jobs/NAME/` and lists it in `pyproject.toml`.
2. Read `uv run etl docs reference` once: it is the contract.
3. In `jobs/NAME/rows.py`, declare one row per table the job reads or writes. Ask for the real column types: every
   `Decimal` needs its precision and scale, every `datetime` its zone, every nullable column `| None`.
4. In `jobs/NAME/job.py`: a `Query` per input, with its SQL in `sql/`, binding only the interval; a `Table` per
   output, with its key and its write mode, which the user must choose (see the reference's table of write modes). Never pick a
   write mode for them by guessing.
5. Write the transform and its tests in `jobs/NAME/tests/`, with `etl_framework.testing.frame` and `inputs`.
6. Replace the fixture in `jobs/NAME/tests/fixture/` with a few real-looking rows per dataset, and set its interval
   in `fixture.json`.
7. Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run ty check`; fix every finding.
8. Run `uv run etl ddl NAME --dialect mssql` (or oracle) and show the user the tables the job needs, if they do not
   exist yet.
