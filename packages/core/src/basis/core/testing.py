"""What every part's contract tests share: the check that code cannot reach I/O through its imports. Each part's own
`testing` module builds its contract tests on it. Whether a repo's agent files match the installed parts is
`basis check`'s to say."""

from collections.abc import Iterable

import grimp

FORBIDDEN = (
    "basis.core.check", "basis.core.clone", "basis.core.command", "basis.core.databases", "basis.core.doctor", "basis.core.fakes", "basis.core.full", "basis.core.hooks",
    "basis.core.hygiene", "basis.core.invalid", "basis.core.new", "basis.core.opened", "basis.core.publish", "basis.core.qa", "basis.core.review", "basis.core.runs",
    "basis.core.settings", "basis.core.snapshots", "basis.core.storage", "basis.core.sync", "basis.core.testing", "basis.core.traces", "basis.core.upgrade", "basis.core.verbs",
    "mssql_python", "oracledb",
    "pyodbc", "pymssql", "cx_Oracle", "sqlalchemy", "psycopg", "psycopg2", "snowflake", "httpx", "httpx2", "requests", "urllib3",
    "aiohttp", "pyarrow", "boto3", "azure",
)  # fmt: skip
"""What a part's pure code may not reach through its imports: basis-core's I/O, its resources as they are opened and
faked, and its exporter of spans, a database driver, an HTTP client, or storage. Each part adds its own run loop. An installed package is named whole, since an import graph records only an
external package's top-level name: `pyarrow`, whose `fs` reads and writes files, rather than `pyarrow.fs`, which no
graph holds. Pure code needs no pyarrow of its own: its part hands it pandas frames, whose columns' Arrow types
`basis.core.frames` gives it (see `TYPES`). The command is I/O too: `basis.core.check` runs tools, `basis.core.verbs`
writes to stderr, and the gates (`clone`, `full`, `qa`, `review`, `doctor`, `upgrade`) run git, read settings files and
write records."""
TYPES = (("basis.core.frames", "pyarrow"),)
"""The imports of pyarrow by basis's own pure code, which the check discounts: `basis.core.frames`, the one module that
gives a frame's columns their Arrow types and converts them, and reads and writes nothing. Code the check reads
reaches pyarrow through it alone; code that imports pyarrow itself is still caught."""


class ContractError(AssertionError):
    """Code that breaks its part's contract; the message says how, and what to do."""


def import_chains(module: str, *, packages: Iterable[str], forbidden: Iterable[str], lazy: Iterable[tuple[str, str]] = ()) -> list[tuple[str, ...]]:
    """Every shortest chain of imports from `module` to what `forbidden` lists, in a graph of `module`'s package and
    `packages`, built on each check, so it is never stale. basis's packages are portions of one namespace, which a graph
    of installed packages collapses to `basis`, so each is named. `lazy` lists imports that load on first use, which
    the code checked never makes, and the imports `TYPES` lists are discounted too. grimp's cache stays off, so no
    `.grimp_cache` folder appears in a repo."""
    graph = grimp.build_graph(*dict.fromkeys([package_of(module), *packages]), include_external_packages=True, exclude_type_checking_imports=True, cache_dir=None)
    for importer, imported in (*lazy, *TYPES):
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
