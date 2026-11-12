from pathlib import Path

import basis
import basis.core


def test_core_is_a_portion_of_the_basis_namespace() -> None:
    """`basis` is a namespace package, with no `__init__.py` of its own, so each installed part adds its portion:
    core's is one, beside whichever parts are installed, and none owns the package."""
    assert getattr(basis, "__file__", None) is None
    portions = [Path(portion).resolve() for portion in basis.__path__]
    assert Path(basis.core.__file__).resolve().parent.parent in portions
    assert all(not (portion / "__init__.py").exists() for portion in portions)
