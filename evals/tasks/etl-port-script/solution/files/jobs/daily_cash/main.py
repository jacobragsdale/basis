"""Run this job: press Run. It reads the databases `.env` configures and writes nothing to them; the run's snapshot
is saved in the repo's `snapshots/`.

To run it on its fixture, without any database:
    run(job, params="params/default.json", data="tests/fixture")
"""

from etl_framework import run

from jobs.daily_cash import job

if __name__ == "__main__":
    result = run(job, params="params/default.json", interval="2026-11-05..2026-11-06", env=".env")
