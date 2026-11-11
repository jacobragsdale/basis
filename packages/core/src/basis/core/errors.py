"""The failures basis raises on purpose: a `BasisError`, which the caller can fix, and a `ServiceError`, which a resource
raises when what it reaches fails. The module is pure, so job code, and an API's declarations, may raise or catch one.

A `BasisError`'s message gives the reason and ends with the fix, and the `basis` command prints it as one line, with no
traceback; every other exception is a defect, whose traceback the command logs. A kind that stands for a builtin error
also subclasses it, such as `SettingsError(BasisError, ValueError)`, so code that catches the builtin still catches it.

A `ServiceError` is one of eight kinds, the same whatever was called, another service, a database, a cache or a compute
function, and whether it is real or a fake, so a failure means one thing everywhere:

| Kind | Raised for | Retried by basis |
|---|---|---|
| `Timeout` | The call's deadline passing, or another service's 504 | Never |
| `Unavailable` | A resource that cannot be reached or lost the connection, or another service's 502, 503 or 429 | An HTTP GET, with backoff, within its deadline |
| `NotFound` | Another service's 404 | Never |
| `Conflict` | Another service's 409; a database's unique key, or a deadlock that chose the statement | Never |
| `Rejected` | Another service's 400 or 422; a database's other constraints | Never |
| `Denied` | Another service's 401 or 403; a database's refusal of the login or of a permission | Never |
| `BadReply` | A reply that does not match the model it is declared to be | Never |
| `Failed` | Another service's 500, and anything else | Never |

Code that has something to say about a kind catches it, as a repository turns `NotFound` into None, and lets the others
go: an API answers each as its problem (`basis.api.errors`), and a job's run fails, its reason naming the kind."""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from typing import NoReturn


class BasisError(Exception):
    """A failure the caller can fix, raised on purpose; its message gives the reason and ends with the fix. Raised as
    `BasisError(reason, fix)`, it keeps each; raised with the whole message, as most are, each is the message split at
    its last `: `."""

    _given: tuple[str, str] | None = None

    def __init__(self, reason: str = "", fix: str | None = None) -> None:
        super().__init__(reason if fix is None else f"{reason}: {fix}")
        self._given = (reason, fix) if fix is not None else None

    @property
    def problem(self) -> tuple[str, str]:
        """The reason and the fix, notes left out."""
        if self._given is not None:
            return self._given
        reason, _, fix = str(self).rpartition(": ")
        return (reason, fix) if reason else (str(self), "fix it")

    @property
    def reason(self) -> str:
        return self.problem[0]

    @property
    def fix(self) -> str:
        return self.problem[1]


@contextmanager
def deliberate(*kinds: type[Exception]) -> Generator[None]:
    """Within the block, an exception of `kinds` is a failure the caller can fix, raised again as a `BasisError` with
    the same message and notes: for a call into code that raises a builtin error on purpose, such as a lookup that
    finds no run. Keep the block to that call, so a defect of the same kind elsewhere is not taken for one."""
    try:
        yield
    except kinds as error:
        raised = BasisError(str(error))
        for note in getattr(error, "__notes__", []):
            raised.add_note(str(note))
        raise raised from error


class ServiceError(Exception):
    """A resource's failure: one of the kinds below, never this class itself.

    `detail` says what happened in basis's words, naming the resource, as `pricing answered 503 to GET /prices/S1`,
    which an API's problem shows its caller; `reason`, where there is one, is what the resource itself said, such as a
    database driver's message, which only the log and a run's record hold: `str(error)` is the detail and the reason."""

    def __init__(self, detail: str, *, resource: str, status: int | None = None, reason: str | None = None) -> None:
        if type(self) is ServiceError:
            msg = "ServiceError is the base of the kinds of a resource's failure, and is never raised itself: raise one of them, such as Unavailable"
            raise TypeError(msg)
        super().__init__(detail if reason is None else f"{detail}: {reason}")
        self.detail = detail
        self.resource = resource
        """The resource's name, as it is declared, such as `pricing`."""
        self.status = status
        """The HTTP status another service answered with, or None where there was no reply, as for a database."""
        self.reason = reason


class Timeout(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """The call's deadline passed, or another service answered 504."""


class Unavailable(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """The resource could not be reached, or lost the connection, or another service answered 502, 503 or 429. An HTTP
    GET is made again, with backoff, while its deadline allows; nothing else is."""


class NotFound(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """Another service answered 404."""


class Conflict(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """Another service answered 409, or a database refused a statement for a unique key, or chose it as a deadlock's
    victim."""


class Rejected(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """Another service answered 400 or 422, or a database refused a statement for a constraint other than a unique key."""


class Denied(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """Another service answered 401 or 403, or a database refused the login or a permission."""


class BadReply(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """A reply that does not match the model it is declared to be."""


class Failed(ServiceError):  # noqa: N818 -- the kind's name, as the design gives it, which a caller reads as what happened
    """Another service answered 500 or anything else that is not a 2xx, or a resource failed in another way."""


KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, NotFound, Conflict, Rejected, Denied, BadReply, Failed)
"""Every kind of a resource's failure."""
DATABASE_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, Conflict, Rejected, Denied, Failed)
"""The kinds a database's statements raise, real or fake (`basis.core.databases.failures`); it never raises a
`NotFound` or a `BadReply`."""

_ANSWERED: dict[int, type[ServiceError]] = {
    HTTPStatus.GATEWAY_TIMEOUT: Timeout,
    HTTPStatus.BAD_GATEWAY: Unavailable,
    HTTPStatus.SERVICE_UNAVAILABLE: Unavailable,
    HTTPStatus.TOO_MANY_REQUESTS: Unavailable,
    HTTPStatus.NOT_FOUND: NotFound,
    HTTPStatus.CONFLICT: Conflict,
    HTTPStatus.BAD_REQUEST: Rejected,
    HTTPStatus.UNPROCESSABLE_CONTENT: Rejected,
    HTTPStatus.UNAUTHORIZED: Denied,
    HTTPStatus.FORBIDDEN: Denied,
}


def answered(status: int) -> type[ServiceError]:
    """The kind of failure that another service's reply with `status`, which is not a 2xx, is."""
    return _ANSWERED.get(status, Failed)


class RenamedError(BasisError):
    """A name of basis's surface that a release renamed, used by its old name: refused, with its replacement, for one
    release, before the old name goes (`scripts/surface.py`). Not an `AttributeError`, so `from basis.job import OLD`
    says why, rather than that it cannot import the name."""


def missing(module: str, name: str, renamed: Mapping[str, str]) -> NoReturn:
    """A module's `__getattr__` past the names it has: a name in `renamed` is refused with what replaced it, and any
    other is no attribute of the module's."""
    if (replacement := renamed.get(name)) is not None:
        reason = f"{module}.{name} is renamed {module}.{replacement}"
        raise RenamedError(reason, f"use {replacement}")
    msg = f"module {module!r} has no attribute {name!r}"
    raise AttributeError(msg)
