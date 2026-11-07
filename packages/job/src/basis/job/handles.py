"""What a run gives a transform for each `Clock` and `Ids` its `Inputs` declare: basis-core's, whose spans record each
value, keeping every value in order for the run's record (`RunRecord.clocks` and `RunRecord.ids`), so that a replay of
the run gives the transform the same values again, in the same order. A run's clock is the system's, and its ids are
UUIDv7s, unless a test fakes them by name, as a run on a fixture does.

A replay whose transform asks for more than its run kept, as one changed since may, gets, past them, the last time its
run kept, or the run's start where it kept none, and new ids, and logs once for each clock or ids that it did."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import overload
from uuid import UUID, uuid7

from opentelemetry.trace import Tracer

from basis.core import fakes, resources
from basis.core.opened import NewIds, SystemClock, utc_now
from basis.core.snapshots import RunRecord
from basis.job.inputs import Clock, Handles, Ids, Inputs
from basis.job.inputs import NewIds as NewIdsOf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kept:
    """The handles a run gives its transform, and the values they have given, by name."""

    handles: Handles
    clocks: dict[str, list[datetime]] = field(default_factory=dict[str, list[datetime]])
    ids: dict[str, list[UUID]] = field(default_factory=dict[str, list[UUID]])


def live(inputs: type[Inputs], given: Mapping[str, fakes.Fake], tracer: Tracer) -> Kept:
    """The handles of a run: the system's clock and new UUIDv7s, or the fakes `given` names for them."""

    def time_of(name: str) -> Callable[[], datetime]:
        return fake.now if isinstance(fake := given.get(name), fakes.Clock) else utc_now

    def ids_of(name: str) -> Callable[[], UUID]:
        return fake.new if isinstance(fake := given.get(name), fakes.Ids) else uuid7

    return _built(inputs, tracer, time_of, ids_of)


def replayed(inputs: type[Inputs], record: RunRecord, tracer: Tracer) -> Kept:
    """The handles of a replay of `record`'s run, which give the values the run kept, in order."""

    def time_of(name: str) -> Callable[[], datetime]:
        times = record.clocks.get(name, [])
        return _Again(times, _Last(times[-1] if times else record.started_at), f"clock {name}", record.run_id)

    def ids_of(name: str) -> Callable[[], UUID]:
        return _Again(record.ids.get(name, []), uuid7, f"ids {name}", record.run_id)

    return _built(inputs, tracer, time_of, ids_of)


def faked(inputs: type[Inputs], now: datetime | None, *, fix: str) -> dict[str, fakes.Fake]:
    """A fake of each clock and ids `inputs` declare, by name: each clock at `now`, and ids that count up from 1, as a
    fixture's run and a transform's test have them. A clock without `now` is refused, with `fix`, which says where to
    give it."""
    given: dict[str, fakes.Fake] = {}
    for name, declared in inputs.declared_resources().items():
        if isinstance(declared, Ids):
            given[name] = fakes.Ids()
        elif now is not None:
            given[name] = fakes.Clock(at=now)
        else:
            msg = f"{inputs.__qualname__}.{name} is a clock, and no time is given for it to read: {fix}"
            raise ValueError(msg)
    return given


def _built(inputs: type[Inputs], tracer: Tracer, time_of: Callable[[str], Callable[[], datetime]], ids_of: Callable[[str], Callable[[], UUID]]) -> Kept:
    """The handles of each clock and ids `inputs` declare, basis-core's, each giving what `time_of` or `ids_of` gives
    it by name, and keeping every value."""
    kept_clocks: dict[str, list[datetime]] = {}
    kept_ids: dict[str, list[UUID]] = {}
    clocks: dict[str, resources.Clock] = {}
    ids: dict[str, NewIdsOf] = {}
    for name, declared in inputs.declared_resources().items():
        if isinstance(declared, Clock):
            clocks[name] = SystemClock(declared.resource, _keeping(time_of(name), kept_clocks.setdefault(name, [])), tracer)
        else:
            ids[name] = _Ids(NewIds(declared.resource, _keeping(ids_of(name), kept_ids.setdefault(name, [])), tracer))
    return Kept(Handles(clocks, ids), kept_clocks, kept_ids)


def _keeping[T](source: Callable[[], T], values: list[T]) -> Callable[[], T]:
    def give() -> T:
        value = source()
        values.append(value)
        return value

    return give


@dataclass(frozen=True)
class _Last:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class _Again[T]:
    """The values a run kept, in order, and then `after`'s, logged once."""

    def __init__(self, values: Sequence[T], after: Callable[[], T], what: str, run_id: str) -> None:
        self._values = list(values)
        self._at = 0
        self._after = after
        self._what = what
        self._run_id = run_id

    def __call__(self) -> T:
        if self._at < len(self._values):
            self._at += 1
            return self._values[self._at - 1]
        if self._at == len(self._values):
            self._at += 1
            log.warning("the replay asks %s for more than run %s kept: the rest are a replay's own", self._what, self._run_id, extra={"resource": self._what, "kept": len(self._values)})
        return self._after()


class _Ids:
    """Basis-core's ids, as a transform makes them: one, or a column's."""

    def __init__(self, ids: NewIds) -> None:
        self._ids = ids

    @overload
    def new(self) -> UUID: ...
    @overload
    def new(self, count: int) -> list[UUID]: ...
    def new(self, count: int | None = None) -> UUID | list[UUID]:
        return self._ids.new() if count is None else self._ids.many(count)
