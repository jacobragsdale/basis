"""The failures basis raises on purpose. A `BasisError`'s message gives the reason and ends with the fix, and the
`basis` command prints it as one line, with no traceback; every other exception is a defect, whose traceback the
command logs. A kind that stands for a builtin error also subclasses it, such as `SettingsError(BasisError,
ValueError)`, so code that catches the builtin still catches it. The module is pure, so job code may raise one."""

from collections.abc import Generator
from contextlib import contextmanager


class BasisError(Exception):
    """A failure the caller can fix, raised on purpose; its message gives the reason and ends with the fix."""


@contextmanager
def deliberate(*kinds: type[Exception]) -> Generator[None]:
    """Within the block, an exception of `kinds` is a failure the caller can fix, raised again as a `BasisError` with
    the same message and notes: for a call into code that raises a builtin error on purpose, such as a lookup that
    finds no run. Keep the block to that call, so a defect of the same kind elsewhere is not taken for one."""
    try:
        yield
    except kinds as error:
        raised = BasisError(str(error))
        for note in getattr(error, "__notes__", []):
            raised.add_note(str(note))
        raise raised from error
