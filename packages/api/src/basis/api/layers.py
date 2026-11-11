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

A module of a `tests` package, a test module, and the repo's jobs, under `jobs/`, are no layer, whatever their names.

Five rules hold them, each failure one `ERROR` line with its fix:

- **Imports go down the table only**, directly or through modules of the repo's that are no layer, such as a
  helper: a domain module never imports a handlers module, nor a declaration a repository.
- **No layer reaches the outside but through what `api.given` gives it**: none imports, directly or through modules
  of the repo's that are no layer, a database driver, an HTTP client or storage, as basis-core's purity check lists
  them (`basis.core.testing.FORBIDDEN`), nor the standard library's sockets, processes, databases or HTTP (`OUTSIDE`).
- **A domain module stays pure**, as a compute module is, `domain.py` or any module of a `domain` package: it imports,
  directly or through modules of the repo's that are no layer, nothing of basis, no web framework, server or settings
  (`fastapi`, `starlette`, `uvicorn`, `pydantic_settings`), and nothing else the rule above refuses (`PURE`), so the
  same domain code runs in a job, which has no HTTP.
- **No layer reads the environment, the clock or new ids itself**: `os.environ` and `os.getenv`, `datetime.now`,
  `date.today` and `time.time`, and `uuid.uuid4` and its kin, which settings, `api.clock` and `api.ids` give a handler.
- **A handler passes a resource on, and never calls its methods**: in a handlers module, a function's parameter that
  `api.given` gives may be passed as an argument, read as an attribute, or called itself, as a compute function is, but
  never have a method called on it, `warehouse.fetch_one(...)`, so the SQL stays in repositories. The check reads the
  code as it is written, a parameter by its own name, its annotation as written or through a type alias the module
  defines or imports from the repo's own modules.

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
from basis.core.testing import FORBIDDEN

RANKS: Mapping[str, int] = {"service": 4, "handlers": 3, "repository": 2, "resources": 1, "errors": 1, "domain": 0}
"""Each layer, by its module's name, and its height: a module imports only those of its height or below."""
JOBS = "jobs"
"""The package of a repo's jobs, basis-job's, whose modules are no API layer whatever their names."""
OUTSIDE = (
    *dict.fromkeys(name.partition(".")[0] for name in FORBIDDEN if not name.startswith("basis.") and name != "pyarrow"),
    "socket", "ssl", "subprocess", "sqlite3", "smtplib", "ftplib", "socketserver",
)  # fmt: skip
"""What no layer reaches through its imports, each by its top-level name, as an import graph records an installed
package: a database driver, an HTTP client or storage, as basis-core lists them for every part's pure code, and the
standard library's sockets, processes and databases. pyarrow, which a repository's `fetch_table` answers, is the
domain's alone to refuse."""
OUTSIDE_MODULES = ("urllib.request", "http.client", "http.server", "xmlrpc.client", "asyncio.subprocess")
"""The standard library's HTTP and processes, by the module a layer imports, since the graph records only `urllib`,
`http` or `asyncio`, whose other modules are pure."""
PURE = ("basis", "fastapi", "starlette", "uvicorn", "pydantic_settings", "pyarrow", *OUTSIDE)
"""What a domain module may not reach through its imports: all of basis, which is plumbing; a web framework, server or
settings; `pyarrow`, for its file systems; and what no layer reaches."""
READS: Mapping[str, str] = {
    "os.environ": "the environment", "os.getenv": "the environment", "os.environb": "the environment",
    "datetime.now": "the clock", "datetime.utcnow": "the clock", "datetime.today": "the clock", "date.today": "the clock",
    "time.time": "the clock", "time.time_ns": "the clock",
    "uuid.uuid1": "a new id", "uuid.uuid4": "a new id", "uuid.uuid6": "a new id", "uuid.uuid7": "a new id",
}  # fmt: skip
"""What no layer reads itself, by its dotted name as the code writes it or imports it."""
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
    `domain.py` is, so each stays pure; None for a module that is no layer, a test's and a job's among them."""
    parts = module.split(".")
    if parts[0] == JOBS or "tests" in parts or parts[-1].startswith("test_") or parts[-1] == "conftest":
        return None
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
            elif imported not in found.internal and imported.partition(".")[0] in OUTSIDE:
                problems.append(_outside(found, chain, own))
        tree = _tree(repo, found.path(module))
        if tree is not None:
            problems += _stdlib(found.path(module), module, own, tree)
            problems += _reads(found.path(module), own, tree)
            if own == "handlers":
                problems += _handlers(repo, found.path(module), tree, packages)
    return problems


def _outside(found: _Graph, chain: tuple[str, ...], own: str) -> Problem:
    importer, imported = chain[0], chain[-1]
    where = f"{found.path(importer)}:{found.line(importer, chain[1]) or 1}"
    through = f", through {' -> '.join(chain[1:-1])}," if len(chain) > 2 else ""  # noqa: PLR2004 -- a chain of more than its ends
    reason = f"{where} imports {imported}{through} into {importer}, and a {own} module reaches the outside only through what api.given gives it"
    fix = "declare the database or the service it reaches as a resource, with api.database or api.http, and ask for it in the handler's parameters, as Annotated[api.Client, api.given(PRICING)]"
    return reason, fix


def _tree(repo: Path, relative: str) -> ast.Module | None:
    try:
        return ast.parse((repo / relative).read_text(encoding="utf-8"), filename=relative)
    except OSError, UnicodeDecodeError, SyntaxError, ValueError:  # ruff and ty say what is wrong with it
        return None


def _stdlib(relative: str, module: str, own: str, tree: ast.Module) -> list[Problem]:
    """Each import of the standard library's HTTP or processes in a layer's module, by the module it names."""
    found: list[Problem] = []
    for node in (found for found in ast.walk(tree) if isinstance(found, ast.Import | ast.ImportFrom)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif node.module is not None and node.level == 0:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        for name in names:
            if name in OUTSIDE_MODULES:
                if own == "domain":
                    reason = f"{relative}:{node.lineno} imports {name} into {module}, and a domain module stays pure: no I/O, no web framework and nothing of basis"
                    fix = f"take what it needs from {name} as arguments, from a handler or a repository, which import it, so the same rules run in a job"
                else:
                    reason = f"{relative}:{node.lineno} imports {name} into {module}, and a {own} module reaches the outside only through what api.given gives it"
                    fix = "declare the service it reaches as a resource, with api.http, and ask for it in the handler's parameters, as Annotated[api.Client, api.given(PRICING)]"
                found.append((reason, fix))
    return found


def _reads(relative: str, own: str, tree: ast.Module) -> list[Problem]:
    """Each read of the environment, the clock or a new id in a layer's module, as `READS` names them, through the
    names the module imports them by."""
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.asname or alias.name: alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported |= {alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names}
    found: list[Problem] = []
    for owner, node in _within(tree):
        if not isinstance(node, ast.Attribute | ast.Name) or isinstance(getattr(node, "ctx", None), ast.Store):
            continue
        name = _dotted(node, imported)
        shown = name.removeprefix("datetime.") if name is not None and name.startswith(("datetime.datetime.", "datetime.date.")) else name
        if shown is None or (what := READS.get(shown)) is None:
            continue
        if what == "the environment":
            reason = f"{relative}:{node.lineno}: {owner} reads {shown}, and a {own} module reads its settings through api.given(Settings)"
            fix = "declare the value as a field of the service's Settings, which reads it from its BASIS_ variable, and ask for the settings in the handler's parameters"
        else:
            reason = f"{relative}:{node.lineno}: {owner} reads {shown}, and a {own} module reads no clock and makes no id"
            fix = "ask for api.clock or api.ids in the handler's parameters, and pass the time or the id it gives to what needs it, so a recorded request replays it"
        found.append((reason, fix))
    return found


def _within(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Each node of `tree`, with the top-level function or class it is in, or `the module`."""
    for statement in tree.body:
        owner = statement.name if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) else "the module"
        for node in ast.walk(statement):
            yield owner, node


def _dotted(node: ast.Attribute | ast.Name, imported: Mapping[str, str]) -> str | None:
    """`node`'s dotted name, as the module's imports resolve its first part, where it is the outermost attribute read:
    `datetime.now` for `datetime.now` after `from datetime import datetime`, never `datetime` alone."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.insert(0, current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id not in imported:
        return None
    return ".".join([imported[current.id], *parts])


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


def _handlers(repo: Path, relative: str, tree: ast.Module, packages: Sequence[str]) -> list[Problem]:
    aliases = _aliases(repo, tree, packages)
    found: list[Problem] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        given = _given(node, aliases)
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


def _aliases(repo: Path, tree: ast.Module, packages: Sequence[str]) -> dict[str, ast.expr]:
    """The type aliases a module can name, by the name it gives each: its own, `type X = ...` or `X: TypeAlias = ...`,
    and those it imports from the repo's own modules, read from their source."""
    found = _defined(tree)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0 and node.module.partition(".")[0] in packages:
            relative = Path(*node.module.split("."))
            source = repo / relative / "__init__.py" if (repo / relative).is_dir() else (repo / relative).with_suffix(".py")
            if (other := _tree(repo, source.relative_to(repo).as_posix())) is not None:
                theirs = _defined(other)
                found |= {alias.asname or alias.name: theirs[alias.name] for alias in node.names if alias.name in theirs}
    return found


def _defined(tree: ast.Module) -> dict[str, ast.expr]:
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.TypeAlias):
            found[node.name.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None and ast.unparse(node.annotation).endswith("TypeAlias"):
            found[node.target.id] = node.value
    return found


def _given(function: ast.FunctionDef | ast.AsyncFunctionDef, aliases: Mapping[str, ast.expr]) -> frozenset[str]:
    """The names of `function`'s parameters that `api.given` gives a resource, by their annotations, as written or
    through a type alias `aliases` holds, or by their defaults: a parameter given settings or a user is not one, and a
    given parameter with no annotation is held, as it may be one."""
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: dict[str, ast.expr] = dict(zip([argument.arg for argument in positional[len(positional) - len(arguments.defaults) :]], arguments.defaults, strict=True))
    defaults |= {argument.arg: default for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True) if default is not None}
    return frozenset(
        argument.arg
        for argument in [*positional, *arguments.kwonlyargs]
        if (argument.annotation is not None and _asks(_through(argument.annotation, aliases)))
        or (argument.arg in defaults and _gives(defaults[argument.arg]) and (argument.annotation is None or _resource(argument.annotation)))
    )


def _through(annotation: ast.expr, aliases: Mapping[str, ast.expr]) -> ast.expr:
    """`annotation`, or the type its alias names, where it is a name `aliases` holds."""
    seen: set[str] = set()
    while isinstance(annotation, ast.Name) and annotation.id in aliases and annotation.id not in seen:
        seen.add(annotation.id)
        annotation = aliases[annotation.id]
    return annotation


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
    with importable(repo, packages):
        graph = grimp.build_graph(*packages, include_external_packages=True, exclude_type_checking_imports=True, cache_dir=None)
    internal = frozenset(module for module in graph.modules if module.partition(".")[0] in packages)
    return _Graph(graph, repo, internal, frozenset(packages))


def packages(repo: Path) -> list[str]:
    """The repo's packages, each a folder at its root with an `__init__.py`."""
    return sorted(_packages(repo))


@contextmanager
def importable(repo: Path, packages: Sequence[str]) -> Generator[None]:
    """The repo's packages imported from its root, whatever the process imported before, as a check of the repo's code
    must, since the installed package may be another checkout's, as it is in the push hook's worktree: the root goes
    first on the path, a module of the same name imported from elsewhere is set aside, and what the block imported of
    them is forgotten after, when the set-aside modules come back."""
    held = {name: module for name, module in sys.modules.items() if name.partition(".")[0] in packages}
    for name in held:
        del sys.modules[name]
    sys.path.insert(0, str(repo))
    try:
        yield
    finally:
        sys.path.remove(str(repo))
        for name in [name for name in sys.modules if name.partition(".")[0] in packages]:
            del sys.modules[name]
        sys.modules.update(held)
