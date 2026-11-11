"""The contract of an email's relay, run against the fake relay and against an SMTP server on a local socket, as
`test_http_contract.py` is for another service: a message sent, a recipient refused while the rest are sent to, and a
relay slower than the send's deadline, which the send gives up on in all, whatever each socket read allows."""

import socketserver
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Literal, override

import pytest
from opentelemetry.trace import NoOpTracer

from basis.core import fakes, messages, sending
from basis.core.errors import Rejected, Timeout
from basis.core.messages import Mail
from basis.core.settings import EmailRelay

type Relay = Literal["fake", "socket"]
TO = ("pm@example.com", "risk@example.com")


@dataclass
class Server:
    """What the local SMTP server saw, and how it answers: the recipients it refuses, and how long it waits before
    accepting a message."""

    refused: set[str] = field(default_factory=set[str])
    delay_s: float = 0.0
    received: list[tuple[list[str], str]] = field(default_factory=list[tuple[list[str], str]])


def handler(server: Server) -> type[socketserver.StreamRequestHandler]:
    class Smtp(socketserver.StreamRequestHandler):
        def reply(self, line: str) -> None:
            self.wfile.write(f"{line}\r\n".encode())

        @override
        def handle(self) -> None:
            self.reply("220 localhost ESMTP")
            recipients: list[str] = []
            while line := self.rfile.readline().decode().rstrip("\r\n"):
                verb = line.split(" ", 1)[0].upper()
                if verb in {"EHLO", "HELO"}:
                    self.reply("250 localhost")
                elif verb == "MAIL":
                    recipients = []
                    self.reply("250 OK")
                elif verb == "RCPT":
                    address = line.split(":", 1)[1].strip().strip("<>")
                    if address in server.refused:
                        self.reply("550 5.1.1 no such user")
                    else:
                        recipients.append(address)
                        self.reply("250 OK")
                elif verb == "DATA":
                    self.reply("354 go ahead")
                    body: list[str] = []
                    while (data := self.rfile.readline().decode().rstrip("\r\n")) != ".":
                        body.append(data)
                    time.sleep(server.delay_s)
                    server.received.append((recipients, "\n".join(body)))
                    self.reply("250 OK")
                elif verb == "QUIT":
                    self.reply("221 bye")
                    return
                else:
                    self.reply("250 OK")

    return Smtp


@dataclass
class Contract:
    relay: Relay
    fake: fakes.Email = field(default_factory=fakes.Email)
    server: Server = field(default_factory=Server)
    port: int = 0

    def refuse(self, address: str) -> None:
        self.fake.refuses(address)
        self.server.refused.add(address)

    def slow(self, seconds: float) -> None:
        self.fake.delay_s = seconds
        self.server.delay_s = seconds

    def send(self, *, deadline_s: float = 5) -> None:
        relay = EmailRelay(host="127.0.0.1", port=self.port or 25, security="none", sender="basis@example.com", to=TO)
        connect = self.fake.connect if self.relay == "fake" else sending.smtp
        mailer = sending.SmtpMailer(messages.email("desk", deadline_s=deadline_s), relay, connect, NoOpTracer())
        mailer.send(Mail(subject="prices", text="3 prices"))

    def delivered(self) -> list[list[str]]:
        if self.relay == "fake":
            return [[address for address in TO if address not in self.fake.refused] for _ in self.fake.sent]
        return [recipients for recipients, _ in self.server.received]


@pytest.fixture(params=["fake", "socket"])
def contract(request: pytest.FixtureRequest) -> Generator[Contract]:
    found = Contract(request.param)
    if found.relay == "fake":
        yield found
        return
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler(found.server)) as server:
        server.daemon_threads = True
        found.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield found
        server.shutdown()


def test_a_message_goes_to_every_recipient(contract: Contract) -> None:
    contract.send()
    assert contract.delivered() == [list(TO)]


def test_a_recipient_the_relay_refuses_fails_the_send_though_the_rest_were_sent_to(contract: Contract) -> None:
    contract.refuse("risk@example.com")
    with pytest.raises(Rejected, match=r"desk's relay refused 1 of its 2 recipients, risk@example.com, and sent the message to the rest"):
        contract.send()
    assert contract.delivered() == [["pm@example.com"]]


def test_a_relay_slower_than_the_deadline_is_given_up_on_within_it(contract: Contract) -> None:
    contract.slow(3)
    start = time.monotonic()
    with pytest.raises(Timeout, match=r"desk did not answer within its deadline of 0\.5s"):
        contract.send(deadline_s=0.5)
    assert time.monotonic() - start < 2
