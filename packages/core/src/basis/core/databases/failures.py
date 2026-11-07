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

The kinds are converted once, where basis calls the driver (`Boundary`): each driver's module wraps the driver's
connection and its cursors, so every call through them raises its kind as it fails, whichever reader, writer or
statement makes it, and code inside a transaction's block may catch one, as `except Conflict`. A statement is never
made again. Each kind's detail says what happened in basis's words, and its reason is the driver's own message, which
can hold the server's name, a constraint's or the values refused, so only the log and a run's record hold it, never an
API's problem.

What ends a block, its rollback and its connection's close, is marked as its cleanup (`ending`, `quietly`): after a
block that failed, a cleanup that fails too, as a rollback on the connection a call timeout broke, is logged, and the
block's own failure is the one raised. A failure raised while code handles an earlier one, as an `UPDATE` in an
`except Conflict`, is a failure of its own, and is raised."""

import logging
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from basis.core.errors import DATABASE_KINDS, Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable

__all__ = ["DATABASE_KINDS", "Boundary", "ending", "failure", "failures", "quietly"]

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
    is, with the driver's error as its cause."""
    try:
        yield
    except driver as error:
        raise failure(database, kind_of(error), error) from error


@dataclass(frozen=True)
class Boundary:
    """Where basis calls one database's driver: each call made through it raises the driver's errors as their kinds."""

    database: str
    driver: tuple[type[BaseException], ...]
    kind_of: Callable[[BaseException], type[ServiceError]]

    def call[**P, T](self, made: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        with failures(self.database, self.driver, self.kind_of):
            return made(*args, **kwargs)

    def each[T](self, items: Iterable[T]) -> Iterator[T]:
        """Each of `items`, a driver's iterator, which fetches as it goes, each fetch a call through the boundary."""
        with failures(self.database, self.driver, self.kind_of):
            found = iter(items)
        while True:
            with failures(self.database, self.driver, self.kind_of):
                try:
                    item = next(found)
                except StopIteration:
                    return
            yield item


@contextmanager
def ending(database: str, end: Callable[[], object], what: str) -> Generator[None]:
    """Run `end`, the cleanup of the block, such as its transaction's rollback, once the block is done. After a block
    that raised, an `end` that fails too is logged, and the block's error is the one raised; after one that did not,
    `end`'s failure is raised."""
    try:
        yield
    except BaseException:
        quietly(database, end, what)
        raise
    end()


def quietly(database: str, end: Callable[[], object], what: str) -> None:
    """Run `end`, a cleanup whose failure changes nothing, as closing a connection does, or one after a failure that is
    the one to raise: its failure is logged, and never raised."""
    try:
        end()
    except Exception:  # logged: the failure that ended the block, if any, is the one to raise
        log.warning("database %s could not %s; the failure that ended its work, if any, is the one raised", database, what, exc_info=True, extra={"database": database})
