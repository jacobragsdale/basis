"""An API's layers, which `basis check` holds, each module known by its own name at any depth: imports go down the
layers only, a domain module stays pure, and a handler passes a resource on and never calls its methods. Each repo
here is a folder of its own, whose package is found at its root, as an API repo's is."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from basis.api import layers

HANDLER = """from typing import Annotated

from basis import api

from accruals import repository
from accruals.resources import CLOCK, SOLVE, WAREHOUSE, Settings


async def bond(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)], settings: Annotated[Settings, api.given(Settings)]) -> object:
    if settings.max_days > 0:
        return await repository.bond(warehouse, security)
    return None


async def solved(solve: Annotated[api.Compute[[int], int], api.given(SOLVE)], clock: Annotated[api.Clock, api.given(CLOCK)]) -> int:
    return await solve(repository.days(clock))
"""
CLEAN = {
    "accruals/__init__.py": "",
    "accruals/service.py": "from accruals import errors, handlers, resources\n",
    "accruals/handlers.py": HANDLER,
    "accruals/repository.py": "from accruals import domain\nfrom accruals.resources import WAREHOUSE\n",
    "accruals/resources.py": "from basis import api\n\nfrom accruals import domain\n\nWAREHOUSE = api.database('warehouse', deadline_s=5, max_concurrent=4)\n",
    "accruals/errors.py": "from basis import api\n\nfrom accruals import domain\n",
    "accruals/domain.py": "from datetime import date\n\nfrom pydantic import BaseModel\n\nfrom accruals import money\n",
    "accruals/money.py": "import math\n",
    "accruals/bonds/__init__.py": "",
    "accruals/bonds/domain.py": "from accruals import domain\n",
    "accruals/tests/__init__.py": "",
    "accruals/tests/test_domain.py": "from accruals import domain, handlers, service\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    """A repo with one package, `accruals`, in its layers; whatever of it a test imports is forgotten after."""
    yield tmp_path
    for name in [name for name in sys.modules if name.partition(".")[0] == "accruals"]:
        del sys.modules[name]


def write(repo: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return repo


def reasons(repo: Path) -> list[str]:
    return [reason for reason, _ in layers.problems(repo)]


def test_a_layered_repo_passes(repo: Path) -> None:
    assert layers.problems(write(repo, CLEAN)) == []


def test_a_repo_with_no_package_has_no_layers_to_hold(repo: Path) -> None:
    write(repo, {"main.py": "import fastapi\n", "notes/README.md": ""})
    assert layers.problems(repo) == []


def test_a_layer_is_known_by_its_own_name_at_any_depth() -> None:
    assert [layers.layer(module) for module in ("api.handlers", "api.bonds.handlers", "api.handlers.bonds", "api.domain", "api.compute", "api")] == [
        "handlers", "handlers", None, "domain", None, None,
    ]  # fmt: skip


def test_an_import_up_the_layers_fails_with_the_fix(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/domain.py": "from datetime import date\n\nfrom accruals import handlers\n"})
    reason = f"accruals/domain.py:3 imports accruals.handlers into accruals.domain: a domain module imports a handlers module, above it, and {layers.ORDER}"
    fix = "move what accruals.domain needs of accruals.handlers down into accruals.domain, or into a layer below it, or move the code that needs it up into a handlers module"
    assert layers.problems(repo) == [(reason, fix)]


def test_an_import_up_the_layers_through_a_helper_names_the_chain(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/money.py": "from accruals import repository\n"})
    [reason] = reasons(repo)
    assert reason.startswith("accruals/domain.py:5 imports accruals.repository, through accruals.money, into accruals.domain: a domain module imports a repository module, above it")


@pytest.mark.parametrize(
    ("changed", "importer", "imported"),
    [
        ({"accruals/resources.py": "from accruals import repository\n"}, "resources", "repository"),
        ({"accruals/errors.py": "from accruals import handlers\n"}, "errors", "handlers"),
        ({"accruals/repository.py": "from accruals import handlers\n"}, "repository", "handlers"),
        ({"accruals/handlers.py": "from accruals import service\n"}, "handlers", "service"),
        ({"accruals/bonds/domain.py": "from accruals import errors\n"}, "domain", "errors"),
    ],
)
def test_no_layer_imports_one_above_it(repo: Path, changed: dict[str, str], importer: str, imported: str) -> None:
    write(repo, CLEAN | changed)
    [reason] = reasons(repo)
    assert f": a {importer} module imports a {imported} module, above it" in reason


def test_a_package_named_as_a_layer_is_that_layer(repo: Path) -> None:
    files = {name: text for name, text in CLEAN.items() if name != "accruals/handlers.py"}
    write(repo, files | {"accruals/handlers/__init__.py": "from accruals import service\n"})
    [reason] = reasons(repo)
    assert reason.startswith("accruals/handlers/__init__.py:1 imports accruals.service into accruals.handlers")


def test_an_import_only_a_type_checker_reads_is_no_import(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/domain.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from accruals import handlers\n"})
    assert layers.problems(repo) == []


@pytest.mark.parametrize(
    ("domain", "imported"),
    [
        ("import fastapi\n", "fastapi"),
        ("from basis.core.clock import Clock\n", "basis"),
        ("import socket\n", "socket"),
        ("import httpx2\n", "httpx2"),
        ("from pydantic_settings import BaseSettings\n", "pydantic_settings"),
    ],
)
def test_a_domain_module_that_reaches_io_or_basis_fails_with_the_fix(repo: Path, domain: str, imported: str) -> None:
    write(repo, CLEAN | {"accruals/domain.py": domain})
    assert layers.problems(repo) == [
        (
            f"accruals/domain.py:1 imports {imported} into accruals.domain, and a domain module stays pure: no I/O, no web framework and nothing of basis",
            f"take what it needs from {imported} as arguments, from a handler or a repository, which import it, so the same rules run in a job",
        )
    ]


def test_a_domain_module_that_reaches_io_through_a_helper_names_the_chain(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/money.py": "import oracledb\n"})
    [reason] = reasons(repo)
    assert reason.startswith("accruals/domain.py:5 imports oracledb, through accruals.money, into accruals.domain, and a domain module stays pure")


def test_a_handler_that_calls_a_resources_method_fails_with_the_fix(repo: Path) -> None:
    called = HANDLER.replace("return await repository.bond(warehouse, security)", "return await warehouse.fetch_one(object, 'SELECT 1', security=security)")
    write(repo, CLEAN | {"accruals/handlers.py": called})
    assert layers.problems(repo) == [
        (
            "accruals/handlers.py:11: bond calls warehouse.fetch_one, a method of what api.given gives it, and a handler passes a resource on and never calls it",
            "call it in a function of repository.py that takes warehouse as an argument, and call that from bond",
        )
    ]


@pytest.mark.parametrize(
    ("body", "method"),
    [("return clock.now()", "now"), ("return [clock.now] and clock.zone.name.upper()", "zone.name.upper"), ("return (lambda: clock.now())()", "now")],
    ids=["a-method", "an-attributes-method", "in-a-lambda"],
)
def test_every_call_of_a_given_parameters_method_is_found(repo: Path, body: str, method: str) -> None:
    handler = "from typing import Annotated\n\nfrom basis import api\n\nfrom accruals.resources import CLOCK\n\n\ndef now(clock: Annotated[api.Clock, api.given(CLOCK)]) -> object:\n    " + body + "\n"
    write(repo, CLEAN | {"accruals/handlers.py": handler})
    assert [reason.partition(": ")[2] for reason in reasons(repo)] == [f"now calls clock.{method}, a method of what api.given gives it, and a handler passes a resource on and never calls it"]


def test_a_parameter_given_as_its_default_is_held_too(repo: Path) -> None:
    handler = "from basis import api\n\nfrom accruals.resources import CLOCK\n\n\ndef now(clock: api.Clock = api.given(CLOCK)) -> object:\n    return clock.now()\n"
    write(repo, CLEAN | {"accruals/handlers.py": handler})
    assert len(reasons(repo)) == 1


@pytest.mark.parametrize(
    "parameter",
    ["settings: Annotated[Settings, api.given(Settings)]", "user: Annotated[User | None, api.given(User)]", "user: User = api.given(User)"],
    ids=["settings", "a-user", "a-user-as-its-default"],
)
def test_a_handler_may_call_the_methods_of_its_settings_and_its_user(repo: Path, parameter: str) -> None:
    name = parameter.partition(":")[0]
    handler = (
        "from typing import Annotated\n\nfrom basis import api\n\n\nclass Settings(api.Settings):\n    pass\n\n\nclass User(api.User):\n    pass\n\n\n"
        f"def mine({parameter}) -> object:\n    return {name}.model_dump()\n"
    )
    write(repo, CLEAN | {"accruals/handlers.py": handler})
    assert layers.problems(repo) == []


def test_a_module_that_is_no_handler_may_call_a_resource(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/repository.py": "async def bond(warehouse, security):\n    return await warehouse.fetch_one(object, 'SELECT 1', security=security)\n"})
    assert layers.problems(repo) == []


def test_the_repos_own_package_is_read_whatever_this_process_imported_of_the_same_name(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    elsewhere = write(tmp_path_factory.mktemp("elsewhere"), CLEAN)
    sys.path.insert(0, str(elsewhere))
    try:
        __import__("accruals.domain")
    finally:
        sys.path.remove(str(elsewhere))
    write(repo, CLEAN | {"accruals/domain.py": "import fastapi\n"})
    assert len(layers.problems(repo)) == 1
    assert Path(sys.modules["accruals"].__file__ or "").parent.parent == elsewhere


def test_a_module_that_does_not_parse_is_left_to_ruff_and_ty(repo: Path) -> None:
    write(repo, CLEAN | {"accruals/domain.py": "def (:\n"})
    assert layers.problems(repo) == []
