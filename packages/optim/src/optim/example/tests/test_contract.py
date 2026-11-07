"""The optim contract, as tests: the default params validate, every portfolio of the fixture is accepted, and the
model's code cannot reach I/O. `optim.testing.ContractTests` says more."""

from optim.testing import ContractTests

from optim.example import model


class TestContract(ContractTests):
    model = model
