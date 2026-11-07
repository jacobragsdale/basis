"""A database driver's errors as the kinds of `basis.core.errors.ServiceError`, which each driver's module tells apart
as narrowly as its driver allows (`mssql.kind_of`, `oracle.kind_of`):

| Kind | A database raises it for |
|---|---|
| `Timeout` | A statement past its timeout |
| `Unavailable` | A database that cannot be reached, or a connection lost |
| `Denied` | A login or a permission refused |
| `Conflict` | A unique key violated, or a deadlock that chose the statement |
| `Rejected` | Any other constraint violated |
| `Failed` | Anything else |

A statement is never made again. Each kind's detail says what happened in basis's words, and its reason is the
driver's own message, which can hold the server's name, a constraint's or the values refused, so only the log and a
run's record hold it, never an API's problem. A driver's error raised while a block's failure is being handled, as a
rollback on the connection a call timeout broke, is logged, and the block's failure is the one raised."""

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager

from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable

log = logging.getLogger(__name__)
_DETAILS: dict[type[ServiceError], str] = {
    Timeout: "database {name}'s statement did not finish within its timeout",
    Unavailable: "database {name} could not be reached, or lost the connection",
    Denied: "database {name} refused the login, or a permission",
    Conflict: "database {name} refused the statement for a unique key, or chose it as a deadlock's victim",
    Rejected: "database {name} refused the statement for a constraint",
    Failed: "database {name} failed the statement",
}


def failure(database: str, kind: type[ServiceError], error: BaseException) -> ServiceError:
    """`error`, a driver's, as the `kind` of failure of `database`, with the driver's message as its reason and its
    notes, such as which server it was connecting to."""
    found = kind(_DETAILS[kind].format(name=database), resource=database, reason=" ".join(str(error).split()))
    for note in getattr(error, "__notes__", []):
        found.add_note(str(note))
    return found


@contextmanager
def failures(database: str, driver: tuple[type[BaseException], ...], kind_of: Callable[[BaseException], type[ServiceError]]) -> Generator[None]:
    """Within the block, an error of `driver` raised again as the kind of failure of `database` that `kind_of` says it
    is. A `ServiceError` passes as it is, so blocks may nest; and one that a driver's error raised while ending the
    block, such as its rollback's, interrupted is raised again in its place, with its own cause and context."""
    first: ServiceError | None = None
    try:
        yield
    except driver as error:
        if not isinstance(error.__context__, ServiceError):
            raise failure(database, kind_of(error), error) from error
        first = error.__context__
        log.warning("database %s failed again while ending what its failure stopped; the first failure is the one raised", database, exc_info=error, extra={"database": database})
    if first is not None:
        raise first  # outside the handler, so it keeps the cause and the context it had
