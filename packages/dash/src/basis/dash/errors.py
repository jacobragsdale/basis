"""The one place a callback's error goes: `on_error`, which basis gives Dash for the whole app, so every callback's
exception nobody caught passes through it. It logs the error once, with its traceback, and
answers nothing, so each output keeps what it showed. Step 15.5 makes a service error a sentence a person can act on,
shown in the notifications (`basis.dash.ids.NOTIFICATIONS`) with the request's id, and a dataset inside its stale
window answer its last good value; it adds to this function, and no page writes a handler of its own."""

import logging

from basis.core.errors import BasisError

log = logging.getLogger(__name__)


class DeclarationError(BasisError, TypeError):
    """An app that does not declare what basis needs; the message says what to declare, and `basis` prints it as one
    line."""


def on_error(error: Exception) -> None:
    """Dash's `on_error` for every callback of the app: the error, logged once, and no update to any output."""
    log.error("a callback failed: %s: %s", type(error).__qualname__, error, exc_info=error)
