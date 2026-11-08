"""Slack and email as a part sends them, through their fakes, which stand in for Slack and the relay alone: the part's
poster and mailer, each send's span and the kind of each failure; and `deliver`, which records each message sent in
its ledger, so a resume never sends one twice and a rerun sends again only when told."""

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from basis.core import fakes, messages, sending
from basis.core.databases import LedgerEntry
from basis.core.errors import Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable
from basis.core.interval import Interval
from basis.core.messages import Mail
from basis.core.resources import ResourceError
from basis.core.settings import GivenSettings, SettingsError
from basis.core.snapshots import MessageRecord

DESK = messages.slack("desk", deadline_s=5)
REPORT = messages.email("report", deadline_s=5)
RELAY = {"host": "smtp.example.com", "port": 587, "security": "starttls", "sender": "etl@example.com", "to": ["desk@example.com", "risk@example.com"]}
SETTINGS = GivenSettings.model_validate({"slack": {"desk": {"webhook": "https://hooks.slack.com/services/T/B/SECRET"}}, "email": {"report": RELAY | {"user": "etl", "password": "p"}}})
DAY = Interval.parse("2026-11-05..2026-11-06")
AT = datetime(2026, 11, 6, 6, tzinfo=UTC)


def spanned() -> tuple[Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_a_post_reaches_the_channel_in_a_span_that_holds_its_hash_and_never_the_webhook() -> None:
    tracer, exporter = spanned()
    channel = fakes.Slack()
    sending.poster(DESK, SETTINGS, tracer, fake=channel).post("*positions*: 3 accounts")
    assert channel.posted == ["*positions*: 3 accounts"]
    [span] = exporter.get_finished_spans()
    assert (span.name, dict(span.attributes or {})) == ("desk post", {"basis.resource": "desk", "basis.sha256": messages.digest("*positions*: 3 accounts"), "http.response.status_code": 200})
    assert "SECRET" not in json.dumps(dict(span.attributes or {}))


@pytest.mark.parametrize("kind", fakes.SLACK_KINDS, ids=lambda kind: kind.__name__)
def test_a_channel_that_fails_raises_the_kind_of_its_failure_and_posts_nothing(kind: type[ServiceError]) -> None:
    channel = fakes.Slack()
    channel.fails(kind, times=1)
    poster = sending.poster(DESK, SETTINGS, spanned()[0], fake=channel)
    with pytest.raises(kind) as raised:
        poster.post("hello")
    assert raised.value.resource == "desk"
    assert channel.posted == []
    poster.post("hello")  # once, then as a channel that answers
    assert channel.posted == ["hello"]


def test_a_mail_goes_from_the_sender_to_the_people_the_settings_name_through_the_relays_login() -> None:
    tracer, exporter = spanned()
    relay = fakes.Email()
    sending.mailer(REPORT, SETTINGS, tracer, fake=relay).send(Mail(subject="positions for 2026-11-05", text="3 accounts, $1,234.50"))
    [sent] = relay.sent
    assert (sent["From"], sent["To"], sent["Subject"], sent.get_content().strip()) == ("etl@example.com", "desk@example.com, risk@example.com", "positions for 2026-11-05", "3 accounts, $1,234.50")
    assert relay.logins == ["etl"]
    [span] = exporter.get_finished_spans()
    assert (span.name, (span.attributes or {})["server.address"]) == ("report send", "smtp.example.com")


@pytest.mark.parametrize("kind", fakes.EMAIL_KINDS, ids=lambda kind: kind.__name__)
def test_a_relay_that_fails_raises_the_kind_of_its_failure(kind: type[ServiceError]) -> None:
    relay = fakes.Email()
    relay.fails(kind)
    with pytest.raises(kind):
        sending.mailer(REPORT, SETTINGS, spanned()[0], fake=relay).send(Mail(subject="s", text="t"))
    assert relay.sent == []


@pytest.mark.parametrize(
    ("call", "says"),
    [
        (lambda: sending.poster(messages.slack("risk", deadline_s=5), SETTINGS, spanned()[0]), "the Slack channel risk is not configured, and a message posts to it: set BASIS_SLACK__RISK__WEBHOOK"),
        (lambda: sending.mailer(messages.email("risk", deadline_s=5), SETTINGS, spanned()[0]), "set BASIS_EMAIL__RISK__HOST, BASIS_EMAIL__RISK__PORT, BASIS_EMAIL__RISK__SECURITY"),
    ],
)
def test_a_channel_or_email_the_settings_do_not_configure_is_refused_naming_its_settings(call: Callable[[], object], says: str) -> None:
    with pytest.raises(SettingsError, match=says):
        call()


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda: Mail(subject="two\nlines", text="t"), "a mail's subject is one line"),
        (lambda: Mail(subject="s", text="  "), "a mail's text is what it says"),
        (lambda: messages.checked_text(""), "a Slack message is some text"),
        (lambda: messages.slack("Desk", deadline_s=5), "a resource's name is lower-case letters"),
        (lambda: messages.email("desk", deadline_s=0), "give it deadline_s"),
        (lambda: GivenSettings.model_validate({"email": {"r": RELAY | {"user": "etl"}}}), "a relay's login is a user and a password"),
        (lambda: GivenSettings.model_validate({"email": {"r": RELAY | {"to": ["not an address"]}}}), "String should match pattern"),
    ],
)
def test_a_message_or_its_settings_that_cannot_be_sent_are_refused_with_the_fix(make: Callable[[], object], says: str) -> None:
    with pytest.raises((ResourceError, ValueError), match=says):
        make()


@dataclass
class Ledger:
    """A database whose ledger keeps what each transaction committed, keyed by run and output as a real one is."""

    name: str = "warehouse"
    rows: list[LedgerEntry] = field(default_factory=list)

    @contextmanager
    def writing(self) -> Generator[Ledger.Writer]:
        writer = Ledger.Writer(self)
        yield writer

    @dataclass
    class Writer:
        ledger: Ledger
        pending: list[LedgerEntry] = field(default_factory=list)

        def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
            ends = [(row, row.interval) for row in self.ledger.rows if row.job == job and row.output == output]
            return sorted({row.run_id for row, found in ends if found is not None and found.start < interval.end and found.end > interval.start})

        def record(self, entry: LedgerEntry) -> None:
            if any((row.run_id, row.output) == (entry.run_id, entry.output) for row in self.ledger.rows):
                detail = "the ledger has the run's output"
                raise Conflict(detail, resource=self.ledger.name)
            self.pending.append(entry)

        def commit(self) -> None:
            self.ledger.rows += self.pending


MESSAGE = MessageRecord(destination="slack", to="desk", ledger="warehouse", text="3 accounts", sha256=messages.digest("3 accounts"))


def delivered(ledger: Ledger, *, run_id: str = "R1", again: bool = False, send: Callable[[], None] | None = None) -> tuple[str, list[str]]:
    posted: list[str] = []

    def post() -> None:
        if send is not None:
            send()
        posted.append(MESSAGE.text)

    delivery = sending.Delivery(run_id, "positions", DAY, "0.0.0", again, lambda: AT)
    record = sending.deliver(delivery, "alert", MESSAGE, post, ledger)  # ty: ignore[invalid-argument-type] -- the ledger a deliver writes, and nothing else of a database
    return record.status, posted


def test_a_message_is_recorded_in_its_ledger_as_it_is_sent_and_a_resume_never_sends_it_twice() -> None:
    ledger = Ledger()
    assert delivered(ledger) == ("sent", ["3 accounts"])
    [row] = ledger.rows
    assert (row.run_id, row.output, row.table, row.counts.inserted, json.loads(row.detail or "")) == ("R1", "alert", "slack:desk", 1, {"sha256": MESSAGE.sha256})
    assert delivered(ledger) == ("sent", [])  # the run's resume: the ledger has it


def test_a_run_for_an_interval_another_run_sent_the_message_for_sends_it_only_when_told_again() -> None:
    ledger = Ledger()
    delivered(ledger)
    assert delivered(ledger, run_id="R2") == ("skipped", [])
    assert delivered(ledger, run_id="R2", again=True) == ("sent", ["3 accounts"])
    assert [row.run_id for row in ledger.rows] == ["R1", "R2"]


def test_a_send_that_fails_records_nothing_so_the_next_attempt_sends_it() -> None:
    ledger = Ledger()

    def fail() -> None:
        detail = "desk's webhook answered 503"
        raise Unavailable(detail, resource="desk")

    with pytest.raises(Unavailable):
        delivered(ledger, send=fail)
    assert ledger.rows == []
    assert delivered(ledger) == ("sent", ["3 accounts"])


def test_the_kinds_each_fake_fails_with_are_the_resources() -> None:
    assert (Timeout, Unavailable, NotFound, Rejected, Denied, Failed) == fakes.SLACK_KINDS
    with pytest.raises(TypeError, match="a fake Slack channel fails with one of"):
        fakes.Slack().fails(Conflict)
