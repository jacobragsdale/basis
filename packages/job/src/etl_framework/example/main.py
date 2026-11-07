"""Run this job: press Run. It reads the databases `.env` configures and writes nothing to them; the run's snapshot
is saved in the repo's `snapshots/`.

To run it on its fixture, without any database:
    run(job, params="params/default.json", data="tests/fixture")
To run a saved run's transform again with the code you have now, and see what changed:
    replay(job, "0193f2...", env=".env.prod")
"""

from etl_framework import replay, run  # noqa: F401 -- replay is the docstring's other call

from etl_framework.example import job

if __name__ == "__main__":
    result = run(job, params="params/default.json", interval="2026-11-05..2026-11-06", env=".env")
