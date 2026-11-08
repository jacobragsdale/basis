"""An API's layers, which `basis check` holds in every API repo, with no configuration file to keep. Each layer is a
module that basis knows by its own name, at any depth, so `bonds/handlers.py` and a package's `bonds/handlers/
__init__.py` are both a handlers module:

| Module | Holds | Imports, of the layers |
|---|---|---|
| `service` | The controller: its routes | any below it |
| `handlers` | One function per route, and the only place resources arrive | repositories, the declarations, domains |
| `repository` | All the SQL and every call to another service, each function taking the resource it needs and returning domain types | the declarations, domains |
| `resources`, `errors` | The declarations: the resources and settings, and the errors, a domain exception's meaning among them | each other, domains |
| `domain` | Pure types and rules | domains alone |

Three rules hold them, each failure one `ERROR` line with its fix:

- **Imports go down the table only**, directly or through modules of the repo's that are no layer, such as a
  helper: a domain module never imports a handlers module, nor a declaration a repository.
- **A domain module stays pure**, as a compute module is, `domain.py` or any module of a `domain` package: it imports, directly or through modules of the repo's that
  are no layer, nothing of basis, no web framework, server or settings (`fastapi`, `starlette`, `uvicorn`,
  `pydantic_settings`), no database driver, HTTP client or storage, and none of the standard library's sockets,
  processes or databases (`PURE`), so the same domain code runs in a job, which has no HTTP.
- **A handler passes a resource on, and never calls its methods**: in a handlers module, a function's parameter that
  `api.given` gives may be passed as an argument, read as an attribute, or called itself, as a compute function is, but
  never have a method called on it, `warehouse.fetch_one(...)`, so the SQL stays in repositories. The check reads the
  code as it is written, a parameter by its own name.

grimp builds the graph of the repo's packages, each folder at its root with an `__init__.py`, with its cache off, so
no folder appears in the repo; an import under `TYPE_CHECKING` is no import."""

import ast
import os
import sys
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import grimp
import grimp.exceptions

from basis.core.sync import UNREAD as _UNREAD
from basis.core.sync import Problem

RANKS: Mapping[str, int] = {"service": 4, "handlers": 3, "repository": 2, "resources": 1, "errors": 1, "domain": 0}
"""Each layer, by its module's name, and its height: a module imports only those of its height or below."""
PURE = (
    "basis", "fastapi", "starlette", "uvicorn", "pydantic_settings",
    "mssql_python", "oracledb", "pyodbc", "pymssql", "cx_Oracle", "sqlalchemy", "psycopg", "psycopg2", "snowflake",
    "httpx", "httpx2", "requests", "urllib3", "aiohttp", "pyarrow", "boto3", "azure",
    "socket", "ssl", "subprocess", "sqlite3", "smtplib", "ftplib", "socketserver",
)  # fmt: skip
"""What a domain module may not reach through its imports, each by its top-level name, as an import graph records an
installed package: all of basis, which is plumbing; a web framework, server or settings; a database driver, an HTTP
client or storage, `pyarrow` among them for its file systems; and the standard library's sockets, processes and
databases."""
GIVEN = "given"
"""The name of what gives a handler its resources, `api.given`, as its parameters' annotations call it."""
ORDER = "imports go down the layers alone: service, handlers, repository, then the declarations, resources and errors, then domain"


@dataclass(frozen=True)
class _Graph:
    graph: grimp.ImportGraph
    root: Path
    internal: frozenset[str]
    packages: frozenset[str]

    def path(self, module: str) -> str:
        """The module's file, relative to the repo, as a problem names it."""
        relative = Path(*module.split("."))
        found = relative / "__init__.py" if (self.root / relative).is_dir() else relative.with_suffix(".py")
        return found.as_posix()

    def line(self, importer: str, imported: str) -> int | None:
        details = self.graph.get_import_details(importer=importer, imported=imported)
        return min((int(detail["line_number"]) for detail in details), default=None)

    def reached(self, module: str, *, stop: frozenset[str]) -> Iterator[tuple[str, tuple[str, ...]]]:
        """Each module `module` imports, and the chain to it, through the repo's modules that are not in `stop`: each
        reached once, by its shortest chain."""
        seen: set[str] = {module}
        frontier: list[tuple[str, ...]] = [(module,)]
        while frontier:
            chain = frontier.pop(0)
            for imported in sorted(self.graph.find_modules_directly_imported_by(chain[-1])):
                if imported in seen:
                    continue
                seen.add(imported)
                found = (*chain, imported)
                yield imported, found
                if imported in self.internal and imported not in stop:
                    frontier.append(found)


def layer(module: str) -> str | None:
    """The layer of the module `module`, by its own name; every module of a `domain` package is the domain too, as
    `domain.py` is, so each stays pure; None for a module that is no layer."""
    parts = module.split(".")
    if parts[-1] in RANKS:
        return parts[-1]
    return "domain" if "domain" in parts[1:-1] else None


def problems(repo: Path) -> list[Problem]:
    """Each way the repo's code breaks its layers, with the fix."""
    packages = sorted(_packages(repo))
    if not packages:
        return []
    try:
        found = _graph(repo, packages)
    except grimp.exceptions.SourceSyntaxError:  # ruff and ty say where, and the layers are checked once it parses
        return []
    layered = sorted(module for module in found.internal if layer(module) is not None)
    stop = frozenset(layered)
    problems: list[Problem] = []
    for module in layered:
        own = layer(module)
        if own is None:
            continue
        for imported, chain in found.reached(module, stop=stop):
            theirs = layer(imported) if imported in found.internal else None
            if theirs is not None and RANKS[theirs] > RANKS[own]:
                problems.append(_upward(found, chain, own, theirs))
            elif own == "domain" and imported not in found.internal and imported.partition(".")[0] in PURE:
                problems.append(_impure(found, chain))
        if own == "handlers":
            problems += _handlers(repo, found.path(module))
    return problems


def _upward(found: _Graph, chain: tuple[str, ...], own: str, theirs: str) -> Problem:
    importer, imported = chain[0], chain[-1]
    where = f"{found.path(importer)}:{found.line(importer, chain[1]) or 1}"
    through = f", through {' -> '.join(chain[1:-1])}," if len(chain) > 2 else ""  # noqa: PLR2004 -- a chain of more than its ends
    reason = f"{where} imports {imported}{through} into {importer}: a {own} module imports a {theirs} module, above it, and {ORDER}"
    fix = f"move what {importer} needs of {imported} down into {importer}, or into a layer below it, or move the code that needs it up into a {theirs} module"
    return reason, fix


def _impure(found: _Graph, chain: tuple[str, ...]) -> Problem:
    importer, imported = chain[0], chain[-1]
    where = f"{found.path(importer)}:{found.line(importer, chain[1]) or 1}"
    through = f", through {' -> '.join(chain[1:-1])}," if len(chain) > 2 else ""  # noqa: PLR2004 -- a chain of more than its ends
    reason = f"{where} imports {imported}{through} into {importer}, and a domain module stays pure: no I/O, no web framework and nothing of basis"
    fix = f"take what it needs from {imported} as arguments, from a handler or a repository, which import it, so the same rules run in a job"
    return reason, fix


def _handlers(repo: Path, relative: str) -> list[Problem]:
    try:
        tree = ast.parse((repo / relative).read_text(encoding="utf-8"), filename=relative)
    except OSError, UnicodeDecodeError, SyntaxError, ValueError:  # ruff and ty say what is wrong with it
        return []
    found: list[Problem] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        given = _given(node)
        for call in (child for statement in node.body for child in ast.walk(statement) if isinstance(child, ast.Call)):
            if (called := _method_of(call.func, given)) is None:
                continue
            parameter, method = called
            reason = f"{relative}:{call.lineno}: {node.name} calls {parameter}.{method}, a method of what api.given gives it, and a handler passes a resource on and never calls it"
            fix = f"call it in a function of repository.py that takes {parameter} as an argument, and call that from {node.name}"
            found.append((reason, fix))
    return found


RESOURCES = frozenset({"Reader", "Writer", "Client", "Cache", "Clock", "Ids", "Compute"})
"""What a handler gets for a resource, by name: the parameters the rule holds. A handler's settings and its user, which
`api.given` gives too, are its own to use."""


def _given(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """The names of `function`'s parameters that `api.given` gives a resource, by their annotations or defaults: a
    parameter given settings or a user is not one, and a given parameter with no annotation is held, as it may be one."""
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: dict[str, ast.expr] = dict(zip([argument.arg for argument in positional[len(positional) - len(arguments.defaults) :]], arguments.defaults, strict=True))
    defaults |= {argument.arg: default for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True) if default is not None}
    return frozenset(
        argument.arg
        for argument in [*positional, *arguments.kwonlyargs]
        if (argument.annotation is not None and _asks(argument.annotation))
        or (argument.arg in defaults and _gives(defaults[argument.arg]) and (argument.annotation is None or _resource(argument.annotation)))
    )


def _asks(annotation: ast.expr) -> bool:
    """Whether `annotation` is `Annotated[TYPE, api.given(...)]`, with TYPE what a handler gets for a resource."""
    match annotation:
        case ast.Subscript(value=ast.Name(id="Annotated") | ast.Attribute(attr="Annotated"), slice=ast.Tuple(elts=[given, *metadata])):
            return _resource(given) and any(_gives(entry) for entry in metadata)
        case _:
            return False


def _resource(annotation: ast.expr) -> bool:
    """Whether `annotation` names what a handler gets for a resource, as `api.Reader` or `api.Cache[Bond]`."""
    match annotation:
        case ast.Subscript(value=value):
            return _resource(value)
        case ast.Attribute(attr=name) | ast.Name(id=name):
            return name in RESOURCES
        case _:
            return False


def _gives(node: ast.expr) -> bool:
    match node:
        case ast.Call(func=ast.Name(id=name) | ast.Attribute(attr=name)):
            return name == GIVEN
        case _:
            return False


def _method_of(called: ast.expr, given: frozenset[str]) -> tuple[str, str] | None:
    """The given parameter and its method that a call calls, as `warehouse.fetch_one`, or None."""
    if not isinstance(called, ast.Attribute):
        return None
    root: ast.expr = called
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name) and root.id in given:
        return root.id, ast.unparse(called).removeprefix(f"{root.id}.")
    return None


def _packages(repo: Path) -> Iterator[str]:
    """The repo's packages: each folder at its root with an `__init__.py`, but those basis never reads."""
    for entry in sorted(os.scandir(repo), key=lambda found: found.name):
        if entry.is_dir() and entry.name not in _UNREAD and not entry.name.startswith(".") and entry.name.isidentifier() and (Path(entry.path) / "__init__.py").is_file():
            yield entry.name


def _graph(repo: Path, packages: Sequence[str]) -> _Graph:
    with _importable(repo, packages):
        graph = grimp.build_graph(*packages, include_external_packages=True, exclude_type_checking_imports=True, cache_dir=None)
    internal = frozenset(module for module in graph.modules if module.partition(".")[0] in packages)
    return _Graph(graph, repo, internal, frozenset(packages))


@contextmanager
def _importable(repo: Path, packages: Sequence[str]) -> Generator[None]:
    """The repo's packages found at its root, whatever the process imported before: grimp finds a package as Python
    would, so the root goes first on the path, and a module of the same name imported from elsewhere is set aside."""
    held = {name: module for name, module in sys.modules.items() if name.partition(".")[0] in packages}
    for name in held:
        del sys.modules[name]
    sys.path.insert(0, str(repo))
    try:
        yield
    finally:
        sys.path.remove(str(repo))
        sys.modules.update(held)
