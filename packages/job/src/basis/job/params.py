"""A job's parameters."""

from pydantic import ConfigDict

from basis.core.model import Model


class Params(Model):
    """Base for a job's parameters. Strict: an unknown key is refused and no value is coerced, so a misspelled key or
    `"0.05"` for a number fails before anything loads, rather than being ignored. Document each field with a docstring
    under it: `etl describe` prints them. Params never change what a run reads, so a replay with other params reads
    the same snapshot."""

    model_config = ConfigDict(strict=True)
