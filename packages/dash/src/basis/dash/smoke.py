"""`basis smoke --env ENV [--commit SHA]`, basis-dash's side: the app as an environment serves it, after each deploy and
from a desktop, each failure one `ERROR` line with its fix. It reaches the app at the address its settings name for the
environment, `BASIS_SMOKE__URL`, as a browser opens it, its prefix included, and checks:

- that it is ready, `health/ready` answering 200, and with `--commit`, that it serves that commit;
- that every page answers: its URL with Dash's index, and Dash Pages' router with its layout, as a browser asks for it;
- that every dataset that declares a smoke example, `smoke=`, loads with it, in this process, through the
  environment's own settings, read-only as ever, as `basis load --env ENV` would.

It sends only GETs and the router's one POST, which changes nothing, and makes no journeys across services
(design.md, "Gates without CI")."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2
from pydantic import ValidationError
from pydantic_settings import SettingsError as SourceError

from basis.core import environments
from basis.core.errors import BasisError, ServiceError
from basis.core.sync import Problem
from basis.dash import declarations, internals
from basis.dash.app import App
from basis.dash.driving import Json, driving
from basis.dash.loading import load
from basis.dash.loads import load as load_dataset
from basis.dash.settings import Settings, unreadable

TIMEOUT_S: Final = 30.0
ROUTER: Final = "_pages_content"
"""The component Dash Pages' router answers a page's layout into."""


def smoke(repo: Path, env: str, *, commit: str | None = None, app: App | None = None, transport: httpx2.BaseTransport | None = None, fakes: Mapping[Any, object] | None = None) -> list[Problem]:
    """Each way the app, as environment `env` serves it, is not ready, does not answer a page, or cannot load a dataset
    it gives an example of: the repo's one app, or `app`, reached over the network, or through `transport`, and its
    datasets loaded through the environment's resources, or `fakes`, as a test stands in for the deploy. `repo` is the
    repo's folder, which the app's own code was installed from."""
    del repo  # the app is the installed one, as `basis serve` serves it
    app = app if app is not None else load()
    try:
        environment = environments.load(env, app.settings)
    except (ValidationError, SourceError) as error:
        raise unreadable(error, app.settings) from None
    found: list[Problem] = []
    reached = environment.settings.smoke
    if reached is None:
        fix = f"set BASIS_SMOKE__URL in {env}'s settings to the app's address there, as a browser opens it, as https://{env}.example.com/apps/{app.name}/"
        found.append((f"basis smoke has nowhere to reach app {app.name} in {env}", fix))
    else:
        base = reached.url if reached.url.endswith("/") else f"{reached.url}/"
        verify: bool | str = str(reached.ca_bundle) if reached.ca_bundle is not None else True
        with httpx2.Client(base_url=base, verify=verify, trust_env=False, timeout=TIMEOUT_S, follow_redirects=False, transport=transport) as client:
            ready = _ready(client, base, commit)
            found += ready
            if not ready:
                found += _pages(client, app, base)
    found += _datasets(app, environment.settings, environment.name, fakes)
    return found


def _ready(client: httpx2.Client, base: str, commit: str | None) -> list[Problem]:
    try:
        response = client.get("health/ready")
    except httpx2.HTTPError as error:
        return [(f"{base}health/ready cannot be reached: {error}", "check that the deploy finished, and that BASIS_SMOKE__URL is where it serves")]
    if response.status_code != 200:  # noqa: PLR2004 -- OK
        return [(f"{base}health/ready answered {response.status_code}, so the app is not ready", "look at its logs for why its startup failed, as its pod's show them")]
    try:
        answered: object = response.json()
    except ValueError:
        kind = response.headers.get("content-type", "no content type").partition(";")[0]
        return [(f"{base}health/ready answered {response.status_code} with {kind}, not JSON", _ADDRESS_FIX)]
    served = answered.get("commit") if isinstance(answered, dict) else None
    if commit is not None and served != commit:
        what = f"commit {served}" if served is not None else "no commit (its image sets no BASIS_GIT_SHA)"
        return [(f"{base} serves {what}, not {commit}", f"wait until the deploy of {commit} finishes, and check that its release reached the environment")]
    return []


_ADDRESS_FIX = "check that BASIS_SMOKE__URL is the app's address, as a browser opens it, its prefix included"


def _pages(client: httpx2.Client, app: App, base: str) -> list[Problem]:
    found: list[Problem] = []
    for page in app.pages:
        url = f"{base}{page.path.lstrip('/')}"
        try:
            response = client.get(page.path.lstrip("/"))
        except httpx2.HTTPError as error:
            found.append((f"page {url} cannot be reached: {error}", "check that the deploy finished"))
            continue
        if response.status_code != 200 or "_dash-config" not in response.text:  # noqa: PLR2004 -- OK
            found.append((f"page {url} answered {response.status_code} without Dash's index", _ADDRESS_FIX))
    if found:
        return found
    try:
        dependencies: Json = client.get(internals.DEPENDENCIES).json()
    except (httpx2.HTTPError, ValueError) as error:
        return [(f"{base}{internals.DEPENDENCIES} did not answer the app's callbacks: {error}", _ADDRESS_FIX)]
    router = next((entry for entry in dependencies if isinstance(entry, dict) and str(entry.get("output", "")).startswith(f"..{ROUTER}.")), None) if isinstance(dependencies, list) else None
    if router is None:
        return [(f"{base}{internals.DEPENDENCIES} lists no page router", _ADDRESS_FIX)]
    prefix = urlsplit(base).path.rstrip("/")
    for page in app.pages:
        found += _routed(client, router, f"{base}{page.path.lstrip('/')}", f"{prefix}{page.path}")
    return found


def _routed(client: httpx2.Client, router: Mapping[str, Json], url: str, pathname: str) -> list[Problem]:
    """Whether Dash Pages' router answers the page at `pathname` with its layout, as a browser at `url` asks it."""
    values = {"pathname": pathname, "search": "", "hash": "", "href": url}
    inputs = [{"id": one["id"], "property": one["property"], "value": values.get(str(one["property"]))} for one in _list(router.get("inputs")) if isinstance(one, dict)]
    state = [{"id": one["id"], "property": one["property"], "value": None} for one in _list(router.get("state")) if isinstance(one, dict)]
    body = internals.request(str(router["output"]), inputs, state=state, changed=[f"{one['id']}.{one['property']}" for one in inputs])
    try:
        response = client.post(internals.UPDATE, json=body)
    except httpx2.HTTPError as error:
        return [(f"page {url}'s layout cannot be reached: {error}", "check that the deploy finished")]
    try:
        answered: Json = response.json() if response.status_code == 200 else None  # noqa: PLR2004 -- OK
    except ValueError:
        answered = None
    content = answered.get("response", {}).get(ROUTER, {}).get("children") if isinstance(answered, dict) else None
    if response.status_code != 200 or content is None:  # noqa: PLR2004 -- OK
        return [(f"page {url} answered no layout (status {response.status_code})", "look at the app's logs for the request, whose record names the error")]
    return []


def _list(found: Json) -> list[Json]:
    return list(found) if isinstance(found, list) else []


def _datasets(app: App, settings: Settings, environment: str, fakes: Mapping[Any, object] | None) -> list[Problem]:
    examples = [(dataset, dataset.smoke) for dataset in declarations.datasets(app.resources) if dataset.smoke is not None]
    if not examples:
        return []
    found: list[Problem] = []
    try:
        with driving(app, settings=settings, environment=environment, fakes=fakes) as drive:
            portal = drive.client.http.portal
            if portal is None:  # the client is open, so its portal is
                return []
            for dataset, example in examples:
                try:
                    params = example if isinstance(example, Mapping) else example()
                    portal.call(_load, dataset, {str(name): value for name, value in params.items()})
                except (BasisError, ServiceError) as error:
                    found.append((f"dataset {dataset.name} did not load in {environment} with its smoke example", " ".join(str(error).split())))
    except (BasisError, ValidationError) as error:
        return [(f"app {app.name} did not start on {environment}'s settings, so basis smoke loaded none of its datasets", " ".join(str(error).split()))]
    return found


async def _load(dataset: declarations.Dataset, params: Mapping[str, object]) -> None:
    await load_dataset(dataset, **params)
