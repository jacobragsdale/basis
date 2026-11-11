"""The base model for everything that crosses a boundary: settings, and records read from files."""

import json

from pydantic import BaseModel, ConfigDict

from basis.core.errors import BasisError


class Model(BaseModel):
    """Base for settings and records read from files: lax, so an exact conversion passes and a lossy one fails."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False, use_attribute_docstrings=True)


def newer(data: bytes, known: int, what: str) -> None:
    """Refuse `data`, a record in JSON, where a newer basis wrote it, in a format beyond `known`."""
    try:
        found = json.loads(data)
    except ValueError:
        return  # its own failure, which reading it reports
    given = found.get("format", 1) if isinstance(found, dict) else 1
    if isinstance(given, int) and not isinstance(given, bool) and given > known:
        msg = f"{what} is in format {given}, which a newer basis wrote"
        raise BasisError(msg, "upgrade basis, as `uv run basis upgrade` does")
