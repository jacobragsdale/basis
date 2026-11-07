import pytest

from basis.core.logs import describe
from basis.job import new


def test_an_error_in_basis_job_is_located_in_its_code() -> None:
    with pytest.raises(ValueError, match="a job's name") as raised:
        new.repo(new.Path("unused"), "Bad", index=None, framework_path=None, install=False)
    where = describe(raised.value)["where"]
    assert isinstance(where, str)
    assert where.startswith("basis/job/new.py:")
    assert where.endswith(" in _check_job_name")
