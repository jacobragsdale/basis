"""`basis smoke --env ENV`, basis-api's side: the API as an environment serves it, after each deploy and from a
desktop. It reaches the API at the base its settings name for the environment, `BASIS_SMOKE__URL`, where its document's
paths are, and checks, each failure one `ERROR` line with its fix:

- that the document it serves, `openapi.json`, is the one the repo commits, but for its first key, the marker, so the
  deployed build is the checked-in commit;
- that it is ready, `health/ready` answering 200;
- that each route that declares a read-only example, `smoke=`, answers it with a 2xx whose body its response holds.

It sends only GETs, and makes no journeys across services (design.md, "Gates without CI")."""

import json
from pathlib import Path

import httpx2
from pydantic import TypeAdapter, ValidationError

from basis.api import routes
from basis.api.service import Route, Service, load
from basis.core import environments
from basis.core.sync import Problem

DOCUMENT = "openapi.json"
TIMEOUT_S = 30.0


def smoke(repo: Path, env: str, *, service: Service | None = None, transport: httpx2.BaseTransport | None = None) -> list[Problem]:
    """Each way the API, as environment `env` serves it, is not what the repo at `repo` commits: the repo's one
    service, or `service`, reached over the network, or through `transport`, as a test stands in for the deploy."""
    service = service if service is not None else load()
    environment = environments.load(env, service.settings)
    reached = getattr(environment.settings, "smoke", None)
    if reached is None:
        return [
            (
                f"basis smoke has nowhere to reach service {service.name} in {env}",
                f"set BASIS_SMOKE__URL in {env}'s settings to the base its document's paths are under there, as https://{env}.example.com/api",
            )
        ]
    base = str(reached.url).rstrip("/")
    verify: bool | str = str(reached.ca_bundle) if reached.ca_bundle is not None else True
    found: list[Problem] = []
    with httpx2.Client(base_url=base, verify=verify, trust_env=False, timeout=TIMEOUT_S, follow_redirects=False, transport=transport) as client:
        found += _document(client, repo, base)
        found += _ready(client, base)
        for route in service.routes:
            if route.smoke is not None:
                found += _example(client, route, base)
    return found


def _document(client: httpx2.Client, repo: Path, base: str) -> list[Problem]:
    committed_path = repo / DOCUMENT
    try:
        committed = json.loads(committed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [(f"{DOCUMENT} cannot be read from the repo: {error}", "run `uv run basis sync`, which writes it")]
    committed.pop("x-basis", None)
    try:
        response = client.get(f"/{DOCUMENT}")
    except httpx2.HTTPError as error:
        return [(f"{base}/{DOCUMENT} cannot be reached: {error}", "check that the deploy finished, and that BASIS_SMOKE__URL is where it serves")]
    if response.status_code != 200:  # noqa: PLR2004 -- OK
        return [(f"{base}/{DOCUMENT} answered {response.status_code}", "check that BASIS_SMOKE__URL is the base the API's paths are under")]
    if response.json() != committed:
        return [(f"the document {base} serves is not this repo's {DOCUMENT}", "deploy this commit, or check out the commit that is deployed")]
    return []


def _ready(client: httpx2.Client, base: str) -> list[Problem]:
    try:
        response = client.get("/health/ready")
    except httpx2.HTTPError as error:
        return [(f"{base}/health/ready cannot be reached: {error}", "check that the deploy finished")]
    if response.status_code != 200:  # noqa: PLR2004 -- OK
        return [(f"{base}/health/ready answered {response.status_code}, so the API is not ready", "look at its logs for why its startup failed, as its pod's show them")]
    return []


def _example(client: httpx2.Client, route: Route, base: str) -> list[Problem]:
    example = route.smoke or {}
    path = route.path
    for name, value in dict(example.get("path") or {}).items():  # ty: ignore[no-matching-overload] -- the example's mapping
        path = path.replace(f"{{{name}}}", str(value))
    query = example.get("query") or {}
    headers = example.get("headers") or {}
    try:
        response = client.get(path, params=query, headers=headers)  # ty: ignore[invalid-argument-type] -- the example's mappings
    except httpx2.HTTPError as error:
        return [(f"{route.name}'s example, GET {base}{path}, cannot be reached: {error}", "check that the deploy finished")]
    if not 200 <= response.status_code < 300:  # noqa: PLR2004 -- a 2xx
        return [(f"{route.name}'s example, GET {base}{path}, answered {response.status_code}", "give it an example the environment answers, or fix what it answers")]
    answered: object = routes.documented(route)
    adapter: TypeAdapter[object] = TypeAdapter(answered)  # ty: ignore[invalid-type-form] -- the route's response, a type held as a value
    try:
        adapter.validate_json(response.content, strict=True)  # a number as a JSON number, as the contract has it
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(map(str, first["loc"]))
        fix = "deploy the commit whose response this is, or fix the route's response"
        return [(f"{route.name}'s example, GET {base}{path}, answered a body its response does not hold: {first['msg']} at {where}", fix)]
    return []
