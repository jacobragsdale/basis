"""Each kind of resource a part reaches besides its databases, as one record: where the settings configure it, its
fake, what `basis doctor --env ENV` checks of each one configured, and what the QA tier refuses of one. A part declares
and opens them its own way, and finds a kind's fake and its checks here, so a new kind is one record more, which
every check iterates.

| Kind | Settings | Fake | doctor checks | The QA tier refuses |
|---|---|---|---|---|
| `http` | `BASIS_HTTP__NAME__*` | `fakes.Http` | that the service answers a GET of its URL, signed in as its login says, whatever its status but a 401 or 403 | nothing: a service is read |
| `slack` | `BASIS_SLACK__NAME__*` | `fakes.Slack` | that Slack's webhook host answers, posting nothing | nothing: a QA run sends nothing |
| `email` | `BASIS_EMAIL__NAME__*` | `fakes.Email` | that the relay greets and takes the login, sending nothing | nothing: a QA run sends nothing |
| `mongo` | `BASIS_MONGO__NAME__*` | `fakes.Mongo` | that the database answers a ping | hosts and a database without the marker |
| `folders` | `BASIS_FOLDERS__NAME__PATH` | none: a test gives a folder | that the folder is there | a path without the marker |
| `lakes` | `BASIS_LAKES__NAME__*` | none: a test gives a filesystem | that the container is reachable, and renames where it may be written | an account and container without the marker |

A database is not one of these: its logins, its ledger and the QA tier's tables are `basis.core.doctor`'s and
`basis.core.qa`'s own, since a database has two logins and a dialect."""

import asyncio
import smtplib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from opentelemetry.trace import NoOpTracer

from basis.core import fakes, lake, mongo, sending
from basis.core.errors import BasisError, ServiceError
from basis.core.opened import client, transport_failure
from basis.core.settings import EmailRelay, FolderSettings, HttpService, LakeSettings, MongoSettings, Settings, SlackChannel

type Problem = tuple[str, str]
"""A failure's reason and its fix."""

PROBE_S = 10.0
"""Seconds `basis doctor` waits on each service, relay and Mongo database it checks."""


@dataclass(frozen=True)
class Probe:
    """What doctor is checking: the environment's name, where its settings come from, and whether it may be written
    here."""

    env: str
    where: str
    writes: bool
    settings: Settings
    lakes: Callable[[str, Settings], lake.Lake] = lake.lake
    """How a lake is opened: a test's, where it gives one."""


@dataclass(frozen=True)
class Kind[S]:
    """One kind of resource: its name, which is its settings' (`BASIS_NAME__...`), its fake, if a test gives one, its
    configured ones, what doctor checks of one, and what the QA tier refuses of one."""

    name: str
    what: str
    """What one is, as `service`, in doctor's lines."""
    fake: type[fakes.Fake] | None
    configured: Callable[[Settings], Mapping[str, S]]
    check: Callable[[str, S, Probe], str]
    """What doctor found of the one named, after its name, as ` answers at URL (200)`; it raises a `BasisError` or `ServiceError` where it is not
    reachable."""
    marked: Callable[[str, S, str, str], Problem | None] | None = None
    """Why the QA tier refuses the one named, given its marker and where its settings are, or None."""

    def doctor(self, settings: Settings, probe: Probe) -> list[tuple[str, Problem | None]]:
        """doctor's line for each one the settings configure, and its problem, if any."""
        found: list[tuple[str, Problem | None]] = []
        for name, configured in sorted(self.configured(settings).items()):
            what = f"{probe.env}'s {self.what} {name}"
            try:
                said = self.check(name, configured, probe)
            except _ProblemError as error:
                found.append((what, error.problem))
                continue
            except (BasisError, ServiceError) as error:
                reason = f"{what} does not answer ({type(error).__name__}: {' '.join(str(error).split())})"
                found.append((what, (reason, f"check {self.variables(name)} in {probe.where}, and that this machine may reach it")))
                continue
            found.append((f"{what}{said}", None))
        return found

    def variables(self, name: str) -> str:
        return f"BASIS_{self.name.upper()}__{name.upper()}__*"


def _http(name: str, service: HttpService, probe: Probe) -> str:
    async def get() -> int:
        async with client(name, service) as reaching:
            return (await reaching.get("", timeout=PROBE_S)).status_code

    try:
        status = asyncio.run(get())
    except httpx2.TimeoutException as error:
        msg = f"{name} did not answer within {PROBE_S:g}s"
        raise BasisError(msg) from error
    except httpx2.RequestError as error:
        raise transport_failure(name, name, f"GET {service.url}", error) from error
    if status in {401, 403}:  # a login the service refuses, or one it asks for and the settings do not give
        prefix = f"BASIS_HTTP__{name.upper()}__LOGIN__"
        if service.login is None:
            raise _ProblemError((f"{probe.env}'s service {name} answers {status} at {service.url}, and its settings give no login", f"set {prefix}KIND and its secrets in {probe.where}"))
        raise _ProblemError((f"{probe.env}'s service {name} refused its login ({status} at {service.url})", f"correct {prefix}* in {probe.where}"))
    return f" answers at {service.url} ({status})"


def _slack(name: str, channel: SlackChannel, probe: Probe) -> str:
    del probe
    parts = urlsplit(channel.webhook.get_secret_value())
    origin = f"{parts.scheme}://{parts.netloc}/"
    try:
        with sending.slack_client(channel) as reaching:
            reaching.head(origin, timeout=PROBE_S)
    except httpx2.TimeoutException as error:
        msg = f"{parts.hostname} did not answer within {PROBE_S:g}s"
        raise BasisError(msg) from error
    except httpx2.RequestError as error:
        raise transport_failure(name, f"{name}'s webhook host", None, error) from error
    return f" reaches {parts.hostname}, posting nothing"


def _email(name: str, relay: EmailRelay, probe: Probe) -> str:
    del probe
    try:
        session = sending.smtp(relay, PROBE_S)
        try:
            if relay.user is not None and relay.password is not None:
                session.login(relay.user, relay.password.get_secret_value())
        finally:
            session.quit()
    except smtplib.SMTPException as error:
        raise sending.smtp_failure(name, error) from error
    except OSError as error:
        msg = f"{name}'s relay {relay.host}:{relay.port} could not be reached: {type(error).__name__}"
        raise BasisError(msg) from error
    return f" reaches {relay.host}:{relay.port}{', and takes the login' if relay.user is not None else ''}, sending nothing"


def _mongo(name: str, settings: MongoSettings, probe: Probe) -> str:
    del probe
    store = mongo.MongoStore(name, settings, NoOpTracer())
    try:
        store.ping()
    finally:
        store.close()
    return f" answers, database {settings.database}"


def _folder(name: str, shared: FolderSettings, probe: Probe) -> str:
    try:
        reachable = Path(shared.path).is_dir()
    except OSError:
        reachable = False
    if not reachable:
        raise _ProblemError((f"{probe.env}'s folder {name}, at {shared.path}, is not there or not reachable from here", f"mount it, or correct BASIS_FOLDERS__{name.upper()}__PATH in {probe.where}"))
    return f": {shared.path}"


class _ProblemError(BasisError):
    """A check whose problem is its kind's own, not `does not answer`."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(*problem)


def _mongo_marked(name: str, settings: MongoSettings, marker: str, where: str) -> Problem | None:
    """Mongo whose hosts and database name all lack the marker, where a QA run reads and upserts; a URI's login is never
    named."""
    hosts = [part.rsplit("@", 1)[-1].split(":", 1)[0] for part in urlsplit(settings.uri.get_secret_value()).netloc.split(",")]
    if any(carries(place, marker) for place in [*hosts, settings.database]):
        return None
    reason = (
        f"BASIS_MONGO__{name.upper()}__URI and __DATABASE {where} name host {', '.join(hosts)} and database {settings.database},"
        f" and neither contains the QA marker {marker!r}, and a QA run reads and upserts there"
    )
    return reason, "point it at QA's Mongo, whose host or database contains the marker"


def _folder_marked(name: str, shared: FolderSettings, marker: str, where: str) -> Problem | None:
    if carries(shared.path, marker):
        return None
    reason = f"BASIS_FOLDERS__{name.upper()}__PATH {where} is {shared.path}, which does not contain the QA marker {marker!r}, and a QA run writes its workbooks there"
    return reason, "point it at QA's folder, whose path contains the marker"


def _lake_marked(name: str, settings: LakeSettings, marker: str, where: str) -> Problem | None:
    """A lake whose account and container both lack the marker, where a QA run would archive."""
    if carries(f"{settings.account}/{settings.container}", marker):
        return None
    reason = f"BASIS_LAKES__{name.upper()}__ACCOUNT and __CONTAINER {where} are {settings.account}/{settings.container}, which lacks the QA marker {marker!r}, and a QA run archives there"
    return reason, "point it at QA's lake, whose account or container contains the marker"


def carries(name: str, marker: str) -> bool:
    """Whether `name` contains the QA marker, whatever its case."""
    return marker.lower() in name.lower()


def _lake(name: str, found: LakeSettings, probe: Probe) -> str:
    del found
    try:
        return f": {lake.check(probe.lakes(name, probe.settings), rename=probe.writes)}"
    except BasisError as error:
        problem = (f"{probe.env}'s lake {name} cannot be archived to ({' '.join(str(error).split())})", f"check BASIS_LAKES__{name.upper()}__* in {probe.where}")
        raise _ProblemError(problem) from error


HTTP: Kind[HttpService] = Kind("http", "service", fakes.Http, lambda settings: settings.http, _http)
SLACK: Kind[SlackChannel] = Kind("slack", "Slack channel", fakes.Slack, lambda settings: settings.slack, _slack)
EMAIL: Kind[EmailRelay] = Kind("email", "email", fakes.Email, lambda settings: settings.email, _email)
MONGO: Kind[MongoSettings] = Kind("mongo", "Mongo database", fakes.Mongo, lambda settings: settings.mongo, _mongo, _mongo_marked)
FOLDERS: Kind[FolderSettings] = Kind("folders", "folder", None, lambda settings: settings.folders, _folder, _folder_marked)
LAKES: Kind[LakeSettings] = Kind("lakes", "lake", None, lambda settings: settings.lakes, _lake, _lake_marked)

KINDS: tuple[Kind[Any], ...] = (FOLDERS, LAKES, HTTP, SLACK, EMAIL, MONGO)
"""Every kind, in the order doctor checks them."""


def qa_problems(settings: Settings, marker: str, where: str) -> list[Problem]:
    """Why the QA tier refuses each resource the settings configure that does not carry the marker."""
    found: list[Problem] = []
    for kind in KINDS:
        if kind.marked is None:
            continue
        for name, configured in sorted(kind.configured(settings).items()):
            if (problem := kind.marked(name, configured, marker, where)) is not None:
                found.append(problem)
    return found
