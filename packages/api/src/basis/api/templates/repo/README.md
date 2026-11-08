# {repo}

An API, `{app}`, and the jobs beside it, on basis. Its code is in `{package}/`, in its layers: `service.py`, the
routes; `handlers.py`, one function per route; `repository.py`, every SQL statement and call to another service;
`domain.py`, the pure types and rules; `resources.py` and `errors.py`, what it declares. Its jobs are in `jobs/`.

```sh
uv run basis dev          # the API on its fakes, on this machine
uv run basis check        # the framework files, the hooks, the lockfile, ruff, ty, the gates and pytest
uv run basis new job NAME # a job, copied from basis-job's example
uv run basis docs         # basis's docs for this repo's parts
```

## Environments

Every command that reaches the firm's systems names its environment, `--env dev`, `qa` or `prod`, and reads your own
settings for it: `~/.config/basis/ENV.env`, or `%APPDATA%\basis\ENV.env` on Windows, with the names `.env.example`
lists. In a pod, the deployment sets them as the process's environment, `BASIS_ENV` among them.

## Deploying with berth

[berth](https://github.com/jacobragsdale/berth), the deploy repository the firm is trying, runs this repo's image from
`apps/{app}/app.yaml` and a file for each environment. This spec fits what the image runs; basis's
`docs/spikes/berth.md` says why each line is as it is.

```yaml
# apps/{app}/app.yaml
name: {app}
description: WHAT_IT_DOES
owners: [OWNER@FIRM, OWNER@FIRM]
source: https://REPOSITORY_URL
azure_identity: true                  # the run store on the data lake, and SQL Server's ActiveDirectoryDefault
talks_to: [pricing/api, ledger/api]   # each service it calls: opens the service's network policy to this app
config:
  BASIS_COMPUTE__WORKERS: "2"         # no default: one per CPU of the service's size
  BASIS_TRACES__EXPORTER: otlp
  BASIS_TRACES__ENDPOINT: http://AGENT_SERVICE.AGENT_NAMESPACE.svc.cluster.local:4318/v1/traces  # the Datadog Agent's node-local Service
  BASIS_DATABASES__WAREHOUSE__KIND: mssql
  BASIS_DATABASES__WAREHOUSE__ISOLATION: read_committed
  BASIS_DATABASES__WAREHOUSE__TIMEOUT_S: "5"
  BASIS_DATABASES__WAREHOUSE__MAX_ROWS: "100000"
secrets: [basis-databases--warehouse] # Key Vault {app}--basis-databases--warehouse: the database's secret parts, as JSON
services:
  api:
    args: [serve]
    port: 8000                        # BASIS_SERVE__PORT's default
    health: {path: /health/ready}     # both probes: 503 only while it starts and while it drains
workflows:
  relay:                              # the outbox's calls, made again until each is accepted, refused or aged out
    orchestrator: kubernetes
    schedule: "*/5 * * * *"
    tasks:
      relay: {args: [relay], timeout: 5m}
  {job}:
    orchestrator: airflow
    schedule: "0 6 * * 1-5"
    timezone: America/New_York
    tasks:
      run:
        args: [run, {job}, --params, jobs/{job}/params/default.json, --interval, previous-business-day, --publish]
        timeout: 1h
```

```yaml
# apps/{app}/dev.yaml
version: COMMIT@sha256:DIGEST         # the image the pipeline tagged with its commit
config:
  BASIS_ENV: dev                      # berth sets BERTH_ENV; basis reads BASIS_ENV
  BASIS_SNAPSHOT_ROOT: abfs://runs@ACCOUNT.dfs.core.windows.net/{app}
  BASIS_HTTP__PRICING__URL: http://api.pricing-dev.svc.cluster.local:8000   # what talks_to injects as PRICING_API_URL
  BASIS_HTTP__LEDGER__URL: http://api.ledger-dev.svc.cluster.local:8000
```

The secret `basis-databases--warehouse` holds `{"connection_string": "...", "write": {"connection_string": "...",
"ledger": "etl.publishes"}}`: berth's secret names are at most 40 characters, and one JSON value at the database's name
merges with the database's other variables. `qa.yaml` and `prod.yaml` are the same with their environment's names;
`prod.yaml` adds `BASIS_PROD_WRITES: "1"` only where the deployment writes. berth names each container's service
`<app>-<component>` in `OTEL_SERVICE_NAME`, which basis's spans and logs then carry, so Datadog shows its logs, traces and
metrics as one service. A job's `--interval previous-business-day` resolves in the job's `CALENDAR`.
