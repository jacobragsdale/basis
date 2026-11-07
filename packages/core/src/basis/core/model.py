"""The base model for everything that crosses a boundary: settings, and records read from files."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Base for settings and records read from files: lax, so an exact conversion passes and a lossy one fails."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False, use_attribute_docstrings=True)
