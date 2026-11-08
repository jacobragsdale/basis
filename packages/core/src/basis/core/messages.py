"""Messages a part sends people: a Slack channel's message, through the channel's incoming webhook, as the firm already
posts to its channels, and an email, through an SMTP relay, such as the firm's. Each is declared by name, as every
resource is, and configured by the environment's settings, so dev and QA post to their own channel and mail their own
people:

| Declared with | Gives | Configured by |
|---|---|---|
| `slack(name, deadline_s=)` | a `Poster` | `BASIS_SLACK__NAME__WEBHOOK`, and `__CA_BUNDLE` and `__PROXY` where it needs them |
| `email(name, deadline_s=)` | a `Mailer` | `BASIS_EMAIL__NAME__HOST`, `__PORT`, `__SECURITY`, `__SENDER` and `__TO`, and `__USER` and `__PASSWORD` where the relay asks for a login |

Each send has a span of its own and a deadline, and a send that fails raises the kind of
`basis.core.errors.ServiceError` its failure is, real or fake (`basis.core.sending`, `basis.core.fakes`). A send is
never made again by basis: neither a webhook's post nor a relay's message is idempotent. The text of a number in a
message is `basis.core.display.text`'s, so it reads as it does in the grid.

This module opens nothing, so a part's declarations import it."""

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

from basis.core.resources import ResourceError, named, seconds

type Destination = Literal["slack", "email"]

MAX_SUBJECT = 200
"""The longest subject a mail takes, in characters, well inside what every relay and client shows."""


@dataclass(frozen=True)
class SlackResource:
    name: str
    """The name its settings are under, as `BASIS_SLACK__DESK__WEBHOOK` for `desk`."""
    deadline_s: float
    """Seconds a post may take, from the first byte sent to the last byte of the reply."""


@dataclass(frozen=True)
class EmailResource:
    name: str
    """The name its settings are under, as `BASIS_EMAIL__DESK__HOST` for `desk`."""
    deadline_s: float
    """Seconds a send may take, the connection, the login and the message together."""


def slack(name: str, *, deadline_s: float) -> SlackResource:
    """A Slack channel, through its incoming webhook, whose URL is the secret setting `BASIS_SLACK__NAME__WEBHOOK`."""
    return SlackResource(named(name, "slack"), seconds(name, deadline_s))


def email(name: str, *, deadline_s: float) -> EmailResource:
    """People to email, through a relay: the setting `BASIS_EMAIL__NAME__TO` says who, and the rest of the block how."""
    return EmailResource(named(name, "email"), seconds(name, deadline_s))


@dataclass(frozen=True)
class Mail:
    """An email: its subject, one line, and its text, as plain text."""

    subject: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip() or "\n" in self.subject or "\r" in self.subject or len(self.subject) > MAX_SUBJECT:
            msg = f"a mail's subject is one line of 1 to {MAX_SUBJECT} characters, not {self.subject!r}: give one, and put the rest in its text"
            raise ResourceError(msg)
        if not isinstance(self.text, str) or not self.text.strip():
            msg = "a mail's text is what it says, and this one is empty: give it some, or send no mail"
            raise ResourceError(msg)


def digest(text: str) -> str:
    """The SHA-256 of a message's text, as UTF-8, in hex: what the ledger keeps of a message it records."""
    return hashlib.sha256(text.encode()).hexdigest()


def checked_text(text: str) -> str:
    """`text`, which must be a Slack message's: some text, as Slack's `mrkdwn` reads it."""
    if not isinstance(text, str) or not text.strip():
        msg = f"a Slack message is some text, not {text!r}: give it some, or send no message"
        raise ResourceError(msg)
    return text


class Poster(Protocol):
    """A Slack channel. `post` sends the text, which Slack reads as `mrkdwn`, or raises the kind of `ServiceError`
    its failure is: `Timeout` past its deadline, `Unavailable` for a webhook that cannot be reached or answers 502,
    503 or 429, and the kind of any other status that is not a 2xx, such as `NotFound` for a channel that is gone."""

    def post(self, text: str) -> None: ...


class Mailer(Protocol):
    """People to email. `send` sends the mail to the addresses the settings give, or raises the kind of
    `ServiceError` its failure is: `Timeout` past its deadline, `Unavailable` for a relay that cannot be reached,
    `Denied` for a login it refuses, `Rejected` for a sender or a recipient it refuses, and `Failed` otherwise."""

    def send(self, mail: Mail) -> None: ...
