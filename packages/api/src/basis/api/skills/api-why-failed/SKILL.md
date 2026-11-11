---
name: api-why-failed
description: Find out why a request to a basis-api service failed or answered an error, from its problem, its logs and a replay with basis call. Use when the user asks why a call, endpoint or request failed.
---

# Why a request failed

1. Find the request's id. Every error answers a problem whose `instance` is `urn:uuid:` and the id, and every
   response carries it as `X-Request-Id`. Every log line of the request carries it as `request_id`, with its
   `trace_id`, which finds its spans in Datadog.
2. Read the request's last log line: `METHOD PATH: STATUS`, with its `route`, `status`, `ms` and `user`. A 500 is
   logged once with its traceback, which names the line of the repo's code that raised; a resource's failure is a
   warning naming the `resource`, the error's `kind` and the `reason`.
3. Read the problem's `type`. A type the route declares, such as `unknown-bond`, is a domain error or a refusal the
   handler raised on purpose: its `detail` says why. `invalid-request` is a request the route's models refused, each
   field in `errors`. A type that starts `service-`, such as `service-timeout` or `service-unavailable`, is a
   resource that failed, which the logs name; a deadline caps every call a route makes. A 500 is a defect: an
   exception the route does not declare.
4. Make it again on this machine, on fakes, with the same request:
   `uv run basis call ROUTE '{"path": {...}, "query": {...}, "body": ..., "user": {"sub": ...}}' --fake`. A request
   Schemathesis failed in `basis check --full` is saved as a file its `ERROR` line names, which
   `uv run basis call ROUTE @FILE --fake` takes. With `--real --env dev` it runs against the real resources.
5. Fix it with a test first, in `tests/test_handlers.py`, that makes the same request on fakes; then
   `uv run basis check`.
6. A call made after its commit, through `send`, that failed waits in the outbox: `uv run basis relay --env prod`
   makes it again, and its log names each call that was refused or aged out.
