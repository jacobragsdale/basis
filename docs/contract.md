# The UI contract

What a basis API offers a basis-ui app, in full, from basis-api's step 2.5 on. [The design](design.md#what-a-ui-can-rely-on)
holds the decisions and their reasons; this page is what they come to, field by field, so that a UI's client, its
fixtures and its tests can be written against it. basis-ui's tests run against the [sample](#the-sample), which this
repo's example API produces.

## The document

Each API repo commits its OpenAPI 3.1 document as `openapi.json` at its root. `basis sync` writes it from the repo's
one service, the one its `basis.apis` entry point lists, and `basis check` fails when the file differs from what the
service declares, with `run uv run basis sync` as its fix; the commit hook runs that check. The app serves the same
document at `/openapi.json`.

| What | The document says |
|---|---|
| First key | `` "x-basis": "managed by basis: `basis sync` rewrites it" ``, which the served document leaves out |
| Operation ids | The handler's name, such as `bond_yield`; a service gives each handler a name of its own |
| Paths | The route's path, with no `/api` prefix: see [One origin](#one-origin) |
| Servers | `/api`, in an app that serves a UI: see [One origin](#one-origin) |
| Models | Every model's schema has `additionalProperties: false` |
| A number | A JSON number, never a string: see [Numbers](#numbers) |
| A dataset's number column | Its display format, where it declares one, as `x-basis-format`: see [Display formats](#display-formats) |
| A time | `{"type": "string", "format": "date-time"}`, which RFC 3339 gives an offset, and every time has one |
| A date | `{"type": "string", "format": "date"}` |
| Each error | Its status, under `application/problem+json`: a 422 as `ValidationProblem`, any other as `Problem` |
| A dataset route | Its success lists `application/vnd.apache.arrow.stream` as `{"type": "string", "format": "binary"}` beside its JSON |

A model that a request and a response share has one schema, unless the two differ, as they do for a model with a
default: then it has two, `NAME-Input` for requests and `NAME-Output` for responses, as FastAPI names them.

### Numbers

Every number is a JSON number, never a string, and NaN and ±inf are `null`, decided on 2026-11-08 and built in step
2.11; [the design](design.md#numbers) gives the reasons. A dataset's row takes `float` for every number, and an `int`
or `Decimal` field in one is refused when the route is declared, with the fix; any other model refuses `Decimal`, and
takes an `int` as a JSON integer. This replaces the rule that a decimal is a JSON string: its `pattern` and
`x-basis-decimal` go, and the sample `openapi.json` that basis-ui pins changes with them (see
[The sample](#the-sample)).

## Requests

A request is read strictly before the handler runs. A request basis refuses answers 422 `invalid-request`, which names
each field at fault (see [Errors](#errors)).

| Part | Rule |
|---|---|
| A JSON body | Sent with `Content-Type: application/json`, or a JSON type such as `application/merge-patch+json` |
| A key | Refused at any depth unless the model declares it |
| A value | Of its own JSON type: a string for text, a number for a `float`, an integer without a fraction for an `int`, `true` or `false` for a boolean |
| A time | A string with its offset, as `"2026-11-06T14:30:00Z"` or `"2026-11-06T09:30:00-05:00"` |
| A route that takes no body | Refuses one |
| A query key | Refused unless the route declares it; given twice for one value, refused |
| Query and path text | Read by its type's grammar: `10`, `0.045`, `true`, `2026-11-06`, a UUID in groups of 8-4-4-4-12 |

A query parameter that is a list takes its key once for each value, `?security=A&security=B`. The full grammar of query
and path text is in [`strict.py`](../packages/api/src/basis/api/strict.py)'s docstring.

## Responses

A response that succeeds is JSON, the route's response model: a number as a JSON number, or `null` for NaN and ±inf, a
date as `2026-11-06`, and a time with its offset. Every time a route reads or answers is pydantic's `AwareDatetime`,
which refuses one without, so a route whose request or response may hold a time without its offset, or a time of day,
is refused when it is declared. A response holds only the fields its model declares, and a UI drops a field it does
not know, so an API adds response fields without breaking one (see [Compatibility](#compatibility)).

Every response carries `X-Request-Id`, the request's id, a UUIDv7, which every log line of the request carries too, and
`traceparent`, the request's trace.

## Errors

Every error is an RFC 9457 problem details response, `application/problem+json`, with the response's status.

| Member | Type | Holds |
|---|---|---|
| `type` | string | The kind of problem: the error's name in kebab case, such as `unknown-bond`, or `about:blank` for an error HTTP itself names, such as no route or the wrong method |
| `title` | string | The kind of problem in the same words every time, such as `No bond has that ISIN` |
| `status` | integer | The response's status |
| `detail` | string, optional | What happened this time |
| `instance` | string, optional | `urn:uuid:` and the request's id |
| `errors` | array, on every 422 | Each field at fault, which may be none |
| `data` | any, optional | An `HTTPException`'s detail that is not a sentence |

A 422 is a `ValidationProblem`, whether basis refused the request or the handler refused what it asked: its `errors`
array holds one entry per field at fault, each with `detail`, the message, and one of:

| Member | For | Holds |
|---|---|---|
| `pointer` | A field of the body | An RFC 6901 JSON Pointer in URI fragment form, such as `#/legs/0/rate`; `#` is the whole body. A key's `~` and `/` are `~0` and `~1`, and what a fragment does not allow is percent-encoded |
| `parameter` | A query, path or header parameter | Its name, such as `end` |

```json
{"type": "invalid-request", "title": "The request is not valid", "status": 422, "instance": "urn:uuid:019a5c3e-0000-7000-8000-000000000001",
 "errors": [{"detail": "Field required", "parameter": "end"}, {"detail": "Input should be greater than 0", "pointer": "#/face"}]}
```

The errors basis answers itself, besides each API's own:

| `type` | Status | When | Headers |
|---|---|---|---|
| `invalid-request` | 422 | The request is not read: see [Requests](#requests) | |
| `sign-in-required` | 401 | A route that needs a user has an anonymous request | `WWW-Authenticate: Bearer` |
| `invalid-token` | 401 | The bearer token does not verify, or has expired | `WWW-Authenticate: Bearer error="invalid_token"` |
| `keys-unavailable` | 503 | The sign-in system's keys cannot be fetched, for a token whose key the API does not hold; a UI retries, and does not sign the user out | |
| `service-timeout` | 504 | A call the route made, to another service, a database, a cache or a compute function, took longer than its deadline, or another service answered 504 | |
| `service-unavailable` | 503 | A service or a database the route called could not be reached or lost the connection, or another service answered 502, 503 or 429; a UI may retry | |
| `service-not-found` | 502 | Another service answered 404 | |
| `service-conflict` | 502 | Another service answered 409, or a database refused a statement for a unique key, or chose it as a deadlock's victim | |
| `service-rejected` | 502 | Another service answered 400 or 422, or a database refused a statement for another constraint | |
| `service-denied` | 502 | Another service answered 401 or 403, or a database refused the API's login or a permission | |
| `service-bad-reply` | 502 | Another service answered with a body that is not the reply the API declares for the call | |
| `service-failed` | 502 | Another service answered 500 or any other status that is not a 2xx, or a database failed the statement | |
| `about:blank` | 404, 405, 500 | No route, the wrong method, or an error the API did not expect, whose problem says nothing more | |

A `service-` problem is a failure of something the route called that its handler did not handle, one of basis's kinds
of service error ([the design](design.md#what-jobs-and-apis-share)); a handler that has something to say about one, as
the example's repository raises its domain's `Unpriced` when the pricing service has no price, which its route answers
as `unpriced`, answers its own error instead. Its
`detail` names what was called, as `pricing answered 503 to GET /prices/US0001`, and never holds what a database's
driver said. An operation lists the kinds that what its route asks for can raise: every kind for another service, all
but `service-not-found` and `service-bad-reply` for a database, and `service-timeout` for a cache or a compute
function.

Each operation lists, under each status, the type and title of every error it may answer, in its description.

## Identity

A UI sends the user's JWT as `Authorization: Bearer TOKEN`; a request without the header is anonymous. In development,
a UI signs its tokens with basis's published development key, which every API refuses except on a declared desktop.
[The design](design.md#apis-basisapi) says what a token is checked for.

## Datasets as Arrow

A route that answers a dataset, rows of one model, declares it with `dataset=True`, and answers Arrow, an IPC stream,
when the request's `Accept` names `application/vnd.apache.arrow.stream` with a `q` above 0 and at least as high as any
JSON range's (`application/json`, `application/*` or `*/*`), and JSON otherwise. `Accept:
application/vnd.apache.arrow.stream` alone asks for Arrow. Both answers carry `Vary: Accept`, and an error is a problem
in JSON whichever was asked for.

The stream holds one record batch of every row, or none when there are no rows, and its schema either way. Each field of
the row's model is a column, named as the JSON names it, nullable only where the field may be null, but for a `float`:
a float column is always nullable, in the Arrow schema and in the document, because NaN and ±inf travel as null.

| Field | Column |
|---|---|
| `str`, `UUID`, or a `Literal` or `Enum` of strings | `utf8` |
| `float` | `float64`, always nullable |
| `bool` | `bool` |
| `date` | `date32` |
| `datetime` | `timestamp[us, tz=UTC]` |

A row holds nothing else: no `int` or `Decimal` (see [Numbers](#numbers)), no nested model, list or map, and no field it
computes, so its JSON and its Arrow hold the same values. An enum or `Literal` in a row is text, so one of integers is
refused, with the fix to use a `str` enum or a `Literal` of strings. A route declared `response=api.Table[Row]`, which
returns a pandas frame or an Arrow table, answers the same JSON and Arrow, its JSON rendered column-wise from the Arrow
(step 2.12). [`dataset.py`](../packages/api/src/basis/api/dataset.py) and [`arrow.py`](../packages/api/src/basis/api/arrow.py)
are the reference.

### Display formats

A dataset's number column may declare its display format, which parallax and @basis/ui's number components format it
by, so a page never chooses decimals. It is the shape agreed with basis-ui on 2026-11-08, which step 2.13 builds. In
Python a column declares it on its field, as `Annotated[float, api.format.money("USD")]` or `api.format.pct(2)`. The
format is a JSON object, written in Arrow as the field metadata key `basis.format`, whose value is the object as a JSON
string, and in the document as `x-basis-format` on each numeric property's schema, the columns declared for an Arrow
response among them.

| Kind | The object |
|---|---|
| A number | `{"kind":"number","decimals":2}` |
| Money | `{"kind":"money","currency":"USD"}`, with an optional `decimals` that defaults to the currency's ISO 4217 minor units |
| A percentage | `{"kind":"pct","decimals":2}`, whose value is a fraction: 0.0525 is 5.25% |
| Basis points | `{"kind":"bps","decimals":1}`, whose value is a fraction, shown ×10,000: 0.0525 is 525 bp |
| A price | `{"kind":"price","decimals":3}` |
| A price in 32nds | `{"kind":"price32","step":"32nd"}`, where `step` is `32nd`, `half`, `quarter` or `eighth`, and defaults to `32nd` |

- `decimals` is optional, an integer from 0 to 15, and each kind has parallax's default.
- `currency` is upper-case ISO 4217. basis ships the code list, with each code's minor units, as data.
- A column without the key gets the formatter's default for its type.
- A UI ignores fields it does not know, and treats an unknown kind as no format.
- basis refuses an unknown kind, a bad currency or an out-of-range `decimals` when the route is declared.

The Python port of parallax's formatter, for Slack and email text (step 5.3), reads the same object.

## One origin

A UI and its API sit behind one host: the ingress routes `/api` to the API, removing the prefix, and the rest to the UI.
The document's paths and the API's routes carry no `/api`, and a UI's client adds it. The browser makes no cross-origin
call, and the API allows none.

An app can serve a built UI folder itself, from step 2.14, which gives one origin without an ingress split. Its routes
then mount under `/api` and the UI at `/`. The document's paths stay unprefixed, as above, and its `servers` lists
`/api`. An app without a UI is unchanged, and the ingress strips `/api` as before.

Where that routing is not possible, the API's settings name each other origin whose pages may call it, per environment,
as the browser sends it: a scheme, `https` or `http`, and a host, with a port only where it is not the scheme's own, in
lower case, and nothing after it.

```sh
BASIS_ALLOWED_ORIGINS='["https://positions.example"]'
```

The API then answers those origins' preflight requests, and lets their pages send `Authorization`, `Content-Type`,
`traceparent` and `tracestate`, with the methods `GET`, `POST`, `PUT`, `PATCH` and `DELETE`, and read `X-Request-Id`,
`traceparent` and `WWW-Authenticate`. It allows no credentialed request, since a UI's token is a header, not a cookie,
and a browser keeps its answer to a preflight request for 600 seconds. Without the setting, or with `[]`, every other origin's page is refused. A `*` is refused when the
settings are read.

## Compatibility

`basis check --full`, which the push hook runs, compares the repo's `openapi.json` with the one at its last release, and
fails on a change that breaks a UI built against that release. The repo declares its release tags, as a glob:

```toml
[tool.basis.api]
releases = "v*"
```

The last release is the nearest tag that matches the glob among the checked commit's ancestors, the commit itself
included, as `git describe --tags --abbrev=0 --match GLOB HEAD` finds it. With no such tag, or none whose commit has an
`openapi.json`, there is no earlier release, and the check passes, saying so. A clone that may lack the release fails
instead, with the fix: a shallow one, as a CI checkout is by default, or one whose remote's `tagOpt` is `--no-tags`, as
`git clone --no-tags` leaves it. `basis review` prints what the check found.

| Change | Breaks a UI |
|---|---|
| A response's field added | No |
| A response's field removed or renamed | Yes |
| A response's field that may hold another type, `null`, an enum's new value, or be absent where it was required, as an integer that becomes a number | Yes |
| A response's field that holds less, as a number that becomes an integer, or any string that becomes a date | No |
| A request's optional field added | No |
| A request's required field added, or a field that becomes required | Yes |
| A request's field removed or renamed | Yes, since the API refuses a field it does not know |
| A request's field that no longer takes a type or an enum's value it took | Yes |
| A request's field that takes another type, or an enum's new value, too, as an integer that becomes a number, or a date that becomes any string | No |
| A request's field that is no longer required | No |
| An operation added | No |
| An operation removed, or moved to another method or path | Yes |
| A new operation id, which renames the client's function | Yes |
| A new success status | Yes |
| A body the route no longer takes, or now requires | Yes |
| A dataset route that no longer answers Arrow | Yes |
| A path parameter renamed | No, since a path's parameters are compared by their place in it |

Errors' problems are basis's, the same in every API, and are not compared; nor are constraints, such as a pattern or a
bound.

## The sample

The example API's document is
[`packages/api/src/basis/api/example/openapi.json`](../packages/api/src/basis/api/example/openapi.json), the sample
basis-ui generates its client from and tests against, pinned to a commit of this repo. It holds every part of the
contract: query and path parameters, a JSON body with numbers and dates, a time with its offset, a dataset route,
`coupons`, with its Arrow, a 422 a handler names its fields in, a route that writes and books its write with another
service through its database's outbox, `record_settlement`, and the errors basis adds, that route's 502 among them. `uv run python -m
basis.api.example` writes it, and the example's tests fail when it differs from the example's service, or when it
breaks its contract with this repo's last `v*` release. Step 2.11 changes it: the example's decimals become floats,
which the document gives as JSON numbers, and `x-basis-decimal` goes, so basis-ui's pinned copy changes with it.
