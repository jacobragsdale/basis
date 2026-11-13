"""The one place a callback's error goes: `on_error`, which basis gives Dash for the whole app, so every callback's
exception nobody caught passes through it, and no page writes a handler of its own. It:

- **logs the error once**, with its traceback where it is a defect, and the trace's and the request's ids, as every
  record inside a request carries them; a resource's failure is no defect, so its record holds its kind, resource and
  reason, and no traceback;
- **answers nothing**, so each output keeps what it showed;
- **tells the person** in the page's notifications (`basis.dash.ids.NOTIFICATIONS`), in one sentence they can act on,
  with the request's id to quote:

| Failure | Says |
|---|---|
| `Timeout` | `warehouse did not answer within 30 s.` This shows what it showed before: try again in a moment. |
| `Unavailable` | `warehouse could not be reached.` This shows what it showed before: try again in a moment, and if it lasts, tell the app's support team. |
| `Denied` | `warehouse refused this app.` Tell the app's support team: its login or a permission needs fixing. |
| Any other kind | `warehouse failed to answer.` Tell the app's support team. |
| Anything else | `Something went wrong.` Tell the app's support team. |

A notification is a dict of DMC's `Notification` props: `id` (`basis-error-` and the request's id), `title`, `message`
(the sentence, what to do, and `Request ID.`), `color` `red`, `autoClose` false and `withCloseButton` true. A dataset
inside its `stale_s` never gets here: its load answers its last good value, marked stale (`basis.dash.runtime`)."""

import logging
import uuid

import dash
from basis.core import logs
from basis.core.errors import BasisError, Denied, ServiceError, Timeout, Unavailable
from basis.dash import internals
from basis.dash.ids import NOTIFICATIONS

log = logging.getLogger(__name__)

AGAIN = "This shows what it showed before: try again in a moment."
SUPPORT = "Tell the app's support team."


class DeclarationError(BasisError, TypeError):
    """An app that does not declare what basis needs; the message says what to declare, and `basis` prints it as one
    line."""


def on_error(error: Exception) -> None:
    """Dash's `on_error` for every callback of the app: the error, logged once, and told to the person in the page's
    notifications, and no update to any output."""
    request = logs.bound().get("request_id")
    if isinstance(error, ServiceError):
        log.error("a callback failed: %s: %s", type(error).__name__, error.detail, extra={"kind": type(error).__name__, "resource": error.resource, "reason": error.reason, "status": error.status})
    else:
        log.error("a callback failed: %s: %s", type(error).__qualname__, error, exc_info=error)
    if internals.in_callback():
        dash.set_props(NOTIFICATIONS, {"sendNotifications": [notification(error, None if request is None else str(request))]})


def notification(error: Exception, request: str | None) -> dict[str, object]:
    """What the page's notifications show of `error`, in the request `request`."""
    title, then = sentence(error)
    quoted = f" Request {request}." if request is not None else ""
    return {"id": f"basis-error-{request if request is not None else uuid.uuid4()}", "title": title, "message": f"{then}{quoted}", "color": "red", "autoClose": False, "withCloseButton": True}


def sentence(error: Exception) -> tuple[str, str]:
    """What happened, in one sentence a person can act on, and what to do."""
    if not isinstance(error, ServiceError):
        return "Something went wrong.", SUPPORT
    name = error.resource
    if isinstance(error, Timeout):
        from basis.dash import loads  # noqa: PLC0415 -- loads declares datasets, whose refusals are this module's

        deadline = loads.running.deadline(name) if loads.running is not None else None
        within = f" within {deadline:g} s" if deadline is not None else " in time"
        return f"{name} did not answer{within}.", AGAIN
    if isinstance(error, Unavailable):
        return f"{name} could not be reached.", f"{AGAIN} If it lasts, {SUPPORT[0].lower()}{SUPPORT[1:]}"
    if isinstance(error, Denied):
        return f"{name} refused this app.", "Tell the app's support team: its login or a permission needs fixing."
    return f"{name} failed to answer.", SUPPORT
