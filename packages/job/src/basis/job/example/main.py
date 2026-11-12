"""Run this job: press Run. It runs on its fixture, without any database; the run's snapshot is saved in the repo's
`snapshots/`.

To run it in dev, reading the databases your own dev.env configures, and writing nothing to them:
    run(job, params="params/default.json", interval="2026-11-05..2026-11-06", env="dev")
To run a saved run's transform again with the code you have now, and see what changed:
    from basis.job import replay
    replay(job, "0193f2...", env="prod")
"""

from basis.job import run

from basis.job.example import job

if __name__ == "__main__":
    result = run(job, params="params/default.json", data="tests/fixture")
