import types

import pytest

from basis.core.calendars import calendar
from basis.job import job
from basis.job.example import job as example


def module_like_the_example(**attributes: object) -> types.ModuleType:
    """A job module that exports what the example does, with `attributes` in place of its own."""
    module = types.ModuleType("calendared")
    module.__file__ = example.__file__
    for name in ("Params", "Inputs", "Outputs", "transform", "check"):
        setattr(module, name, getattr(example, name))
    for name, value in attributes.items():
        setattr(module, name, value)
    return module


def test_a_job_s_calendar_is_its_module_s_calendar() -> None:
    assert job.read(example).calendar is calendar("sifma-us")
    assert job.read(module_like_the_example()).calendar is None


def test_a_calendar_given_by_name_alone_is_refused_with_the_fix() -> None:
    with pytest.raises(job.JobError, match=r"^calendared's CALENDAR is 'sifma-us', not a calendar: give one by name, as CALENDAR = etl.calendar\(\"sifma-us\"\)$"):
        job.read(module_like_the_example(CALENDAR="sifma-us"))
