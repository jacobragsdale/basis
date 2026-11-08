"""The gates `basis check` holds every repo's tests to, whatever its parts:

- **Coverage.** The unit tests cover at least the floor `[tool.basis] coverage` gives, a whole percentage of the
  code's lines and branches, which every generator writes as 90; a repo without it fails, with the fix. pytest-cov
  measures it, from the repo's own environment, as basis configures it, never the repo's `[tool.coverage]`: branch
  coverage of every module under the repo's folder, an untested one included, but tests, conftests and repository
  modules (any module named `repository`, at any depth), which the integration tier covers, and with no lines left
  out but `if TYPE_CHECKING:`, `raise NotImplementedError`, `@overload`, a script's `if __name__ == "__main__":`, and
  what a `# pragma: no cover` marks. The unit tests are what the repo's pytest runs by default, its integration tier
  left out by its `-m 'not integration'`.
- **No mocks.** A test module, one named `test_*.py`, `*_test.py` or `conftest.py`, or any module in a `tests`
  folder, imports neither `unittest.mock`, nor `mock`, nor pytest-mock, and asks for none of pytest-mock's fixtures:
  a fake is a test's only double. A file that cannot do without them says why on a line of its own, as
  `# basis: allow-mocks -- the vendor's SDK has no fake, and its client opens a socket as it is made`, which
  `ALLOW_MOCKS` finds; one that gives no reason is refused.
- **No sockets in unit tests.** basis's pytest plugin (`basis.core.pytest_plugin`) fails a unit test that opens one,
  and tells `basis check` which did.

Each failure is one `ERROR` line with its fix. A pytest run that fails only because unit tests opened sockets prints
their lines alone; any other failure adds pytest's own line. What basis has the pytest step write, the coverage data,
its report and the plugin's file, goes to a temporary folder, removed after, so none of it lands in the repo."""

import ast
import json
import math
import os
import re
import tempfile
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from basis.core.sync import Problem
from basis.core.verbs import ProjectError, basis_table

KEY = "coverage"
"""The key of `[tool.basis]` that gives the floor."""
GENERATED = 90
"""The floor every generator writes: Google calls 90% exemplary, and finds chasing 100% has diminishing returns."""
REPOSITORY = "repository"
"""The name of a module the floor leaves out, at any depth, since the integration tier covers it."""
ALLOW_MOCKS = re.compile(r"^\s*# basis: allow-mocks(?P<reason>.*)$", re.MULTILINE)
"""A test file's line that lets it mock, with its reason after ` -- `."""
_REASON = re.compile(r"^ -- \S")
MOCKS = frozenset({"unittest.mock", "mock", "pytest_mock"})
"""The modules that make mocks, each refused with any module under it."""
MOCKERS = frozenset({"mocker", "class_mocker", "module_mocker", "package_mocker", "session_mocker"})
"""pytest-mock's fixtures, which a test asks for by its parameter's name."""
_UNREAD = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".hypothesis", "mutants", "snapshots"})
"""Folders that hold no test of the repo's own."""
OMITTED = ("**/tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py", f"**/{REPOSITORY}.py", f"**/{REPOSITORY}/**", "**/.venv/**")
EXCLUDED = ("if TYPE_CHECKING:", "raise NotImplementedError", "@overload", "if __name__ == .__main__.:")
PLUGIN_OPTION = "--basis-gates"
"""What `basis check` gives pytest, with a file, for basis's plugin to write what it found in."""


def floor(repo: Path) -> int | Problem:
    """The floor the repo's `[tool.basis] coverage` gives, or the problem with it."""
    pyproject = repo / "pyproject.toml"
    try:
        given = (basis_table(pyproject) or {}).get(KEY) if pyproject.is_file() else None
    except ProjectError as error:
        return str(error), "then run basis check again"
    if given is None:
        return (
            f"{pyproject.name}'s [tool.basis] gives no {KEY} floor",
            f"add `{KEY} = {GENERATED}` to it, the percentage of the code's lines and branches its unit tests must cover, as every generator writes it",
        )
    if isinstance(given, bool) or not isinstance(given, int) or not 0 < given <= 100:  # noqa: PLR2004 -- a percentage
        return f"{pyproject.name}'s [tool.basis] {KEY} is {given!r}", f"give a whole percentage from 1 to 100, as `{KEY} = {GENERATED}`"
    return given


def mocks(repo: Path) -> list[Problem]:
    """Each test file of the repo that mocks without saying why, with the fix."""
    found: list[Problem] = []
    for path in _tests(repo):
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except OSError, UnicodeDecodeError, SyntaxError, ValueError:  # ruff and ty say what is wrong with it
            continue
        uses = _mocking(tree)
        if not uses:
            continue
        name = path.relative_to(repo).as_posix()
        allowed = [match.group("reason") for match in ALLOW_MOCKS.finditer(text)]
        if any(_REASON.match(reason) for reason in allowed):
            continue
        line, what = uses[0]
        if allowed:
            found.append((f"{name}:{line} uses {what}, and its `# basis: allow-mocks` line gives no reason", "say why after it, as `# basis: allow-mocks -- WHY`"))
        else:
            found.append(
                (
                    f"{name}:{line} uses {what}, and a test's only double is a fake",
                    "build what the test runs on a fake, from basis's fakes or the repo's own; where none can stand in, give the file a line `# basis: allow-mocks -- WHY`",
                )
            )
    return found


def _tests(repo: Path) -> Iterator[Path]:
    for folder, names, files in os.walk(repo):
        here = Path(folder)
        names[:] = sorted(name for name in names if name not in _UNREAD and not (here / name / "pyvenv.cfg").exists())
        inside = "tests" in here.relative_to(repo).parts
        for name in sorted(files):
            if name.endswith(".py") and (inside or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"):
                yield here / name


def _mocking(tree: ast.Module) -> list[tuple[int, str]]:
    """Each use of a mock in `tree`, by its line, in order."""
    uses: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                uses += [(node.lineno, alias.name) for alias in names if _mocks(alias.name)]
            case ast.ImportFrom(module=str(module), level=0) if _mocks(module):
                uses.append((node.lineno, module))
            case ast.ImportFrom(module=str(module), level=0, names=names):
                uses += [(node.lineno, f"{module}.{alias.name}") for alias in names if _mocks(f"{module}.{alias.name}")]
            case ast.Attribute(value=ast.Name(id="unittest"), attr="mock"):
                uses.append((node.lineno, "unittest.mock"))
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                uses += [(node.lineno, f"pytest-mock's {argument.arg} fixture") for argument in arguments if argument.arg in MOCKERS]
            case _:
                pass
    return sorted(uses)


def _mocks(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in MOCKS)


def installed() -> Problem | None:
    """Why the repo's environment cannot measure coverage, if it cannot: `basis check` runs in it, as pytest does."""
    if find_spec("pytest_cov") is None:
        return "pytest-cov is not installed, and basis check measures the unit tests' coverage with it", "add pytest-cov to [dependency-groups] dev, and run `uv lock`"
    return None


@dataclass(frozen=True)
class Found:
    """What the pytest step found, beyond whether it passed."""

    problems: list[Problem]
    """Each gate the tests failed, with its fix."""
    explained: bool
    """Whether those problems are why pytest failed, so its own line would say nothing more."""


@dataclass(frozen=True)
class Measured:
    """The pytest step's arguments for the gates, and what it found, read from the files it wrote in `folder`."""

    folder: Path
    floor: int | None
    """None where the floor or pytest-cov is missing, which is its own problem, so coverage is not measured."""

    @property
    def report(self) -> Path:
        return self.folder / "coverage.json"

    @property
    def gates(self) -> Path:
        return self.folder / "gates.json"

    def argv(self) -> tuple[str, ...]:
        plugin = (f"{PLUGIN_OPTION}={self.gates}",)
        if self.floor is None:
            return plugin
        config = self.folder / "coveragerc"
        return (*plugin, "--cov", f"--cov-config={config}", "--cov-report=term-missing:skip-covered", f"--cov-report=json:{self.report}")

    def found(self, exit_code: int) -> Found:
        problems: list[Problem] = []
        sockets, failed = self._sockets()
        for test, addresses in sockets.items():
            problems.append(
                (
                    f"{test} opened a socket ({', '.join(addresses)}), and a unit test reaches nothing outside its process",
                    "give it a fake of what it reaches, from basis's fakes or the repo's own, or, for a test that needs a real database or service, mark it @pytest.mark.integration",
                )
            )
        if self.floor is not None:
            problems += self._coverage(exit_code)
        explained = exit_code == 1 and bool(failed) and failed <= set(sockets)
        return Found(problems, explained)

    def _sockets(self) -> tuple[dict[str, list[str]], set[str]]:
        try:
            written: object = json.loads(self.gates.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}, set()
        if not isinstance(written, dict):
            return {}, set()
        sockets, failed = written.get("sockets"), written.get("failed")
        found = {str(test): [str(address) for address in addresses] for test, addresses in sockets.items() if isinstance(addresses, list)} if isinstance(sockets, dict) else {}
        return found, {str(test) for test in failed} if isinstance(failed, list) else set()

    def _coverage(self, exit_code: int) -> list[Problem]:
        try:
            report: object = json.loads(self.report.read_text(encoding="utf-8"))
        except OSError, ValueError:
            if exit_code != 0:
                return []  # pytest's own line says why it stopped
            return [("pytest wrote no coverage report, so basis cannot hold the unit tests to their floor", "check that pytest-cov loads, as `uv run pytest --cov` shows")]
        totals = report.get("totals") if isinstance(report, dict) else None
        covered = totals.get("percent_covered") if isinstance(totals, dict) else None
        if not isinstance(covered, int | float) or self.floor is None or covered >= self.floor:
            return []
        shown = math.floor(covered * 10) / 10  # never rounded up to the floor it is under
        return [
            (
                f"the unit tests cover {shown:.1f}% of the code's lines and branches, under the floor of {self.floor}% that [tool.basis] {KEY} gives",
                "test what the coverage report above lists as missing; a repository module counts for nothing here, since the integration tier covers it",
            )
        ]


@contextmanager
def measured(floor: int | None) -> Generator[Measured]:
    """A temporary folder for the pytest step's files, with the coverage settings basis measures by, removed after."""
    with tempfile.TemporaryDirectory(prefix="basis-check-") as folder:
        found = Measured(Path(folder), floor)
        if floor is not None:
            (found.folder / "coveragerc").write_text(_settings(found.folder), encoding="utf-8")
        yield found


def _settings(folder: Path) -> str:
    """coverage.py's settings, as basis measures by: its own, so a repo's `[tool.coverage]` cannot leave code out."""
    lines = ["[run]", "branch = True", "source = .", f"data_file = {folder / '.coverage'}", "omit =", *(f"    {pattern}" for pattern in OMITTED)]
    lines += ["", "[report]", "exclude_also =", *(f"    {pattern}" for pattern in EXCLUDED)]
    return "\n".join(lines) + "\n"
