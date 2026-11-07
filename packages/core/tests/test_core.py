import basis.core


def test_core_is_a_portion_of_the_basis_namespace() -> None:
    assert basis.core.__name__ == "basis.core"
