import importlib.util
from pathlib import Path

import pytest

from basis.core.interval import Interval
from basis.core.logs import describe


def test_an_error_in_basis_core_is_located_in_its_code() -> None:
    with pytest.raises(ValueError, match=r"an interval is START\.\.END") as raised:
        Interval.parse("2026-11-05")
    where = describe(raised.value)["where"]
    assert isinstance(where, str)
    assert where.startswith("basis/core/interval.py:")
    assert where.endswith(" in parse")


def test_a_basis_folder_that_is_no_basis_package_is_not_framework_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder named `basis` on the path, such as one in the working directory, joins the namespace, and is not basis."""
    (tmp_path / "basis").mkdir()
    (tmp_path / "basis" / "local.py").write_text("def fail() -> None:\n    raise RuntimeError('local')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    spec = importlib.util.spec_from_file_location("basis.local", tmp_path / "basis" / "local.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="local") as raised:
        module.fail()
    assert describe(raised.value)["where"] is None
