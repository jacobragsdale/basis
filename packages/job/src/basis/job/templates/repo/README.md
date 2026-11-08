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

The image runs `basis run JOB --params jobs/JOB/params/default.json --interval INTERVAL --publish`, and its settings
are the variables `.env.example` lists, which its deployment sets, with `BASIS_ENV=prod` and `BASIS_PROD_WRITES=1`. A
scheduler whose command never changes gives a relative interval, such as `previous-business-day`, which the run
resolves in its job's `CALENDAR` and records as dates. Build the image with the commit, which every run records:
`docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .`. `azure-pipelines.yml` builds and pushes it from `main`,
once basis's release fills in the firm's pipeline templates.

## Deploying with berth

[berth](https://github.com/jacobragsdale/berth), the deploy repository the firm is trying, runs this repo's image from
`apps/APP/app.yaml` and a file for each environment, APP being the repo's name in berth. This spec fits what the image
runs; basis's `docs/spikes/berth.md` says why each line is as it is.

```yaml
# apps/APP/app.yaml
name: APP
description: WHAT_IT_DOES
owners: [OWNER@FIRM, OWNER@FIRM]
source: https://REPOSITORY_URL
azure_identity: true                  # the run store on the data lake, and SQL Server's ActiveDirectoryDefault
config:
  BASIS_TRACES__EXPORTER: otlp
  BASIS_TRACES__ENDPOINT: http://AGENT_SERVICE.AGENT_NAMESPACE.svc.cluster.local:4318/v1/traces  # the Datadog Agent's node-local Service
  BASIS_DATABASES__SOURCE__KIND: mssql
  BASIS_DATABASES__SOURCE__ISOLATION: snapshot
  BASIS_DATABASES__SOURCE__TIMEOUT_S: "300"
  BASIS_DATABASES__SOURCE__MAX_ROWS: "5000000"
secrets: [basis-databases--source]    # Key Vault APP--basis-databases--source: the database's secret parts, as JSON
workflows:
  {job}:
    orchestrator: airflow
    schedule: "0 6 * * 1-5"
    timezone: America/New_York
    tasks:
      run:
        args: [run, {job}, --params, jobs/{job}/params/default.json, --interval, previous-business-day, --publish]
        timeout: 1h
        retries: 1                    # a rerun replaces and upserts again; a job with an Append() table keeps 0, and is resumed
```

```yaml
# apps/APP/dev.yaml
version: COMMIT@sha256:DIGEST         # the image the pipeline tagged with its commit
config:
  BASIS_ENV: dev                      # berth sets BERTH_ENV; basis reads BASIS_ENV
  BASIS_SNAPSHOT_ROOT: abfs://runs@ACCOUNT.dfs.core.windows.net/APP
```

The secret `basis-databases--source` holds `{"connection_string": "..."}`, and a written database's also its `write`
block: berth's secret names are at most 40 characters, and one JSON value at the database's name merges with the
database's other variables. `prod.yaml` adds `BASIS_PROD_WRITES: "1"` where the job publishes. berth names each task's
container `<app>-<workflow>-<task>` in `OTEL_SERVICE_NAME`, which basis's spans and logs then carry.
