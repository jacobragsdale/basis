"""What every part's contract tests share: the check that code cannot reach I/O through its imports, and the check
that a repo's agent files match the installed version. Each part's own `testing` module builds its contract tests on
these."""

from collections.abc import Iterable
from pathlib import Path

import grimp

from basis.core import sync
from basis.core.sync import AgentFiles

FORBIDDEN = (
    "basis.core.databases", "basis.core.new", "basis.core.publish", "basis.core.runs", "basis.core.settings",
    "basis.core.snapshots", "basis.core.storage", "basis.core.sync", "basis.core.testing", "mssql_python", "oracledb",
    "pyodbc", "pymssql", "cx_Oracle", "sqlalchemy", "psycopg", "psycopg2", "snowflake", "httpx", "requests", "urllib3",
    "aiohttp", "pyarrow.fs", "boto3", "azure",
)  # fmt: skip
"""What a part's pure code may not reach through its imports: basis-core's I/O, a database driver, an HTTP client, or
storage. Each part adds its own run loop."""


class ContractError(AssertionError):
    """Code that breaks its part's contract; the message says how, and what to do."""


def import_chains(module: str, *, packages: Iterable[str], forbidden: Iterable[str], lazy: Iterable[tuple[str, str]] = ()) -> list[tuple[str, ...]]:
    """Every shortest chain of imports from `module` to what `forbidden` lists, in a graph of `module`'s package and
    `packages`, built on each check, so it is never stale. basis's packages are portions of one namespace, which a graph
    of installed packages collapses to `basis`, so each is named. `lazy` lists imports that load on first use, which
    the code checked never makes."""
    graph = grimp.build_graph(*dict.fromkeys([package_of(module), *packages]), include_external_packages=True, exclude_type_checking_imports=True)
    for importer, imported in lazy:
        if graph.direct_import_exists(importer=importer, imported=imported):
            graph.remove_import(importer=importer, imported=imported)
    return sorted(
        chain
        for name in forbidden
        if name in graph.modules or any(found.startswith(f"{name}.") for found in graph.modules)
        for chain in graph.find_shortest_chains(importer=module, imported=name, as_packages=True)
    )


def package_of(module: str) -> str:
    """The package a module's import graph is built from: its top-level package, or for a module in basis, its portion,
    such as `basis.job`, since the namespace is no package of its own."""
    top, _, rest = module.partition(".")
    return f"{top}.{rest.partition('.')[0]}" if top == "basis" and rest else top


def check_agent_files(repo: Path, files: AgentFiles) -> None:
    """One part's block of `AGENTS.md` and its skills, in the repo at `repo`, match the installed version."""
    __tracebackhide__ = True
    if stale := sync.stale(repo, files):
        msg = f"the agent files do not match the installed {files.part}: run `uv run {files.command} sync`, which rewrites {', '.join(map(str, stale))}"
        raise ContractError(msg)
