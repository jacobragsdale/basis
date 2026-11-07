"""A job's parameters, and the base model for everything else that crosses a boundary."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Base for settings and records read from files: lax, so an exact conversion passes and a lossy one fails."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False, use_attribute_docstrings=True)


class Params(Model):
    """Base for a job's parameters. Strict: an unknown key is refused and no value is coerced, so a misspelled key or
    `"0.05"` for a number fails before anything loads, rather than being ignored. Document each field with a docstring
    under it: `etl describe` prints them. Params never change what a run reads, so a replay with other params reads
    the same snapshot."""

    model_config = ConfigDict(strict=True)
