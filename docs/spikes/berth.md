# Spike 0.9: berth fit

Can a repo that `basis-api new app` makes deploy through berth, the firm's proposed deploy repository
(github.com/jacobragsdale/berth), as berth stands today? Mostly yes: the image, its user, its read-only root
filesystem, the probes and the scheduled relay fit with no change on either side. Seven things do not fit, and each has
a fix on basis's side below; berth is unchanged. Run on 2026-11-07, by reading berth's docs and its renderer and by
making a repo with the generator. Nothing was deployed: no cluster, Airflow or Control-M was reached.

## Versions

basis at fef6930 (main), with a repo made by `uv run basis-api new app quote-board --framework-path .`; berth 0.1.0 at
8cd8f4a, read from `docs/reference.md`, `docs/tutorial.md`, `src/berth/model.py` and `src/berth/render/` (`pod.py`,
`k8s.py`, `airflow.py`, `controlm.py`), with its `platform/platform.yaml` as the example platform; pydantic-settings as
basis's lock pins it, for the one setting checked by running it.

## What each side does

| | basis (`basis-api new app`) | berth (`app.yaml` and what it renders) |
|---|---|---|
| Image | One image per repo, `python:3.14-slim`, entrypoint `tini -- basis`, `USER 10001`, no uv at run time, `PYTHONDONTWRITEBYTECODE=1`, the commit in `BASIS_GIT_SHA` and the OCI revision label. The pipeline stub tags it with the commit. | `<registry>/<image>:<tag>@sha256:<digest>`, the image defaulting to the app's name; pods run as UID 10001, non-root, read-only root filesystem, every capability dropped, an emptyDir at `/tmp`. |
| Commands | The container's command follows `basis`: `serve`, `relay` on a schedule where a database declares an outbox, and `run JOB --params jobs/JOB/params/default.json --interval START..END --publish`. | `services.<name>.args` and `workflows.<name>.tasks.<task>.args`, static lists. |
| Port | `BASIS_SERVE__PORT`, default 8000, on 0.0.0.0. | `services.<name>.port`, any of 1024–65535; `berth new` writes 8080. |
| Health | `/health/live` (the process answers) and `/health/ready` (200 between the resources opening and SIGTERM, 503 while it drains, `drain_s` 5 by default); both at `/` and under `/api` for an app that serves a UI. | One `health.path` for both probes: readiness every 5 s, 3 failures; liveness every 10 s, 6 failures, after 10 s. |
| Environment | `BASIS_ENV` (dev, qa or prod) required by every command that reaches the firm's systems, `basis serve` among them since 5.6; in a pod, settings come from the process's environment alone. `BASIS_PROD_WRITES=1` lets a prod run write. | `BERTH_APP`, `BERTH_ENV` (the platform's environment names: dev, qa, prod), `BERTH_VERSION` (the tag); `config` as a ConfigMap through `envFrom`, merged per environment file. |
| Settings | `BASIS_*`, nested with `__`, such as `BASIS_HTTP__PRICING__URL` and `BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING`; `BASIS_COMPUTE__WORKERS` has no default. | `config` (any `^[A-Z_][A-Z0-9_]*$` key) and `secrets`: a Key Vault secret `<app>--<name>` becomes the variable `NAME` upper-cased, `-` to `_`, the name at most 40 characters of `[a-z0-9-]`. |
| Other services | `api.http("pricing", ...)` reads `BASIS_HTTP__PRICING__URL`, with its own `proxy` and `ca_bundle`; never `HTTP_PROXY`. | `talks_to: [pricing/api]` injects `PRICING_API_HOST`, `_PORT` and `_URL` (`http://api.pricing-<env>.svc.cluster.local:<port>`) and opens the target's network policy. `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in every container where the environment has a proxy. |
| Traces | Spans over OTLP/HTTP to `BASIS_TRACES__ENDPOINT`, a full URL; no `OTEL_*` variable changes anything (design.md, "Traces and logs"). The resource's `service.name` is the `api.Service`'s or the job's name, `service.version` the commit. | `OTEL_SERVICE_NAME` = `<app>-<component>`, `OTEL_RESOURCE_ATTRIBUTES` (env, version = tag, namespace = app), `OTEL_EXPORTER_OTLP_ENDPOINT` = `http://$(HOST_IP):4318` with `HOST_IP` from the downward API. |
| Datadog tags | Every log record carries `service` = the service's or the job's name, which overrides the pod's tag (docs/spikes/datadog.md, finding 4). | Pod labels `tags.datadoghq.com/service` = `<app>-<component>`, `env`, `version`; the rightsizing queries and `berth explain`'s links use `service:<app>-<component>`. |
| Jobs | Whatever schedules the container gives it the command; a run's interval is absolute dates or instants. | `orchestrator: airflow` (one DAG per workflow, `KubernetesPodOperator` with a static `pod_template_dict`), `controlm` (one fixed time a day) or `kubernetes` (a CronJob, one task). `retries` re-run a task's pod. |
| Pipeline | `azure-pipelines.yml` (managed by basis, STUB): `basis check --full`, build and push the image tagged with the commit, then `basis smoke --env qa` after "the team's template deploys the image to QA". | A version reaches an environment by a pull request to the deploy repo (`berth release`, `berth promote`, the release bot), reviewed by tier, then `berth deploy` on merge. |

## Answers

| # | Question | Answer | Fix on basis's side |
|---|---|---|---|
| 1 | Does the image run under berth's pod security? | Yes. UID 10001 is the image's own user; nothing writes outside `/tmp` (no bytecode, no uv). The process pool's semaphores use `/dev/shm`, which stays writable. | None. Unverified on a cluster: a driver or library writing to `$HOME`, which `--no-create-home` leaves as `/`. |
| 2 | Do the probes fit one `health.path`? | Yes, with `/health/ready`. It is 503 only before the app is up, when uvicorn refuses connections anyway, and while it drains, when the pod is terminating: 5 s of drain plus the in-flight budget stays inside the 30 s grace period, well before 6 failed liveness checks (60 s). `/health/live` would also work, but the cluster would keep sending requests during the drain. | Say `health: {path: /health/ready}` in the generated README (fix A). |
| 3 | Does the port fit? | Yes, once `app.yaml` says `port: 8000`. `berth new` writes 8080, so a copied spec answers nothing and fails its probes. | Fix A. |
| 4 | Does the environment reach basis? | No. berth sets `BERTH_ENV`, not `BASIS_ENV`, and since 5.6 `basis serve`, `relay` and `run` refuse to start without it. | Set `BASIS_ENV` in each environment file's `config` (it works today; fix A says so). The names agree (dev, qa, prod). Reading `BERTH_ENV` would tie core to one platform. |
| 5 | Do the settings fit `config` and `secrets`? | Partly. Everything not secret goes in `config`. A secret whose variable is a nested name, such as `BASIS_DATABASES__SOURCE__CONNECTION_STRING`, needs the Key Vault name `basis-databases--source--connection-string`, 42 characters, over berth's 40. | None in code. pydantic-settings merges a JSON value at any nested level with the other variables, which this spike checked: the secret `basis-databases--warehouse` (26 characters) holds `{"connection_string": "...", "write": {"connection_string": "...", "ledger": "etl.publishes"}}` and `config` holds `KIND`, `ISOLATION`, `TIMEOUT_S` and `MAX_ROWS`. Document it in fix A and the job and API references (fix B). |
| 6 | Does `talks_to` configure basis's client? | No. It injects `PRICING_API_URL`; basis reads `BASIS_HTTP__PRICING__URL`. Kubernetes expands `$(VAR)` only in a container's own `env` values, never in a ConfigMap read through `envFrom`, so `config` cannot point one at the other. | Write `BASIS_HTTP__PRICING__URL` in each environment file, the same deterministic URL; keep `talks_to` for the network policy, which refuses a URL into another environment's namespace anyway. An API that serves a UI is under `/api`, so its callers' URL ends with `/api`. Fix A. |
| 7 | Do traces reach the Datadog Agent? | No. design.md's endpoint, `http://$(HOST_IP):4318/v1/traces`, only works as a container `env` value; in `config` the text `$(HOST_IP)` reaches basis unexpanded. pydantic's `HttpUrl` takes it (as `http://$(host_ip):4318/v1/traces`, checked here), so the app starts, and every export fails to resolve the host: spans are dropped, logged once. basis ignores berth's `OTEL_EXPORTER_OTLP_ENDPOINT`, by design. | Point `BASIS_TRACES__ENDPOINT` at the Agent's node-local Service, `http://<agent-service>.<namespace>.svc.cluster.local:4318/v1/traces` (`internalTrafficPolicy: Local`, docs/spikes/datadog.md finding 1), a static URL `config` can hold. The fallback, if the firm's Agent install has no such Service, is decision 2 below. |
| 8 | Do logs, traces and metrics share one Datadog service? | No. berth tags pods and metrics `quote-board-api`, and builds its rightsizing queries and links on it; basis's logs say `service: quote-board` (overriding the pod's tag) and its spans `service.name: quote-board`, so logs and traces sit apart from the metrics. A job's records say the job's name; berth's component for a task is `<workflow>-<task>`. | Decision 2 below. |
| 9 | Does the version agree? | Yes, if the release takes the image tag the pipeline stub pushes, the commit: berth's `version` tag and basis's `service.version` (`BASIS_GIT_SHA`) are then the same text. | None. A release tag such as `v0.3.0` instead of the commit would split them; fix C keeps the commit. |
| 10 | Can berth schedule a job's run? | No. A berth task's `args` are static, and `basis run` needs `--interval START..END` as dates. Control-M's rendering has one fixed time a day and passes no date. Whether Airflow templates `pod_template_dict` (so `{{ data_interval_start }}` could fill it) is unverified, and would not help Control-M. | An interval relative to the run's start, resolved by `basis run` in the job's zone and calendar: for example `--interval previous-business-day` or `--interval today-1..today`, with step 5.2's calendars (SIFMA). Decision 1 below. |
| 11 | Can berth run the relay? | Yes. `basis relay` is one run over the outboxes, which is what an `orchestrator: kubernetes` workflow (a CronJob, one task, a schedule) runs. It needs the API's database and service settings, which `config` gives every container of the app. | Fix A writes the workflow. |
| 12 | Does a retried task do the right thing? | Yes for upserts and delete-inserts. A plain insert that published before its pod failed is refused on the retry, since the ledger has its interval; the run's ERROR line says to pass `--again`. | None; fix A sets `retries: 0` for a job whose writes are plain inserts. |
| 13 | Do outbound calls get through the proxy? | Yes, as basis decides it: berth's `HTTP_PROXY` is ignored, so in-cluster calls go direct, and each call to the outside (the identity provider's JWK set, Slack, another service outside the cluster) names its `proxy` and `ca_bundle` in settings; a CA bundle file comes from berth's `files`, mounted at `/config/<name>`. The data lake's Azure SDK does read `HTTPS_PROXY`, and berth's `NO_PROXY` leaves cluster names direct. | None. Fix A shows a `ca_bundle` from `files`. |
| 14 | Does the pipeline stub fit berth's release flow? | No. The stub ends at "deploy the image to QA" inside the repo's pipeline, then smokes QA. Under berth a version reaches an environment by a pull request to the deploy repo, reviewed by tier and deployed on merge, so the smoke cannot follow in the same run, and nothing checks the smoke ran against the commit just built. | Fix C. |

## Fixes on basis's side

**A. The deploy spec in the generated repo.** `basis-api new app` (and `basis-job new repo`) writes a "Deploying" section
in the README with an `app.yaml` and a `dev.yaml` that fit the repo it made, from what the repo declares: its one
service, a relay workflow where a database declares an outbox, and one workflow per job. Later, if it earns it, a verb
prints the same from the repo's current code, so it follows renamed jobs and new outboxes. For the repo this spike made:

```yaml
# apps/quote-board/app.yaml
name: quote-board
description: Accrued interest, prices and yields of the firm's bonds
owners: [nina.berg@contoso.com, kai.muller@contoso.com]
source: https://dev.azure.com/contoso/trading/_git/quote-board
azure_identity: true            # the run store on the data lake; SQL Server's ActiveDirectoryDefault
talks_to: [pricing/api, ledger/api]
config:
  BASIS_COMPUTE__WORKERS: "2"   # no default: one per CPU of the service's size
  BASIS_TRACES__EXPORTER: otlp
  BASIS_TRACES__ENDPOINT: http://datadog-agent-local.datadog.svc.cluster.local:4318/v1/traces  # answer 7; the name is the firm's
  BASIS_DATABASES__WAREHOUSE__KIND: mssql
  BASIS_DATABASES__WAREHOUSE__ISOLATION: read_committed
  BASIS_DATABASES__WAREHOUSE__TIMEOUT_S: "5"
  BASIS_DATABASES__WAREHOUSE__MAX_ROWS: "100000"
  BASIS_IDENTITY__JWKS__CA_BUNDLE: /config/firm-ca.pem
files:
  firm-ca.pem: |
    -----BEGIN CERTIFICATE-----
    ...
secrets: [basis-databases--warehouse]   # {"connection_string": ..., "write": {...}}: answer 5
services:
  api:
    args: [serve]
    port: 8000
    health: {path: /health/ready}
workflows:
  relay:
    orchestrator: kubernetes
    schedule: "*/5 * * * *"
    tasks:
      relay: {args: [relay], timeout: 5m}
  positions:
    orchestrator: airflow
    schedule: "0 6 * * 1-5"
    timezone: America/New_York
    tasks:
      run:
        args: [run, positions, --params, jobs/positions/params/default.json, --interval, previous-business-day, --publish]  # decision 1
        timeout: 1h
```

```yaml
# apps/quote-board/dev.yaml
version: 3f1c2a9e@sha256:...       # the commit the pipeline tagged
config:
  BASIS_ENV: dev                   # answer 4
  BASIS_SNAPSHOT_ROOT: abfs://runs@devaccount.dfs.core.windows.net/quote-board
  BASIS_HTTP__PRICING__URL: http://api.pricing-dev.svc.cluster.local:8000   # answer 6: what talks_to injects as PRICING_API_URL
  BASIS_HTTP__LEDGER__URL: http://api.ledger-dev.svc.cluster.local:8000
```

`prod.yaml` adds `BASIS_PROD_WRITES: "1"` only where the deployment writes, and berth's review rules then show the
change to the reviewer.

**B. The references say how a secret fits a 40-character name.** The job and API references, and the generated
`.env.example`, say that a database's secret parts can be one JSON value at the database's own name
(`BASIS_DATABASES__WAREHOUSE`), merged with its other variables.

**C. The pipeline stub follows the deploy repo's flow.** The image stage keeps tagging with the commit and gives the
digest as the stage's output; a STUB step after it opens the release pull request to the deploy repo (`berth release
quote-board --env dev --version COMMIT@DIGEST`, as the release bot), and the `smoke_qa` stage leaves this pipeline for
one the deploy pipeline triggers after QA deploys. For that, `basis smoke` gains `--commit SHA`, and the app answers
its commit (`BASIS_GIT_SHA`) in `/health/ready`, so a smoke waits until QA serves the commit it was asked about and
fails, naming both commits, when it never does.

## Decisions for the maintainer

1. **A relative interval for scheduled runs (answer 10).** Recommended: `basis run --interval` also takes forms
   resolved at the run's start, in the job's zone, with the calendars of step 5.2: `previous-business-day` (with
   `[tool.basis.job] calendar = "sifma"`), `today-N..today` and `this-hour`. Every orchestrator then runs the same
   static command, and a rerun of a missed day still passes dates. The alternative, the orchestrator passing dates,
   works for Airflow only if its pod template is templated (unverified) and never for berth's Control-M jobs.
2. **The service name Datadog sees (answers 7 and 8).** design.md has basis read no `OTEL_*` variable. Under berth
   that leaves logs and traces under `quote-board` and metrics under `quote-board-api`. Options:
   - (a) basis reads the two standard variables that name a process, `OTEL_SERVICE_NAME` and
     `OTEL_RESOURCE_ATTRIBUTES`' `deployment.environment.name` and `service.version`, as the name and attributes its
     spans and log records carry, when they are set, and still nothing that configures the exporter. One source, the
     platform's, per container, a task's included. Recommended: berth sets them so that "traces, metrics and logs
     line up with what berth deployed", and nothing else gives basis a name per container (`config` is per app).
   - (b) the generator names the service `<app>-api`, and each job logs as `<app>-<job>-run`: matches berth's names
     by convention only, and breaks when a workflow or task is named otherwise.
   - (c) keep basis's names and drop the `service` field from log records, so Datadog takes the pod's tag for logs:
     logs and metrics agree, traces stay apart.

   With (a), basis could also take `OTEL_EXPORTER_OTLP_ENDPOINT` plus `/v1/traces` as its endpoint's default, which
   removes answer 7's dependency on the Agent's local Service. That is a second exception to the same rule, so it is
   listed, not recommended.

## Not verified

- No cluster: the pod security, the probes' timing against the drain, `/dev/shm` and `$HOME` writes are read from the
  rendered specs, not run.
- The Datadog Agent's node-local Service name and port in the firm's install, and whether Airflow's
  `KubernetesPodOperator` renders Jinja in `pod_template_dict`.
- Workload identity reaching SQL Server's `ActiveDirectoryDefault` and the data lake from a pod.
- The release bot's and deploy pipeline's triggers, which berth describes but this spike did not run.
