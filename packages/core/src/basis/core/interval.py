"""The run's interval: the span of time a run is for, half-open, in UTC. A job that has no notion of one runs without
it. A query binds it as `interval_start` and `interval_end`, naive UTC datetimes (Oracle reserves `START`, so the
names are long); a date compares with them as its midnight."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import override


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            msg = f"an interval's ends are instants: give {self.start} and {self.end} a zone"
            raise ValueError(msg)
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.start >= self.end:
            msg = f"an interval starts before it ends: {self}"
            raise ValueError(msg)

    @classmethod
    def parse(cls, text: str) -> Interval:
        """`START..END`, each a date, meaning its midnight UTC, or an ISO datetime with an offset, such as
        `2026-11-05..2026-11-06` or `2026-11-05T06:00Z..2026-11-05T07:00Z`."""
        start, separator, end = text.partition("..")
        if not separator:
            msg = f"an interval is START..END, as 2026-11-05..2026-11-06, not {text!r}"
            raise ValueError(msg)
        return cls(_instant(start), _instant(end))

    def binds(self) -> dict[str, datetime]:
        """The interval as query binds: naive UTC, which every driver binds without a zone conversion of its own."""
        return {"interval_start": self.start.replace(tzinfo=None), "interval_end": self.end.replace(tzinfo=None)}

    @override
    def __str__(self) -> str:
        return f"{_text(self.start)}..{_text(self.end)}"


def _instant(text: str) -> datetime:
    text = text.strip()
    if len(text) == len("2026-11-05"):
        return datetime.combine(date.fromisoformat(text), time(), UTC)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        msg = f"{text!r} has no offset: write it as {text}Z for UTC, or with its offset"
        raise ValueError(msg)
    return moment


def _text(moment: datetime) -> str:
    return moment.date().isoformat() if moment.time() == time() else moment.isoformat().replace("+00:00", "Z")
