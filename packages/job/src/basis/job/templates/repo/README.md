# {repo}

ETL jobs on [basis-job](https://github.com/jacobragsdale/basis/tree/main/packages/job). Each job is a folder in `jobs/`.

## Start

```sh
uv sync
uv run pytest                      # every job's contract tests, on its fixture: no database needed
cp .env.example .env               # then fill in the databases this machine reads
uv run python jobs/{job}/main.py   # run a job, from your IDE or here; it never writes to a database
```

`uv run basis --help` lists every command this repo has, `uv run basis check` runs every check a commit must pass,
`uv run basis docs` lists the framework's docs, and `uv run basis docs job/tutorial` prints the tutorial.

## Where each change goes

| To | Edit | Guide |
|---|---|---|
| Add a column | the row in `jobs/JOB/rows.py`, then the SQL or the transform | `basis docs job/reference` |
| Read another table | a `Query` in `Inputs`, its SQL in `jobs/JOB/sql/`, a file in the fixture | `basis docs job/reference` |
| Write another table | a `Table` in `Outputs`, naming its write mode | `basis docs job/reference` |
| Change what the job computes | `transform` in `jobs/JOB/job.py`, and its tests | `basis docs job/reference` |
| Add a reason not to write | `check` in `jobs/JOB/job.py` | `basis docs job/reference` |
| Add a job | `uv run basis new job NAME` | `basis docs job/reference` |

## Production

The image runs `basis run JOB --params jobs/JOB/params/default.json --interval START..END --publish`. Build it with the
commit, which every run records: `docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .`
