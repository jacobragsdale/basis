# Spike 0.5: Datadog

basis can build step 5.1 against a typical install now. Spans go over OTLP/HTTP to the Agent on the pod's own node, at
`http://$(HOST_IP):4318/v1/traces`. Logs stay JSON on stderr. Datadog reads basis's `level` field as the log's status,
and links a record to its trace by top-level `trace_id` (32 lowercase hex) and `span_id` (16 lowercase hex), which
basis-api already writes. Datadog needs no trace-id field of its own. The firm-specific parts become a checklist for the
port. Researched on 2026-11-07, without access to the firm's cluster.

"From docs" means Datadog's or OpenTelemetry's published docs, with each page's last-modified date. "Verified here"
means I checked it on this machine: by running basis's code, or by reading the source of the Agent, the Helm chart or
the exporter at the version named below.

## Versions

- **Datadog Agent:** 7.85.2, the latest release (2026-11-06). Source read on `main` on 2026-11-07.
- **Datadog Helm chart:** `datadog` 3.269.1.
- **Datadog Operator:** v1.31.2 (2026-10-21).
- **basis `main` (d6d3f1f):** Python 3.14.7, opentelemetry-sdk 1.45.2, opentelemetry-exporter-otlp-proto-http 1.45.2,
  FastAPI 0.162.0.
- **Doc dates:** each source below shows its page's last-modified date. All pages were read on 2026-11-07.

## Answers

| # | Question | Answer | How known |
|---|---|---|---|
| 1 | Does the Agent take OTLP traces on gRPC 4317 and HTTP 4318? | Yes. Each protocol is off until enabled, and basis needs HTTP. | from docs |
| 1 | Can a pod reach its node's Agent at `status.hostIP` through a hostPort? | Yes. Helm and the Operator both turn the hostPort on by default. | from docs; Helm defaults verified here |
| 1 | Is there also a node-local service? | Yes. The Helm chart's Agent service has `internalTrafficPolicy: Local`, and it gets port 4318 once HTTP is enabled. | verified here (chart source) |
| 1 | Does DDOT change anything for basis? | No. It listens on the same 4317/4318, and the SDK side stays the same. | from docs |
| 2 | Does the Agent collect containers' stdout and stderr? | Yes, with `logs.enabled` and `containerCollectAll`. | from docs |
| 2 | Is JSON parsed without a pipeline? | Yes. Datadog's intake does it, not the Agent. | from docs |
| 2 | Is a stderr line's status `error` by default? | Yes. | from docs; verified here (Agent source) |
| 2 | Do basis's records still get the right status? | Yes. `level` is a default status attribute, and basis's values map to the right statuses. | from docs; values verified here |
| 2 | How is the plain-text `ERROR:` line treated? | Its status is `error`, from the stream. It is unparsed and not linked to a trace. | from docs; verified here (Agent source) |
| 3 | Do Agent-collected logs need `dd.trace_id` in decimal (the low 64 bits)? | No. OTel's hex `trace_id` and `span_id` are accepted. | from docs |
| 4 | Do OTel resource attributes map to `service`, `env` and `version`? | Yes. | from docs |
| 4 | Do container logs get those tags from the JSON, or from the trace's resource? | No. Pod labels give all three. A JSON `service` field gives `service` alone. | from docs |
| 5 | Are hits, errors and latency computed for OTLP spans without a setting? | Yes. | from docs; defaults verified here (Agent source) |
| 5 | Is anything billed as custom metrics? | No, as long as basis sends no OTLP metrics, which it doesn't. | from docs (partly inferred); no metrics verified here |
| 6 | Does a desktop export nothing when the traces settings are unset? | Yes, that is the code today. | verified here |

## 1. OTLP ingest in the Agent

The Agent has ingested OTLP traces since 7.32. It runs gRPC on `0.0.0.0:4317` and HTTP on `0.0.0.0:4318`, each off until
enabled. basis's exporter is OTLP/HTTP protobuf, so only the HTTP receiver matters.

- **Helm:** `datadog.otlp.receiver.protocols.http.enabled: true`. Its defaults are `endpoint: "0.0.0.0:4318"` and
  `useHostPort: true`.
- **Operator:** `spec.features.otlp.receiver.protocols.http.enabled: true`. The CRD says "Host port is enabled by
  default"; `hostPortConfig.hostPort` changes the port.

The documented way for a pod to reach the Agent is the downward API's `status.hostIP` plus that hostPort, since "you
cannot send OpenTelemetry telemetry ... to an Agent on a different host".

The Helm chart also renders a Service named after the release, with `internalTrafficPolicy: Local` and an `otlphttpport`
of 4318 when HTTP is enabled. The DDOT docs offer that service as the alternative to a hostPort.

**DDOT.** The Datadog Distribution of the OTel Collector (`datadog.otelCollector.enabled: true`, or
`features.otelCollector.enabled: true`) runs inside the Agent pod, on the same ports. Datadog's compatibility page now
lists DDOT setups first, and OTLP ingest in the Agent is still documented (page modified 2026-10-28). For basis only the
cluster side differs. Don't enable both on one node's 4317/4318 hostPorts; that is my inference, not a documented rule.

**Verified here:**

- The exporter uses the endpoint it is given as is. `http://host:4318` posted to `/`, and `http://host:4318/v1/traces`
  posted to `/v1/traces`. The OTel spec says the same of a per-signal endpoint, and `/v1/traces` is the Collector
  receiver's default path, which the Agent follows. So `BASIS_TRACES__ENDPOINT` must end in `/v1/traces`.
- basis's resource has no `host.name`. That is what the hostname guide asks for: "Omit hostname attributes", and Datadog
  then uses the Agent's hostname.

**Best guess.** Each pod spec, a Deployment's and the one an Airflow KubernetesPodOperator makes alike, gets:

```yaml
env:
  - name: HOST_IP
    valueFrom: {fieldRef: {fieldPath: status.hostIP}}
  - name: BASIS_TRACES__EXPORTER
    value: otlp
  - name: BASIS_TRACES__ENDPOINT
    value: http://$(HOST_IP):4318/v1/traces   # HOST_IP must come first in the list
```

A job is a short-lived process, so 5.1 must shut down the tracer provider before the process exits. That flushes the
batch processor, which otherwise drops the last spans.

**Fallbacks:**

- **Only gRPC is enabled:** ask for `protocols.http.enabled`. Switching basis to the gRPC exporter would add `grpcio`.
- **hostPorts are blocked by policy:** use the local service,
  `http://<release>.<agent-ns>.svc.cluster.local:4318/v1/traces`. The Operator's equivalent is named by
  `global.localService.nameOverride`, which is probably `<dda-name>-agent`.
- **The firm runs DDOT:** nothing changes in basis.

## 2. Log collection

- **Helm:** `datadog.logs.enabled: true` and `datadog.logs.containerCollectAll: true`.
- **Operator:** `features.logCollection.enabled: true` and `containerCollectAll: true`.

The Agent tails `/var/log/pods`, Datadog's recommended method, and reassembles lines the runtime split, since its
Kubernetes parser supports partial lines (verified here). Datadog "automatically parses JSON-formatted logs".

**The gotcha holds.** The Docker log docs say "Logs coming from container Stderr have a default status of Error". The
Agent's Kubernetes parser does the same: `stderr` gives `StatusError` and `stdout` gives `StatusInfo`.

It doesn't hurt basis, because JSON preprocessing makes the first of `status`, `severity`, `level` and `syslog.severity`
the log's official status. basis writes `level` as `record.levelname.lower()`. Running basis's `JsonFormatter`, and
applying the remapper's rules, gives:

| `level` | Status |
|---|---|
| `debug` | debug |
| `info` | info |
| `warning` | warning |
| `error` | error (starts with `err`) |
| `critical` | critical |

No doc says in so many words that a JSON `level` beats the stderr default, only that it becomes the official status. The
port's checklist confirms it.

The final `ERROR: ...` line isn't JSON, so it keeps the stream's `error` status. That is the right status for it. Its
whole line is the message, and it has no trace id, so it shows beside the run's records but not on the trace.

Two other preprocessing facts touch basis's record:

- **`time` is not a default date attribute.** The defaults are `timestamp`, `@timestamp`, `date` and a few others. The
  log keeps the time the Agent or intake gives it, which is close to basis's own. Renaming `time` to `timestamp` would
  make basis's clock the official one. It is optional.
- **`service` is a reserved attribute,** and it overrides the pod's tag. basis-api already binds `service=service.name`
  (server.py:62), which is the same value as the trace's `service.name`. That is correct, and jobs should do the same.

**Best guess.** No change to basis: keep `level`. Collection is the cluster's `logs.enabled` and `containerCollectAll`.

**Fallbacks:**

- **The org's preprocessing was edited and lacks `level`, or stderr wins:** add `level` to the Status attributes in
  "Preprocessing for JSON logs", or add a pipeline on `service:<name>` with a status remapper on `level`. That is one UI
  change, and is smaller than renaming the field in basis.
- **`containerCollectAll` is off:** add the annotation `ad.datadoghq.com/<container>.logs:
  '[{"source":"basis","service":"<name>"}]'`. The annotation also helps if a container's short image name matches an
  integration's, whose pipeline would otherwise parse basis's lines.

## 3. Linking logs to traces

The current wording of Datadog's OpenTelemetry correlation page (modified 2026-09-15), for logs scraped from files or
collected by the Agent:

> `trace_id`: The ID of the trace. It must be a 32-character lowercase hexadecimal string.
> `span_id`: The ID of the span. It must be a 16-character lowercase hexadecimal string.

and, under "Collect logs using the Datadog Agent":

> Datadog automatically detects the `dd.trace_id` and `dd.span_id` convention used by Datadog SDKs, as well as the
> OpenTelemetry standards `trace_id` and `span_id`.

The page's Python example is `f'{span_context.trace_id:032x}'`. The JSON preprocessing defaults list `trace_id` and
`span_id` beside the `dd.*` names.

How the docs got here:

- **Before 2025-08-07:** the page told you to convert the span id to Datadog's 64-bit unsigned integer.
- **2025-08-07:** the hex wording arrived (DataDog/documentation#30354).
- **2025-11-10:** `trace_id` and `span_id` were added to the documented preprocessing defaults (#32717).
- **2025-12-15:** the Agent-path sentence arrived (#33203).

So decimal low-64-bit `dd.trace_id` is the old way, and basis doesn't need it.

**Verified here.** basis-api's request logs carry `"trace_id":"0eab4b65685a63db1bde96a303523e33"` and
`"span_id":"09bbe7faf7ef0102"`, from `trace.format_trace_id` and `trace.format_span_id` (requests.py:46). That is the
documented format.

**Best guess.** basis emits these fields, and no `dd.*` field:

| Field | Format | Example |
|---|---|---|
| `trace_id` | 32 lowercase hex characters, no `0x`, top level | `0eab4b65685a63db1bde96a303523e33` |
| `span_id` | 16 lowercase hex characters, no `0x`, top level | `09bbe7faf7ef0102` |

Each record carries them only while a span is current. In 5.1, log a run's `finished` record before its root span ends,
so that the summary links too.

**Fallback.** If the org's preprocessing lacks `trace_id`, add it, and `span_id`, to the Trace Id and Span Id lists. A
trace ID remapper on `trace_id` does the same job.

## 4. Service, env and version

| OTel resource attribute | Datadog tag |
|---|---|
| `service.name` | `service` |
| `deployment.environment.name` (Agent 7.58+) or the deprecated `deployment.environment` | `env` |
| `service.version` | `version` |

basis sets only `service.name` today (traces.py).

Logs need the same three: unified service tagging "is required for all data correlation". Container logs don't get them
from the trace: "The Datadog Agent does not automatically convert OTel resource attributes (for example, `service.name`)
to Datadog's standard tags."

They come from the pod labels `tags.datadoghq.com/service`, `/env` and `/version`, which "cover ... log collection". A
JSON `service` field also sets `service`. There is no reserved JSON attribute for `env` or `version`. Without the
labels, a log's service is the container's short image name, and it has no env. Also, the OTLP receiver falls back to
the Agent's own `DD_ENV` when a span has no environment attribute (verified here, in `pkg/trace/api/otlp.go`).

**Best guess.**

- **Resource:** 5.1 builds it from basis's own settings: `service.name`, `service.version` (the app's release version)
  and `deployment.environment.name` (the `--env` or `BASIS_ENV` value: dev, qa or prod).
- **Pod labels:** the deployment and each job's pod set the three `tags.datadoghq.com/*` labels to the same values.
- **`service` field:** every log record carries `service`, as basis-api's do.

**Fallbacks:**

- **The Agent is older than 7.58:** also set `deployment.environment`.
- **The firm's env names differ** (such as `production`): map basis's names to the firm's in the deployment's settings.

## 5. APM metrics from OTLP

OTLP spans go through the trace-agent, which computes stats itself. No "compute stats" setting is needed in the Agent;
that setting belongs to the standalone Collector's connector.

Defaults, verified here in the Agent's settings:

- `apm_config.compute_stats_by_span_kind: true`, so server, client, producer and consumer spans get stats.
- `apm_config.features` is empty. That leaves the opt-in `enable_otlp_compute_top_level_by_span_kind` off, so the
  Datadog rule for service-entry spans applies: a root span, or a span whose parent is in another service.

Either way, a run's root span and an API's server span get hits, errors and latency, and SQL and HTTP spans of kind
`client` get them too. Internal spans get none.

basis samples `ParentBased(ALWAYS_ON)`, so the metrics count all traffic. Datadog warns that sampling in the SDK makes
them sampled.

**Billing.** basis sends no OTLP metrics. FastAPI's telemetry has `"metrics": False`, and no MeterProvider is set
(verified here). Trace metrics (`trace.*`) come with APM, and no Datadog page counts them as custom; that part is my
inference. What is custom is metrics someone generates from spans in the UI. APM itself bills per Agent host that sends
traces, plus spans ingested and indexed. A node that runs only basis pods becomes an APM host once they trace.

**Best guess.** No setting. Give a job's root span kind `SERVER`, or leave it a root span, and keep SQL and HTTP spans
kind `CLIENT`.

**Fallback.** If a job's service shows no request metrics, set its root span's kind to `SERVER`.

## 6. Desktops

Today `Settings.traces` is `None` unless set. With it unset, `traces.exporter(None)` adds no processor: spans are made,
so logs still carry trace ids, and then they are dropped. FastAPI's `operation_spans` are off too.

An `otlp` exporter without an endpoint is refused at startup. That is better than the OTel default of `localhost:4318`,
which would log a failed export for every batch on a machine with no Agent.

**Best guess.** Keep it. A desktop exports nothing unless `BASIS_TRACES__EXPORTER=console` asks to see the spans.

One catch for 5.1: the console exporter prints to stdout (traces.py), and a job's stdout carries its results. When
tracing moves into core, the console exporter must write to stderr.

## (work): to confirm at the port

Nothing here blocks 5.1. Run these when the pilot first deploys, and fix whatever differs.

```sh
# 1. Find the Agent and how it was installed
helm list -A | grep -i datadog
kubectl get datadogagents -A
kubectl get pods -A -o wide | grep -i datadog
NS=<agent namespace>; AGENT=<one node-agent pod>; RELEASE=<helm release>

# 2. Its OTLP, logs and DDOT settings (whichever installer applies)
helm get values $RELEASE -n $NS --all -o yaml | grep -n -A12 -E '^  (otlp|logs|otelCollector):'
kubectl get datadogagent -n $NS -o yaml | grep -n -A12 -E '^    (otlp|logCollection|otelCollector):'

# 3. What it runs: its version, whether OTLP and logs are up, the hostPort, the local service
kubectl exec -n $NS $AGENT -c agent -- agent version
kubectl exec -n $NS $AGENT -c agent -- agent status | grep -n -i -A6 -E 'otlp|logs agent'
kubectl get ds -n $NS -o yaml | grep -n -B2 -A3 'containerPort: 4318'
kubectl get svc -n $NS -o wide
```

4. **Send a test span and its logs from a pod.** Use any image with `sh` and `curl` that the cluster can pull, such as
   the firm's Python base image.

```sh
kubectl apply -n <app namespace> -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: basis-dd-spike
  labels:
    tags.datadoghq.com/service: basis-spike
    tags.datadoghq.com/env: dev
    tags.datadoghq.com/version: 0.0.1
spec:
  restartPolicy: Never
  containers:
    - name: spike
      image: <image with sh and curl>
      env:
        - name: HOST_IP
          valueFrom: {fieldRef: {fieldPath: status.hostIP}}
        - name: ENDPOINT
          value: http://$(HOST_IP):4318/v1/traces
      command: ["sh", "-c"]
      args:
        - |
          tid=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
          sid=$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')
          start=$(date +%s)000000000; end=$(( $(date +%s) + 1 ))000000000
          body=$(cat <<JSON
          {"resourceSpans":[{"resource":{"attributes":[
            {"key":"service.name","value":{"stringValue":"basis-spike"}},
            {"key":"deployment.environment.name","value":{"stringValue":"dev"}},
            {"key":"service.version","value":{"stringValue":"0.0.1"}}]},
           "scopeSpans":[{"scope":{"name":"spike"},"spans":[{"traceId":"$tid","spanId":"$sid",
            "name":"spike.run","kind":2,"startTimeUnixNano":"$start","endTimeUnixNano":"$end"}]}]}]}
          JSON
          )
          curl -sS -o /dev/null -w 'otlp http status %{http_code}\n' -H 'Content-Type: application/json' -d "$body" "$ENDPOINT"
          echo "{\"level\":\"info\",\"logger\":\"basis.spike\",\"message\":\"finished spike\",\"service\":\"basis-spike\",\"trace_id\":\"$tid\",\"span_id\":\"$sid\"}" >&2
          echo "{\"level\":\"warning\",\"logger\":\"basis.spike\",\"message\":\"spike warning\",\"service\":\"basis-spike\",\"trace_id\":\"$tid\",\"span_id\":\"$sid\"}" >&2
          echo "ERROR: spike: a plain line, as basis prints last" >&2
          echo "trace_id=$tid"
          sleep 30
EOF
kubectl logs -n <app namespace> basis-dd-spike     # expect "otlp http status 200" and the trace id
```

5. **Find the test span and its logs in Datadog:**
   - **APM > Traces, `service:basis-spike`:** open `spike.run`. Check its `env` and `version`, then its **Logs** tab.
   - **Logs > Explorer, `service:basis-spike`:** note the status of each of the four lines. Open the `info` record's
     **Trace** tab.
   - **The `basis-spike` service page:** check that requests, errors and latency appear.
   - **Logs > Pipelines > Preprocessing for JSON logs:** read the Status, Service, Trace Id and Span Id lists.
6. **Clean up:** `kubectl delete pod -n <app namespace> basis-dd-spike`.

**Send back:**

- the Agent's version, and whether Helm, the Operator or DDOT installed it;
- the `otlp`, `logs` and `otelCollector` values;
- whether 4318 is a hostPort, and the local service's name and namespace;
- the curl's HTTP status, or its error;
- whether the span appeared, with `env:dev` and `version:0.0.1`;
- the status of each of the four log lines (expected: info, warning, error, info);
- whether the log and its trace link both ways;
- the four preprocessing lists;
- whether the service page shows request metrics.

## Sources

**Datadog docs** (page last-modified dates):

- [OTLP Ingestion by the Datadog Agent](https://docs.datadoghq.com/opentelemetry/setup/otlp_ingest_in_the_agent/)
  (2026-10-28)
- [Hostname and Tagging](https://docs.datadoghq.com/opentelemetry/config/hostname_tagging/) (2026-10-08)
- [DDOT Collector](https://docs.datadoghq.com/opentelemetry/setup/ddot_collector/) (2026-09-11)
- [DDOT as a Kubernetes
  DaemonSet](https://docs.datadoghq.com/opentelemetry/setup/ddot_collector/install/kubernetes_daemonset/) (2026-09-28)
- [Datadog and OpenTelemetry Compatibility](https://docs.datadoghq.com/opentelemetry/compatibility/) (2026-11-04)
- [Kubernetes log collection](https://docs.datadoghq.com/containers/kubernetes/log/) (2026-08-17)
- [Docker log collection](https://docs.datadoghq.com/containers/docker/log/), for the stderr status (2026-08-06)
- [Pipelines: preprocessing](https://docs.datadoghq.com/logs/log_configuration/pipelines/#preprocessing) (2026-11-05)
- [Log Status Remapper](https://docs.datadoghq.com/logs/log_configuration/processors/log_status_remapper/) (2026-08-06)
- [Logs show info status for warnings or
  errors](https://docs.datadoghq.com/logs/guide/logs-show-info-status-for-warnings-or-errors/) (2026-08-06)
- [Correlating OpenTelemetry Traces and
  Logs](https://docs.datadoghq.com/tracing/other_telemetry/connect_logs_and_traces/opentelemetry/) (2026-09-15)
- [Semantic mapping](https://docs.datadoghq.com/opentelemetry/mapping/semantic_mapping/) (2026-09-15)
- [Unified service tagging](https://docs.datadoghq.com/getting_started/tagging/unified_service_tagging/) (2026-09-15)
- [Service-entry spans](https://docs.datadoghq.com/opentelemetry/mapping/service_entry_spans/) (2026-09-15)
- [Trace metrics](https://docs.datadoghq.com/tracing/metrics/metrics_namespace/) (2026-09-15)
- [Generate custom metrics from spans](https://docs.datadoghq.com/tracing/trace_pipeline/generate_metrics/) (2026-08-06)
- [APM billing](https://docs.datadoghq.com/account_management/billing/apm_tracing_profiler/) (2026-09-09)

**Datadog docs history:**

- [DataDog/documentation#30354](https://github.com/DataDog/documentation/pull/30354): the hex trace-id wording.
- [#32717](https://github.com/DataDog/documentation/pull/32717): `trace_id` and `span_id` in the preprocessing defaults.
- [#33203](https://github.com/DataDog/documentation/pull/33203): the Agent-path sentence.

**Source, on `main`:**

- Helm chart: [values.yaml](https://github.com/DataDog/helm-charts/blob/main/charts/datadog/values.yaml) and
  [agent-services.yaml](https://github.com/DataDog/helm-charts/blob/main/charts/datadog/templates/agent-services.yaml)
- Operator:
  [configuration.v2alpha1.md](https://github.com/DataDog/datadog-operator/blob/main/docs/configuration.v2alpha1.md)
- Agent:
  [kubernetes.go](https://github.com/DataDog/datadog-agent/blob/main/pkg/logs/internal/parsers/kubernetes/kubernetes.go)
  (log parser), [otlp.go](https://github.com/DataDog/datadog-agent/blob/main/pkg/trace/api/otlp.go) (OTLP receiver) and
  [the Linux config
  example](https://github.com/DataDog/datadog-agent/blob/main/pkg/config/example/datadog-agent_linux.yaml.example)

**OpenTelemetry:**

- [OTLP exporter configuration](https://opentelemetry.io/docs/specs/otel/protocol/exporter/), on per-signal endpoints
  used as is
- [Collector OTLP
  receiver](https://github.com/open-telemetry/opentelemetry-collector/blob/main/receiver/otlpreceiver/README.md), whose
  default path is `/v1/traces`
