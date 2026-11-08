# {repo}

ETL jobs on [basis-job](https://github.com/jacobragsdale/basis/tree/main/packages/job). Each job is a folder in `jobs/`.

## Start

```sh
uv sync
uv run pytest                      # every job's contract tests, on its fixture: no database needed
uv run python jobs/{job}/main.py   # run a job on its fixture, from your IDE or here; it never writes to a database
```

`uv run basis --help` lists every command this repo has, `uv run basis check` runs every check a commit must pass,
`uv run basis docs` lists the framework's docs, and `uv run basis docs job/tutorial` prints the tutorial. The hooks in
`.githooks/`, which `basis check` turns on, run `basis check` on every commit and `basis check --full` on each commit
pushed, and `uv run basis review` prints what the push's check found, for a pull request's description. A Debian
package the image needs goes in `pyproject.toml`'s `[tool.basis] apt-packages`, and `uv run basis sync` writes it
into the Dockerfile.

## Where each change goes

| To | Edit | Guide |
|---|---|---|
| Add a column | the row in `jobs/JOB/rows.py`, then the SQL or the transform | `basis docs job/reference` |
| Read another table | a `Query` in `Inputs`, its SQL in `jobs/JOB/sql/`, a file in the fixture | `basis docs job/reference` |
| Read from another service | a `Fetch` in `Inputs`, its `BASIS_HTTP__NAME__URL` in `.env.example` and each environment's settings, a file in the fixture | `basis docs job/reference` |
| Use the time or new ids | a `Clock` or `Ids` in `Inputs`, and the fixture's `now` | `basis docs job/reference` |
| Write another table | a `Table` in `Outputs`, naming its write mode | `basis docs job/reference` |
| Change what the job computes | `transform` in `jobs/JOB/job.py`, and its tests | `basis docs job/reference` |
| Add a reason not to write | `check` in `jobs/JOB/job.py` | `basis docs job/reference` |
| Add a job | `uv run basis new job NAME` | `basis docs job/reference` |

## Environments

Every command that reaches the firm's systems names its environment, `--env dev`, `qa` or `prod`, and reads your own
settings for it: `~/.config/basis/ENV.env`, or `%APPDATA%\basis\ENV.env` on Windows, which every repo you work in
shares, with the names `.env.example` lists. A desktop writes in dev and QA; prod is read-only from a desktop.

## Production

The image runs `basis run JOB --params jobs/JOB/params/default.json --interval START..END --publish`, and its settings
are the variables `.env.example` lists, which its deployment sets, with `BASIS_ENV=prod` and `BASIS_PROD_WRITES=1`. Build it with the commit, which every run records:
`docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .`. `azure-pipelines.yml` builds and pushes it from `main`,
once basis's release fills in the firm's pipeline templates.
