# {repo}

ETL jobs on [basis-job](https://github.com/jacobragsdale/basis/tree/main/packages/job). Each job is a folder in `jobs/`.

## Start

```sh
uv sync
uv run pytest                      # every job's contract tests, on its fixture: no database needed
cp .env.example .env               # then fill in the databases this machine reads
uv run python jobs/{job}/main.py   # run a job, from your IDE or here; it never writes to a database
```

`uv run etl docs` lists the framework's docs, and `uv run etl docs tutorial` prints the tutorial.

## Where each change goes

| To | Edit | Guide |
|---|---|---|
| Add a column | the row in `jobs/JOB/rows.py`, then the SQL or the transform | `etl docs reference` |
| Read another table | a `Query` in `Inputs`, its SQL in `jobs/JOB/sql/`, a file in the fixture | `etl docs reference` |
| Write another table | a `Table` in `Outputs`, naming its write mode | `etl docs reference` |
| Change what the job computes | `transform` in `jobs/JOB/job.py`, and its tests | `etl docs reference` |
| Add a reason not to write | `check` in `jobs/JOB/job.py` | `etl docs reference` |
| Add a job | `uv run etl new job NAME` | `etl docs reference` |

## Production

The image runs `etl run JOB --params jobs/JOB/params/default.json --interval START..END --publish`. Build it with the
commit, which every run records: `docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .`
