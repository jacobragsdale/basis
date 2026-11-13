"""`basis check`'s checks of a Dash app's repo, basis-dash's `Part.checks`: each problem one `ERROR` line, its reason
and its fix. The repo's code is imported from its own folder, never an installed copy, as basis-api's checks do.

**The layers**, held by the import graph, as an API's are. Each is a module basis knows by its name, at any depth, a
package's modules taking its name too (`pages/overview.py` is a page):

| Module | Holds | Imports, of the layers |
|---|---|---|
| `app` | The declaration, `bd.App` | any below it |
| `pages` | Each page's layout and its callbacks | `data`, `domain` |
| `data` | Rows, resources and datasets | `domain` |
| `domain` | Pure transforms: frames in, a frame out | none |

A test's module, `dev.py` and a job's (`jobs/`) are no layer. Imports go down the table alone, directly or through
the repo's modules that are no layer. A domain module, and the module of every view's function wherever it is, imports
no Dash, Plotly or web server, nothing of `basis.dash` (a loader among it), nothing that does I/O as basis-core lists
it (`basis.core.testing.FORBIDDEN`), and none of the standard library's sockets, processes or databases: so a job can
run its functions as they stand. basis-core's pure modules, its rows among them, it may import.

**The callbacks.** A callback is the app's only in the app's module or a page's, so one declared anywhere else is
refused, as basis would leave it out silently; and one that is not `async` and calls `bd.load` or `bd.reload` is
refused, since it would run on the event loop's thread.

**The app, driven on its fakes** through `bd.testing`'s client, with no browser and no socket:

- it builds and starts on the fakes it declares, which an app that lists resources must declare;
- every page builds, inside basis's chrome, and every input, output and state of every callback names a component the
  page has (the chrome's, or that page's, for a page's own callbacks) and a property that component's type has;
- every callback of the app's and its pages' modules that Dash calls when a page loads is called, in the order the
  browser would, each with the values the layout and the callbacks before it give, and must not fail, answer more than
  `sizes.BUDGET_BYTES`, hold the event loop longer than `LOOP_BUDGET_MS`, answer a figure Plotly refuses or rows that
  leave a grid's column empty, or put rows in a `dcc.Store`;
- no page holds `dash_table.DataTable`, which Dash deprecates, nor a URL of another origin anywhere the browser would
  fetch it: in the index's scripts and stylesheets, inside every stylesheet it links, in a layout or a callback's
  answer; a link's `href` is navigation, which fetches nothing, and an XML namespace is a name, not a fetch.

A callback with a pattern-matching id, a clientside one and one with `prevent_initial_call` is checked by its ids
alone, since no page load calls it with values the layout gives."""

import ast
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

import grimp
import grimp.exceptions
import orjson

from basis.core.sync import Problem
from basis.core.testing import FORBIDDEN, import_chains, importable, repo_packages
from basis.dash import declarations, ids, internals, loading, sizes
from basis.dash.app import App
from basis.dash.driving import Drive, Json, Node, PageError, driving, nodes, outputs, short

RANKS: Final[Mapping[str, int]] = {"app": 3, "pages": 2, "data": 1, "domain": 0}
"""Each layer, by its module's name, and its height: a module imports only those below it."""
ORDER: Final = "imports go down the layers alone: app, then pages, then data, then domain"
NO_LAYER: Final = frozenset({"dev", "conftest"})
JOBS: Final = "jobs"
OUTSIDE: Final = ("socket", "ssl", "subprocess", "sqlite3", "smtplib", "ftplib", "socketserver")
"""The standard library's sockets, processes and databases, by the top-level name an import graph records."""
IMPURE: Final = (
    *FORBIDDEN, "basis.dash", "dash", "dash_ag_grid", "dash_mantine_components", "plotly", "flask", "fastapi", "starlette", "uvicorn", "pydantic_settings", *OUTSIDE,
)  # fmt: skip
"""What a domain module, or a view's function's, may not reach through its imports."""
LOOP_BUDGET_MS: Final = 250.0
"""The longest a callback may hold the event loop, which every user's page waits on meanwhile: work a callback does
itself, outside an awaited load, its answer's JSON among it. Step 17.6 measured about 17 ms for 1,000 rows of 25
columns and 176 ms for 10,000, so the budget passes at about 15,000, past `sizes.BUDGET_BYTES`' 8,000."""
LOADS: Final = frozenset({"load", "reload"})
"""What a callback that is not `async` must not call of `bd`'s: each awaits a load."""
NAMESPACES: Final[Mapping[str, str]] = {"dash_html_components": "dash.html", "dash_core_components": "dash.dcc", "dash_table": "dash.dash_table"}
"""The module of each of Dash's own namespaces, as the layout names them; any other is its module's own name."""
XML_NAMESPACES: Final = ("http://www.w3.org/",)
"""Names an SVG or XML document gives itself, which a browser never fetches."""
_URL = re.compile(r"""(?i)(?:\bhttps?:)?//[a-z0-9-]+(?:\.[a-z0-9-]+)+""")
_ATTRIBUTE = re.compile(r"""(?i)\b(?:src|href)\s*=\s*["']([^"']+)["']""")
_STYLESHEET = re.compile(r"""(?i)<link\b[^>]*\brel=["']stylesheet["'][^>]*>""")
_CSS_URL = re.compile(r"""(?i)(?:url\(\s*["']?|@import\s+["'])((?:https?:)?//[^"')\s]+)""")
_WILDCARDS: Final = frozenset({"ALL", "MATCH", "ALLSMALLER"})


def problems(repo: Path) -> list[Problem]:
    """Each way the repo's app breaks what the module's docstring says, with the fix."""
    packages = repo_packages(repo)
    if not packages:
        return []
    found = layers(repo, packages)
    with importable(repo, packages):
        try:
            app = loading.load()
        except Exception as error:  # noqa: BLE001 -- the repo's own code, whatever it raises, which `basis dev` shows in full
            return [*found, ("the repo's Dash app does not load", _said(error))]
        found.extend(elsewhere(repo, packages, app))
        found.extend(impure_views(app, packages))
        held = blocking(app)
        found.extend(problem for _, problem in held)
        found.extend(driven(app, skip=frozenset(id(function) for function, _ in held)))
    return found


def layers(repo: Path, packages: Sequence[str]) -> list[Problem]:
    """Each import of the repo's that goes up the layers, or out of the domain's purity, with the fix."""
    with importable(repo, packages):
        try:
            graph = grimp.build_graph(*packages, include_external_packages=True, exclude_type_checking_imports=True, cache_dir=None)
        except grimp.exceptions.SourceSyntaxError:  # ruff and ty say where, and the layers are checked once it parses
            return []
        internal = frozenset(module for module in graph.modules if module.partition(".")[0] in packages)
        layered = sorted(module for module in internal if layer(module) is not None)
        found: list[Problem] = []
        for module in layered:
            own = layer(module)
            for imported, chain in _reached(graph, module, internal, stop=frozenset(layered)):
                theirs = layer(imported) if imported in internal else None
                if own is not None and theirs is not None and RANKS[theirs] > RANKS[own]:
                    found.append(
                        (
                            f"{_path(repo, module)}:{_line(graph, chain)} imports {imported}, so the {own} layer reaches the {theirs} layer above it{_through(chain)}",
                            f"{ORDER}: move what it needs down",
                        )
                    )
        found.extend(problem for module in layered if layer(module) == "domain" for problem in _impure(repo, module, packages, "a domain module"))
    return found


def layer(module: str) -> str | None:
    """The layer of the module `module`, by its own name or a package's it is in, or None for one that is no layer: a
    test's, `dev`'s and a job's among them."""
    parts = module.split(".")
    if parts[0] == JOBS or "tests" in parts or parts[-1].startswith("test_") or parts[-1] in NO_LAYER:
        return None
    named = [part for part in parts[1:] if part in RANKS]
    return named[-1] if named else None


def elsewhere(repo: Path, packages: Sequence[str], app: App) -> list[Problem]:
    """Each callback declared in a module of the repo's that is neither the app's nor a page's, which basis leaves out
    of the app."""
    owned = {app.module, *(page.module for page in app.pages)}
    found: list[Problem] = []
    for path in sorted(path for package in packages for path in (repo / package).rglob("*.py")):
        relative = path.relative_to(repo)
        module = ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
        if module in owned or "tests" in relative.parts or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError, UnicodeDecodeError:  # ruff and ty say where
            continue
        for line in _callbacks_declared(tree):
            pages = ", ".join(page.module for page in app.pages)
            fix = f"declare it in the module of the page it serves ({pages}), or in {app.module}, whose callbacks alone are the app's"
            found.append((f"{relative.as_posix()}:{line} declares a callback, which app {app.name} leaves out, since that module is neither the app's nor a page's", fix))
    return found


def impure_views(app: App, packages: Sequence[str]) -> list[Problem]:
    """Each view whose function's module reaches Dash, a basis loader or I/O through its imports."""
    found: list[Problem] = []
    for dataset in declarations.datasets(app.resources):
        if not isinstance(dataset, declarations.View):
            continue
        module = getattr(dataset.function, "__module__", None)
        if not isinstance(module, str) or module.partition(".")[0] not in packages or layer(module) == "domain":
            continue  # a domain module's own line says it once
        named = getattr(dataset.function, "__qualname__", repr(dataset.function))
        found.extend(_impure(None, module, packages, f"view {dataset.name}'s function, {named}, is in {module}, which"))
    return found


def blocking(app: App) -> list[tuple[Callable[..., object], Problem]]:
    """Each callback of the app's own modules that is not `async` and loads, so it would hold the event loop while its
    query runs, with its function."""
    owned = {app.module, *(page.module for page in app.pages)}
    found: list[tuple[Callable[..., object], Problem]] = []
    for function in internals.functions_of(owned):
        if inspect.iscoroutinefunction(function) or not (calls := _loads_called(function)):
            continue
        name = getattr(function, "__name__", "callback")
        where = f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', name)}"
        reason = f"callback {where} calls bd.{calls[0]} and is not async, so it would run on the event loop's thread"
        found.append((function, (reason, f"make it `async def {name}(...)` and `await bd.{calls[0]}(...)`")))
    return found


def driven(app: App, *, skip: frozenset[int] = frozenset()) -> list[Problem]:
    """Each way the app, built on its fakes and driven through `bd.testing`'s client, breaks what the module's
    docstring says. The callbacks whose functions' ids `skip` holds are not called, as `blocking` has said why."""
    if app.fakes is None and app.resources:
        return [
            (
                f"app {app.name} lists resources and declares no fakes, so basis check cannot run its callbacks",
                "declare bd.App(..., fakes='module:function'), a function that returns a fake of each resource, as fixtures.database(WAREHOUSE)",
            )
        ]
    with ExitStack() as stack:
        try:
            drive = stack.enter_context(driving(app))
        except Exception as error:  # noqa: BLE001 -- the repo's own code, its fakes or its startup, whatever it raises
            return [(f"app {app.name} does not build on the fakes it declares", _said(error))]
        return _Checked(drive, skip).problems()
    return []  # pragma: no cover -- ExitStack never swallows what the block raises


@dataclass
class _Checked:
    drive: Drive
    skip: frozenset[int]
    found: list[Problem] = field(default_factory=list[Problem])

    @property
    def app(self) -> App:
        return self.drive.app

    def problems(self) -> list[Problem]:
        self._index()
        pages: dict[str, list[Node]] = {}
        for page in self.app.pages:
            try:
                pages[page.path] = self.drive.page(page.path)
            except PageError as error:
                self.found.append((f"app {self.app.name}'s page {page.path} does not build on the fakes: {error}", f"fix its layout, as `uv run basis render {page.path}` shows it"))
                continue
            self._layout(page.path, pages[page.path])
        for entry in self.drive.dependencies:
            module = self.drive.module(str(entry["output"]))
            on = [path for path in pages if module in {page.module for page in self.app.pages if page.path == path}] or list(pages)
            self._ids(entry, [node for path in on for node in pages[path]], on)
        for path, components in pages.items():
            values = self.drive.first(path, components)
            kinds = {str(props["id"]): node for node in components if isinstance((props := node.get("props") or {}).get("id"), str)}
            for entry in self.drive.calls(path, components, skip=self.skip):
                self._call(path, entry, values, kinds)
        return self.found

    def _index(self) -> None:
        index = self.drive.client.index()
        for value in _ATTRIBUTE.findall(index):
            if _foreign(value):
                self.found.append(
                    (
                        f"app {self.app.name}'s index fetches {value}, of another origin",
                        "serve every script, stylesheet and font from the app: put the file in the repo's assets/, and never name a CDN",
                    )
                )
        for href in stylesheets(index):
            if _foreign(href):
                continue
            answered = self.drive.client.http.get(href)
            for url in _CSS_URL.findall(answered.text if answered.status_code == HTTPStatus.OK else ""):
                if _foreign(url):
                    self.found.append(
                        (
                            f"app {self.app.name}'s stylesheet {href.partition('?')[0]} fetches {url}, of another origin",
                            "vendor the file into the repo's assets/, and name it by a path of the app's own",
                        )
                    )

    def _layout(self, path: str, components: list[Node]) -> None:
        for node, prop, url in _urls(components[0] if components else None):
            self.found.append((f"page {path}'s {_named(node)} names {url} in its {prop}, of another origin, which the browser would fetch", _ORIGIN_FIX))
        for node in components:
            kind, props = f"{node.get('namespace')}.{node.get('type')}", node.get("props") or {}
            if node.get("namespace") == "dash_table":
                self.found.append((f"page {path} holds {_named(node)}, a dash_table.DataTable, which Dash deprecates", "show rows with bd.grid(ID, Row), or dash_ag_grid's AgGrid"))
            if kind == "dash_core_components.Store" and props.get("id") != ids.STATUS and _rows(props.get("data")):
                self.found.append((f"page {path}'s store {_named(node)} holds rows", _STORE_FIX))

    def _ids(self, entry: Mapping[str, Any], components: list[Node], on: list[str]) -> None:
        output = str(entry["output"])
        where = f"page {on[0]}" if len(on) == 1 else "any page"
        for role, given in [("output", outputs(output)), ("input", entry.get("inputs") or []), ("state", entry.get("state") or [])]:
            for one in given:
                component, prop = _parsed(str(one["id"])), str(one["property"])
                matched = [node for node in components if _matches((node.get("props") or {}).get("id"), component)]
                if not matched:
                    if not isinstance(component, dict):
                        fix = "give a component that id in the page's layout, or name one it has"
                        self.found.append((f"callback {short(output)}'s {role} {_key(component, prop)} names no component of {where}", fix))
                    continue
                for node in matched:
                    if (known := _properties(node)) is not None and not _has(known, prop):
                        listed = ", ".join(sorted(name for name in known[0] if name != "id")[:12])
                        fix = f"name one of its properties, as {listed}"
                        self.found.append((f"callback {short(output)}'s {role} {_key(component, prop)} names a property {node.get('type')} does not have", fix))

    def _call(self, path: str, entry: Mapping[str, Any], values: dict[str, Json], kinds: Mapping[str, Node]) -> None:
        called = self.drive.call(entry, values, again_past_ms=LOOP_BUDGET_MS)
        name, start = short(called.output), f"from page {path}'s first values"
        if called.failed:
            said = called.failures[0] if called.failures else f"the app answered {called.status}"
            self.found.append((f"callback {name} failed on the fakes, {start}: {said}", f"fix it, as `uv run basis render {path}` shows it"))
            return
        if called.held_ms > LOOP_BUDGET_MS:
            self.found.append((f"callback {name} held the event loop for {called.held_ms:.0f} ms on the fakes, {start}, past the {LOOP_BUDGET_MS:.0f} ms every user's page waits on", _LOOP_FIX))
        if len(called.content) > sizes.BUDGET_BYTES:
            megabytes = len(called.content) / 1_048_576
            self.found.append((f"callback {name} answered {megabytes:.1f} MB on the fakes, {start}, past the {sizes.BUDGET_BYTES / 1_048_576:.0f} MB budget", _SIZE_FIX))
        for component, props in called.response.items():
            for prop, value in props.items():
                self._answer(name, path, kinds.get(component), f"{component}.{prop}", value)

    def _answer(self, name: str, path: str, node: Mapping[str, Any] | None, key: str, value: Json) -> None:
        if _patch(value):
            return
        kind = f"{node.get('namespace')}.{node.get('type')}" if node is not None else None
        prop = key.rpartition(".")[2]
        for _, _, url in _urls(value, prop=prop if node is None or kind != "dash_html_components.Link" else None):
            self.found.append((f"callback {name} answers {key} naming {url}, of another origin, which the browser would fetch", _ORIGIN_FIX))
        if any(found.get("namespace") == "dash_table" for found in nodes(value)):
            self.found.append((f"callback {name} answers {key} holding a dash_table.DataTable, which Dash deprecates", "show rows with bd.grid(ID, Row), or dash_ag_grid's AgGrid"))
        if kind == "dash_core_components.Graph" and prop == "figure" and (why := _bad_figure(value)) is not None:
            self.found.append((f"callback {name} answers {key} with a figure Plotly refuses: {why}", "build it with bd.charts, or a plotly.graph_objects.Figure, which checks each property"))
        if kind == "dash_ag_grid.AgGrid" and prop == "rowData" and node is not None and (empty := _empty_columns(node, value)):
            self.found.append(
                (
                    f"callback {name} answers {key} with rows that hold no {', '.join(empty)}, a column of the grid on page {path}",
                    "answer bd.rows of a frame of the grid's row, or name the column by its field",
                )
            )
        if kind == "dash_core_components.Store" and prop == "data" and _rows(value):
            self.found.append((f"callback {name} answers {key}, a store, with rows", _STORE_FIX))


_PURE_FIX = (
    "keep its functions pure: frames and values in, a frame out, with no Dash, no bd.load and no I/O, so a job can run them as they stand; "
    "load in the page's callback, or declare the transform as a bd.view"
)
_ORIGIN_FIX = "serve the file from the app's assets/, by a path of its own"
_STORE_FIX = "keep rows out of stores: each callback that needs a dataset asks bd.load for it again, which the cache answers in under a millisecond"
_LOOP_FIX = "move the work into a view (bd.view), which runs in a worker thread, or narrow what it computes"
_SIZE_FIX = "send fewer rows or columns: filter in a view, or show a summary and let the person ask for the detail"


def stylesheets(index: str) -> list[str]:
    """The address of each stylesheet an index links, as it names it."""
    return [href for tag in _STYLESHEET.findall(index) if (href := next(iter(_ATTRIBUTE.findall(tag)), None)) is not None]


def _said(error: BaseException) -> str:
    text = " ".join(str(error).split())
    from basis.core.errors import BasisError  # noqa: PLC0415 -- basis's own failure, which already ends with its fix

    if isinstance(error, BasisError):
        return text
    return f"{type(error).__name__}: {text}: fix it, as `uv run basis dev` shows it in full"


def _impure(repo: Path | None, module: str, packages: Sequence[str], who: str) -> Iterator[Problem]:
    try:
        chains = import_chains(module, packages=[*packages, "basis.core", "basis.dash"], forbidden=IMPURE)
    except grimp.exceptions.SourceSyntaxError:
        return
    seen: set[str] = set()
    for chain in sorted(chains, key=len):
        reached = chain[-1]
        if chain[1] in seen:  # one line for each of the module's own imports, however much it reaches
            continue
        seen.add(chain[1])
        start = f"{_path(repo, module)}, a domain module," if repo is not None else who
        through = _through(chain)
        yield (f"{start} imports {reached}{through}", _PURE_FIX)


def _reached(graph: grimp.ImportGraph, module: str, internal: frozenset[str], *, stop: frozenset[str]) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Each module `module` imports, and the chain to it, through the repo's modules that are not in `stop`."""
    seen: set[str] = {module}
    frontier: list[tuple[str, ...]] = [(module,)]
    while frontier:
        chain = frontier.pop(0)
        for imported in sorted(graph.find_modules_directly_imported_by(chain[-1])):
            if imported in seen:
                continue
            seen.add(imported)
            found = (*chain, imported)
            yield imported, found
            if imported in internal and imported not in stop:
                frontier.append(found)


def _line(graph: grimp.ImportGraph, chain: tuple[str, ...]) -> int | str:
    details = graph.get_import_details(importer=chain[0], imported=chain[1])
    return min((int(detail["line_number"]) for detail in details), default="")


def _through(chain: tuple[str, ...]) -> str:
    return f", through {' -> '.join(chain[1:-1])}" if len(chain) > 2 else ""  # noqa: PLR2004 -- the module, and what it reached


def _path(repo: Path | None, module: str) -> str:
    relative = Path(*module.split("."))
    if repo is not None and (repo / relative).is_dir():
        return (relative / "__init__.py").as_posix()
    return relative.with_suffix(".py").as_posix()


def _callbacks_declared(tree: ast.Module) -> Iterator[int]:
    """The line of each callback a module declares: a function under `@callback(...)` or `@dash.callback(...)`, or a
    call of `clientside_callback`."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                called = decorator.func if isinstance(decorator, ast.Call) else decorator
                if _name(called) == "callback":
                    yield decorator.lineno
        elif isinstance(node, ast.Call) and _name(node.func) == "clientside_callback":
            yield node.lineno


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _loads_called(function: Callable[..., object]) -> list[str]:
    """What of `bd.load` and `bd.reload` the function calls, as its source names it, through `bd` or a name imported
    from it."""
    import basis.dash as bd  # noqa: PLC0415 -- what app code calls, to compare with

    targets = {name: getattr(bd, name) for name in LOADS}
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except OSError, TypeError, SyntaxError:
        return []
    found: list[str] = []
    space = getattr(function, "__globals__", {})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name) and called.attr in LOADS:
            owner = space.get(called.value.id)
            if getattr(owner, "__name__", None) == "basis.dash":
                found.append(called.attr)
        elif isinstance(called, ast.Name) and any(space.get(called.id) is target for target in targets.values()):
            found.append(next(name for name, target in targets.items() if space.get(called.id) is target))
    return found


def _named(node: Mapping[str, Any]) -> str:
    component = (node.get("props") or {}).get("id")
    return f"{node.get('type')} {component!r}" if component is not None else str(node.get("type"))


def _parsed(component: str) -> str | dict[str, Any]:
    if component.startswith("{"):
        with suppress(orjson.JSONDecodeError):
            found: Json = orjson.loads(component)
            if isinstance(found, dict):
                return {str(key): value for key, value in found.items()}
    return component


def _matches(given: Json, wanted: str | dict[str, Any]) -> bool:
    if isinstance(wanted, str):
        return isinstance(given, str) and given == wanted
    if not isinstance(given, dict) or set(given) != set(wanted):
        return False
    return all(value == given[key] or (isinstance(value, list) and value and value[0] in _WILDCARDS) for key, value in wanted.items())


def _key(component: str | dict[str, Any], prop: str) -> str:
    return f"{component}.{prop}" if isinstance(component, str) else f"{orjson.dumps(component).decode()}.{prop}"


def _properties(node: Mapping[str, Any]) -> tuple[frozenset[str], tuple[str, ...]] | None:
    """The properties a component's type has, and the prefixes of the wildcard ones, or None for a type basis cannot
    find."""
    namespace, kind = str(node.get("namespace")), str(node.get("type"))
    try:
        module = __import__(NAMESPACES.get(namespace, namespace), fromlist=[kind])
    except ImportError:
        return None
    made = getattr(module, kind, None)
    if not isinstance(made, type):
        return None
    try:
        names = [name for name in inspect.signature(made.__init__).parameters if name not in {"self", "args", "kwargs"}]
    except TypeError, ValueError:
        return None
    try:
        wildcards = tuple(str(prefix) for prefix in made()._valid_wildcard_attributes)  # noqa: SLF001 -- Dash's own list of a type's `data-*` and `aria-*` props
    except Exception:  # noqa: BLE001 -- a type that needs props to be made: React passes such props on to its element
        wildcards = ("data-", "aria-")
    return frozenset(names), wildcards


def _has(known: tuple[frozenset[str], tuple[str, ...]], prop: str) -> bool:
    names, wildcards = known
    return prop in names or (bool(wildcards) and prop.startswith(wildcards))


def _patch(value: object) -> bool:
    return isinstance(value, dict) and "__dash_patch_update" in value


def _foreign(url: str) -> bool:
    return bool(_URL.match(url.strip())) and not url.strip().startswith(XML_NAMESPACES)


def _urls(value: Json, *, node: Mapping[str, Any] | None = None, prop: str | None = None) -> Iterator[tuple[Mapping[str, Any], str, str]]:
    """Each URL of another origin in a value, at any depth, with the component and the property that hold it: but a
    link's `href`, which is navigation, and fetches nothing, as any component's but `html.Link`'s is."""
    if isinstance(value, str):
        for found in _URL.finditer(value):
            start = found.start()
            if start and (value[start - 1].isalnum() or value[start - 1] in "/:"):
                continue
            url = value[start:].split()[0].strip("'\"()")
            if _foreign(url):
                yield (node or {}, prop or "value", url)
    elif isinstance(value, dict):
        if isinstance(value.get("props"), dict) and "type" in value and "namespace" in value:
            link = value.get("namespace") == "dash_html_components" and value.get("type") == "Link"
            for key, inner in value["props"].items():
                if key != "href" or link:
                    yield from _urls(inner, node=value, prop=key)
        else:
            for inner in value.values():
                yield from _urls(inner, node=node, prop=prop)
    elif isinstance(value, list):
        for inner in value:
            yield from _urls(inner, node=node, prop=prop)


def _rows(value: Json) -> bool:
    """Whether a store's value is rows: records, a frame's columns, or a frame's JSON."""
    if isinstance(value, str):
        try:
            value = orjson.loads(value)
        except orjson.JSONDecodeError:
            return False
    if isinstance(value, list):
        return bool(value) and all(isinstance(row, dict) for row in value) and len({frozenset(row) for row in value}) == 1 and len(value[0]) > 1
    if isinstance(value, dict):
        if {"columns", "data"} <= set(value):
            return True
        lists = [inner for inner in value.values() if isinstance(inner, (list, dict))]
        return len(value) > 1 and len(lists) == len(value) and len({len(inner) for inner in lists}) == 1 and len(lists[0]) > 0
    return False


def _bad_figure(value: Json) -> str | None:
    import plotly.graph_objects as go  # noqa: PLC0415 -- Plotly loads when a figure is checked

    if not isinstance(value, dict):
        return f"it is a {type(value).__name__}, not a figure's dict"
    try:
        go.Figure(value)
    except ValueError as error:
        return " ".join(str(error).split())[:300]
    return None


def _empty_columns(node: Mapping[str, Any], value: Json) -> list[str]:
    """Each column of the grid, by its field, that no row of `value` holds."""
    if not isinstance(value, list) or not value or not all(isinstance(row, dict) for row in value):
        return []
    fields = [str(column["field"]) for column in (node.get("props") or {}).get("columnDefs") or [] if isinstance(column, dict) and "field" in column]
    held = {key for row in value for key in row}
    return [name for name in fields if name not in held]
