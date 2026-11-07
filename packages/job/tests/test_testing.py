from pathlib import Path

import pytest

from basis.core.testing import ContractError
from basis.job.job import Job
from basis.job.params import Params
from basis.job.testing import check_imports


@pytest.mark.parametrize("statement", ["import pyarrow.fs", "from pyarrow import fs", "from pyarrow.fs import LocalFileSystem"])
def test_a_job_that_imports_pyarrows_filesystem_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str) -> None:
    package = tmp_path / "leaky_jobs"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "job.py").write_text(f"{statement}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    job = Job("leaky", "leaky_jobs.job", Params, object, object, print, None, package)  # ty: ignore[invalid-argument-type] -- the import check reads the module alone
    with pytest.raises(ContractError, match=r"leaky_jobs\.job -> pyarrow"):
        check_imports(job)
