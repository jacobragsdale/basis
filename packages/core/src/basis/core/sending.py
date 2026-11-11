"""The messages `basis.core.messages` declares, as a part opens them: a Slack channel's webhook, posted to with httpx2,
and an SMTP relay, through `smtplib`, each from the environment's settings alone, or a fake's (`basis.core.fakes`),
which stands in only for what the message reaches, Slack or the relay, so the span, the deadline and the kind of each
failure are the part's own, as in production. A send is made once, never again on failure.

`deliver` is how a part that publishes, such as a job, sends a message it planned: in a transaction on the ledger of
the database the message names, which records it first, as a publish is, keyed by the run and the output, with its
text's SHA-256, then sends it, then commits. A send that fails rolls the record back. A resume after a crash
therefore finds the record of a message that went, and sends it again only where the process died between the send
and the commit, a moment's window; and a run for an interval another run has sent the message for sends it only when
told `again`, as a plain insert publishes again only then."""

import json
import logging
import smtplib
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Literal, Protocol

import httpx2
from opentelemetry.trace import SpanKind, Tracer

from basis.core import deadlines, fakes
from basis.core.databases import Counts, Database, LedgerEntry
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Unavailable, answered
from basis.core.interval import Interval
from basis.core.messages import EmailResource, Mail, SlackResource, checked_text, digest
from basis.core.settings import EmailRelay, Settings, SettingsError, SlackChannel, variable
from basis.core.snapshots import MessageRecord, SendRecord

log = logging.getLogger(__name__)

_UNREACHABLE = (httpx2.NetworkError, httpx2.RemoteProtocolError, httpx2.ProxyError)
"""A transport's failures to reach the webhook: to connect, through the proxy too, or a connection reset or closed."""
_TEMPORARY = 400
_PERMANENT = 500
"""SMTP's reply codes: 4xx is a refusal that may pass, 5xx one that will not."""


def slack_client(channel: SlackChannel) -> httpx2.Client:
    """The client of a channel's webhook: Slack's certificate checked against the declared CA bundle or the operating
    system's trusted CAs, through the declared proxy or none, and nothing read from the environment."""
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=channel.ca_bundle) if channel.ca_bundle is not None else True
    return httpx2.Client(verify=verify, proxy=str(channel.proxy) if channel.proxy is not None else None, trust_env=False)


class SlackPoster:
    """A Slack channel, as `Poster` gives it: each post in a span of its own, within its deadline."""

    def __init__(self, resource: SlackResource, client: httpx2.Client, webhook: str, tracer: Tracer) -> None:
        self._resource = resource
        self._client = client
        self._webhook = webhook
        """A secret, which no log or span records."""
        self._tracer = tracer

    def post(self, text: str) -> None:
        name = self._resource.name
        text = checked_text(text)
        limit = deadlines.limit(name, self._resource.deadline_s)
        if limit.seconds <= 0:
            raise limit.exceeded()
        attributes = {"basis.resource": name, "basis.sha256": digest(text)}
        with self._tracer.start_as_current_span(f"{name} post", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                response = within(limit, lambda: self._client.post(self._webhook, json={"text": text}, timeout=limit.seconds), abort=self._client.close)
            except httpx2.TimeoutException as error:
                raise limit.exceeded() from error
            except _UNREACHABLE as error:
                msg = f"{name}'s webhook could not be reached: {type(error).__name__}"
                raise Unavailable(msg, resource=name) from error
            except httpx2.RequestError as error:
                msg = f"{name}'s webhook failed: {type(error).__name__}"
                raise Failed(msg, resource=name) from error
            span.set_attribute("http.response.status_code", response.status_code)
            if not response.is_success:
                status = response.status_code
                detail = f"{name}'s webhook answered {status}"
                raise answered(status)(detail, resource=name, status=status, reason=response.text[:200])


class Connection(Protocol):
    """An SMTP session, as `smtplib.SMTP` is one."""

    def login(self, user: str, password: str) -> object: ...

    def send_message(self, msg: EmailMessage) -> object: ...

    def quit(self) -> object: ...


type Connect = Callable[[EmailRelay, float], Connection]
"""Opens a session with a relay, within the seconds given: `smtplib`'s, or a fake relay's."""


def smtp(relay: EmailRelay, timeout_s: float) -> Connection:
    """A session with the relay the settings give, over TLS where they say, its certificate checked against the
    operating system's trusted CAs."""
    if relay.security == "tls":
        return smtplib.SMTP_SSL(relay.host, relay.port, timeout=timeout_s, context=ssl.create_default_context())
    session = smtplib.SMTP(relay.host, relay.port, timeout=timeout_s)
    if relay.security == "starttls":
        session.starttls(context=ssl.create_default_context())
    return session


class SmtpMailer:
    """People to email, as `Mailer` gives them: each send in a span of its own, its connection, login and message
    each within what is left of its deadline."""

    def __init__(self, resource: EmailResource, relay: EmailRelay, connect: Connect, tracer: Tracer) -> None:
        self._resource = resource
        self._relay = relay
        self._connect = connect
        self._tracer = tracer

    def send(self, mail: Mail) -> None:
        name, relay = self._resource.name, self._relay
        limit = deadlines.limit(name, self._resource.deadline_s)
        if limit.seconds <= 0:
            raise limit.exceeded()
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = relay.sender, ", ".join(relay.to), mail.subject
        message.set_content(mail.text)
        attributes = {"basis.resource": name, "basis.sha256": digest(mail.text), "server.address": relay.host, "server.port": relay.port}
        with self._tracer.start_as_current_span(f"{name} send", kind=SpanKind.CLIENT, attributes=attributes):
            opened: list[Connection] = []

            def deliver() -> dict[str, object]:
                session = self._connect(relay, limit.seconds)
                opened.append(session)
                try:
                    if relay.user is not None and relay.password is not None:
                        session.login(relay.user, relay.password.get_secret_value())
                    refused = session.send_message(message)
                finally:
                    _quit(session)
                return dict(refused) if isinstance(refused, dict) else {}

            try:
                refused = within(limit, deliver, abort=lambda: [_close(session) for session in opened])
            except TimeoutError as error:
                raise limit.exceeded() from error
            except smtplib.SMTPException as error:
                raise _failure(name, error) from error
            except OSError as error:  # a relay that refuses or drops the connection, or cannot be resolved
                msg = f"{name}'s relay {relay.host}:{relay.port} could not be reached: {type(error).__name__}"
                raise Unavailable(msg, resource=name, reason=str(error)) from error
            if refused:  # the relay took the message for some recipients and refused the rest, which it did not send to
                msg = f"{name}'s relay refused {len(refused)} of its {len(relay.to)} recipients, {', '.join(sorted(refused))}, and sent the message to the rest"
                raise Rejected(msg, resource=name, reason=str(refused))


def within[T](limit: deadlines.Limit, work: Callable[[], T], *, abort: Callable[[], object]) -> T:
    """`work`'s result, within what `limit` leaves in all, whatever its socket timeouts allow each read or write: run
    in a thread of its own, and on the deadline `abort`ed, as closing its connection does, and refused as exceeded. A
    send cut off so may have gone, as any send past its deadline may."""
    done: list[T] = []
    failed: list[BaseException] = []

    def run() -> None:
        try:
            done.append(work())
        except BaseException as error:  # noqa: BLE001 -- raised again in the caller's thread
            failed.append(error)

    worker = threading.Thread(target=run, name="basis-send", daemon=True)
    worker.start()
    worker.join(limit.seconds)
    if worker.is_alive():
        abort()
        raise limit.exceeded()
    if failed:
        raise failed[0]
    return done[0]


def _close(session: Connection) -> None:
    """Close a session's socket from another thread, so the read it waits in ends."""
    sock = getattr(session, "sock", None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            log.debug("the relay's socket did not close cleanly")


def _quit(session: Connection) -> None:
    try:
        session.quit()
    except smtplib.SMTPException, OSError:  # the message went, or failed, already; a goodbye that fails changes neither
        log.debug("the relay's session did not close cleanly")


def _failure(name: str, error: smtplib.SMTPException) -> ServiceError:
    """The kind of failure an SMTP error is."""
    reason = str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return Denied(f"{name}'s relay refused the login", resource=name, reason=reason)
    if isinstance(error, smtplib.SMTPSenderRefused | smtplib.SMTPRecipientsRefused):
        return Rejected(f"{name}'s relay refused the sender or a recipient", resource=name, reason=reason)
    if isinstance(error, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError):
        return Unavailable(f"{name}'s relay closed the connection", resource=name, reason=reason)
    if isinstance(error, smtplib.SMTPResponseException) and _TEMPORARY <= error.smtp_code < _PERMANENT:
        return Unavailable(f"{name}'s relay answered {error.smtp_code}, for now", resource=name, reason=reason)
    return Failed(f"{name}'s relay refused the message", resource=name, reason=reason)


FAKE_WEBHOOK = "https://hooks.slack.invalid/fake"
"""The webhook a fake channel is posted to, where the settings configure none: it reaches nothing."""
FAKE_RELAY = EmailRelay(host="relay.invalid", port=25, security="none", sender="basis@example.invalid", to=("someone@example.invalid",))
"""The relay and the people a fake email goes to, where the settings configure none: it reaches nothing."""


def poster(resource: SlackResource, settings: Settings, tracer: Tracer, *, fake: fakes.Slack | None = None) -> SlackPoster:
    """The channel `resource` names, as the settings configure it, or `fake`, a test's, which needs no settings."""
    channel = settings.slack.get(resource.name)
    if fake is not None:
        return SlackPoster(resource, fake.client(), channel.webhook.get_secret_value() if channel is not None else FAKE_WEBHOOK, tracer)
    if channel is None:
        msg = f"the Slack channel {resource.name} is not configured, and a message posts to it: set {variable(('slack', resource.name, 'webhook'))}, as .env.example shows"
        raise SettingsError(msg)
    return SlackPoster(resource, slack_client(channel), channel.webhook.get_secret_value(), tracer)


def mailer(resource: EmailResource, settings: Settings, tracer: Tracer, *, fake: fakes.Email | None = None) -> SmtpMailer:
    """The people `resource` names, and their relay, as the settings configure them, or `fake`, a test's relay, which
    needs no settings: the people are the settings', where they name some."""
    relay = settings.email.get(resource.name)
    if fake is not None:
        return SmtpMailer(resource, relay if relay is not None else FAKE_RELAY, fake.connect, tracer)
    if relay is None:
        names = ", ".join(variable(("email", resource.name, part)) for part in ("host", "port", "security", "sender", "to"))
        msg = f"the email {resource.name} is not configured, and a message goes to it: set {names}, as .env.example shows"
        raise SettingsError(msg)
    return SmtpMailer(resource, relay, smtp, tracer)


@dataclass(frozen=True)
class Delivery:
    """What a part's run says of the messages it delivers."""

    run_id: str
    job: str
    """The part's name for what it ran, such as the job's, as its ledger rows name it."""
    interval: Interval | None
    version: str
    again: bool
    clock: Callable[[], datetime]


type Outcome = Literal["sent", "skipped", "failed"]


def deliver(delivery: Delivery, output: str, message: MessageRecord, send: Callable[[], None], database: Database) -> SendRecord:
    """Send `message`, the run's output `output`, through `send`, recording it in `database`'s ledger, as the module's
    docstring says, and what became of it."""
    destination = f"{message.destination}:{message.to}"
    with database.writing() as writer:
        writer.lock(delivery.job, output)
        if delivery.interval is not None and not delivery.again and (runs := [run for run in writer.overlapping(delivery.job, output, delivery.interval) if run != delivery.run_id]):
            reason = f"runs {', '.join(runs)} sent {output} for an interval that overlaps {delivery.interval}: pass --again to send it again"
            log.warning("not sending %s: %s", output, reason, extra={"output": output})
            return SendRecord(status="skipped", at=delivery.clock(), reason=reason)
        entry = LedgerEntry(
            delivery.run_id, delivery.job, output, destination, delivery.interval, rerun_safe=False, counts=Counts(inserted=1),
            published_at=delivery.clock(), version=delivery.version, detail=json.dumps({"sha256": message.sha256}),
        )  # fmt: skip
        try:
            writer.record(entry)
        except Conflict:
            log.info("%s was sent by this run already", output, extra={"output": output})
            return SendRecord(status="sent", at=delivery.clock(), reason="sent by this run already")
        send()
        writer.commit()
    log.info("sent %s to %s", output, destination, extra={"output": output, "sha256": message.sha256})
    return SendRecord(status="sent", at=delivery.clock())
