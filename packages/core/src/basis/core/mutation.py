"""Mutation testing, which `basis check --full` runs, `basis review` reports, and which never fails the check:
mutmut makes small changes, mutants, to each domain function the change touches, one at a time, and runs the unit
tests on each; a mutant no test fails survives, and shows a behaviour no test pins down. Coverage alone cannot tell
an empty test from a real one, and a surviving mutant can.

A domain module is one named `domain`, at any depth, as `domain.py` or a package's `domain/__init__.py`: the pure types
and rules an API's handlers and a job's transforms share. Its functions are its top-level functions and its classes'
methods. The change is what the repo's folder holds against the commit it branched from: where the clone has
`origin/HEAD`, the remote's default branch, their merge base; otherwise the commit's parent; and with neither, the
first commit, every domain function. A function is touched when the change adds, changes or removes a line of it, or
when its file is new.

mutmut runs where it needs `fork`, in WSL and on Linux, and nowhere else: elsewhere the check says it skipped it, and
why. It runs from the repo's own environment, `mutmut` in its development dependencies, on a copy of the repo's folder
in a temporary folder, removed after, so the repo's folder holds nothing new: basis gives it its settings there, in
the copy's `pyproject.toml`, in place of any `[tool.mutmut]` of the repo's own. Its output goes nowhere; what it
found is the record's."""

import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from basis.core import clone
from basis.core.review import MutantRecord, MutationRecord
from basis.core.sync import UNREAD as _UNREAD

DOMAIN = "domain"
"""The name of a domain module, at any depth."""
PLATFORMS = ("linux",)
"""Where mutmut runs: it forks a process for each mutant."""
SHOWN = 20
"""The most surviving mutants the record shows the change of; it counts the rest."""
KILLED = frozenset({1, 3})
SURVIVED = 0
STATUSES = {
    None: "not checked",
    2: "interrupted",
    5: "no tests",
    33: "no tests",
    34: "skipped",
    36: "timeout",
    24: "timeout",
    -24: "timeout",
    152: "timeout",
    255: "timeout",
    37: "caught by type check",
}
"""What mutmut's exit codes mean, but a killed or a surviving mutant's; any other is suspicious."""
NO_MUTMUT = "add `mutmut>=3.8; sys_platform == 'linux'` to [dependency-groups] dev, and run `uv lock`"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@", re.MULTILINE)
_TABLE = re.compile(r"^\[tool\.mutmut\][^\n]*\n(?:(?!\[)[^\n]*\n?)*", re.MULTILINE)


@dataclass(frozen=True)
class Function:
    """One domain function: a module's own, or a method of one of its classes."""

    module: str
    """The module's dotted name, as the tests import it."""
    path: str
    """Its file, relative to the repo, `/`-separated."""
    name: str
    """Its name, or `Class.method`."""
    lines: tuple[int, int]
    """Its first line, its decorators' included, and its last."""

    @property
    def qualified(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def mutants(self) -> str:
        """The names mutmut gives its mutants, as a glob."""
        owner, _, method = self.name.rpartition(".")
        mangled = f"xǁ{owner}ǁ{method}" if owner else f"x_{method}"
        return f"{self.module}.{mangled}__mutmut_*"


def mutate(repo: Path) -> MutationRecord:
    """Run mutmut on the domain functions the change in the repo at `repo` touches, and say what it found."""
    if sys.platform not in PLATFORMS:
        return MutationRecord(skipped="mutmut runs in WSL or on Linux only, since it forks a process for each mutant: push from WSL to have it run")
    found = clone.of(repo)
    if found is None:
        return MutationRecord(skipped="the repo is in no clone, so no change is known")
    base = _base(found)
    functions = touched(repo, found, base)
    if not functions:
        return MutationRecord(skipped="the change touches no domain function", base=base)
    if find_spec("mutmut") is None:
        return MutationRecord(skipped=f"mutmut is not installed: {NO_MUTMUT}", base=base, functions=[function.qualified for function in functions])
    with tempfile.TemporaryDirectory(prefix="basis-mutation-") as scratch:
        copy = Path(scratch) / repo.name
        _copy(repo, copy, functions)
        return _run(copy, base, functions)


def _base(found: clone.Clone) -> str | None:
    """The commit the change is measured from, or None for the first commit, whose every domain function is new."""
    default = found.commit("refs/remotes/origin/HEAD")
    if default is not None and found.head() is not None and (merged := clone.output(found.git, found.root, "merge-base", "HEAD", default)) is not None:
        return merged
    return found.commit("HEAD^")


def domain_modules(repo: Path) -> Iterator[tuple[Path, str]]:
    """Each domain module of the repo, by its file and its dotted name, outside its tests."""
    for folder, names, files in os.walk(repo):
        here = Path(folder)
        names[:] = sorted(name for name in names if name not in _UNREAD and name != "tests" and not (here / name / "pyvenv.cfg").exists())
        parts = here.relative_to(repo).parts
        if f"{DOMAIN}.py" in files:
            yield here / f"{DOMAIN}.py", ".".join((*parts, DOMAIN))
        if parts and parts[-1] == DOMAIN and "__init__.py" in files:
            yield here / "__init__.py", ".".join(parts)


def functions(path: Path, module: str, relative: str) -> list[Function]:
    """The functions of the domain module at `path`, and its classes' methods."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except OSError, UnicodeDecodeError, SyntaxError, ValueError:  # ruff and ty say what is wrong with it
        return []
    found: list[Function] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(Function(module, relative, node.name, _lines(node)))
        elif isinstance(node, ast.ClassDef):
            found += [Function(module, relative, f"{node.name}.{method.name}", _lines(method)) for method in node.body if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)]
    return found


def _lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]), node.end_lineno or node.lineno


def touched(repo: Path, found: clone.Clone, base: str | None) -> list[Function]:
    """The domain functions the change touches, against `base`, or every one where there is none."""
    modules = [(path, module, path.relative_to(repo).as_posix()) for path, module in domain_modules(repo)]
    if not modules:
        return []
    every = [function for path, module, relative in modules for function in functions(path, module, relative)]
    if base is None:
        return every
    paths = [relative for _, _, relative in modules]
    new = clone.output(found.git, repo, "ls-files", "--others", "--exclude-standard", "--", *paths)
    added = set(new.splitlines()) if new is not None else set()
    changed: dict[str, list[tuple[int, int]]] = {}
    for relative in paths:
        if relative in added:
            continue
        diff = clone.output(found.git, repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", base, "--", relative, raw=True) or ""
        changed[relative] = [(int(hunk["start"]), int(hunk["start"]) + max(int(hunk["count"] or 1), 1) - 1) for hunk in _HUNK.finditer(diff)]
    return [function for function in every if function.path in added or any(first <= function.lines[1] and last >= function.lines[0] for first, last in changed.get(function.path, ()))]


def _copy(repo: Path, copy: Path, functions: Sequence[Function]) -> None:
    """Copy the repo's folder to `copy`, with basis's settings for mutmut in its `pyproject.toml`."""

    def ignored(folder: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _UNREAD or name in {".coverage", "uv.lock.tmp"} or (Path(folder) / name / "pyvenv.cfg").exists()}

    shutil.copytree(repo, copy, ignore=ignored, symlinks=True)
    files = sorted({function.path for function in functions})
    sources = sorted({path.split("/")[0] for path in files})
    also = sorted(entry.name for entry in copy.iterdir() if entry.name not in sources and entry.name != "pyproject.toml")
    settings = [
        "[tool.mutmut]",
        f"source_paths = {json.dumps(sources)}",
        f"only_mutate = {json.dumps(files)}",
        f"also_copy = {json.dumps(also)}",
        "use_git_change_detection = false",
        'pytest_add_cli_args = ["-p", "no:cacheprovider"]',
    ]
    pyproject = copy / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
    pyproject.write_text(_TABLE.sub("", text).rstrip("\n") + "\n\n" + "\n".join(settings) + "\n", encoding="utf-8")


def _run(copy: Path, base: str | None, functions: Sequence[Function]) -> MutationRecord:
    environment = {**{name: value for name, value in os.environ.items() if not name.startswith("GIT_")}, "PYTHONDONTWRITEBYTECODE": "1"}
    ran = subprocess.run(
        [sys.executable, "-m", "mutmut", "run", *(function.mutants for function in functions)], cwd=copy, env=environment, capture_output=True, text=True, errors="replace", check=False
    )
    codes: dict[str, int | None] = {}
    for path in sorted({function.path for function in functions}):
        meta = copy / "mutants" / f"{path}.meta"
        try:
            read: object = json.loads(meta.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        by_key = read.get("exit_code_by_key") if isinstance(read, dict) else None
        if isinstance(by_key, dict):
            codes |= {str(name): code if isinstance(code, int) else None for name, code in by_key.items()}
    owners = {name: function for name in codes for function in functions if fnmatch.fnmatchcase(name, function.mutants)}
    survived = sorted(name for name, code in codes.items() if name in owners and code == SURVIVED)
    shown = [MutantRecord(name=name, function=owners[name].qualified, change=_change(copy, environment, name)) for name in survived[:SHOWN]]
    unsettled: dict[str, int] = {}
    for name, code in codes.items():
        if name in owners and code != SURVIVED and code not in KILLED:
            status = STATUSES.get(code, "suspicious")
            unsettled[status] = unsettled.get(status, 0) + 1
    failure = None if ran.returncode == 0 else _tail(ran.stdout + ran.stderr)
    return MutationRecord(
        base=base,
        functions=[function.qualified for function in functions],
        mutants=len(owners),
        killed=sum(code in KILLED for name, code in codes.items() if name in owners),
        survived=len(survived),
        shown=shown,
        unsettled=unsettled,
        failure=failure,
    )


def _change(copy: Path, environment: dict[str, str], name: str) -> str:
    """What the mutant `name` changed, as its lines before and after, from `mutmut show`."""
    shown = subprocess.run([sys.executable, "-m", "mutmut", "show", name], cwd=copy, env=environment, capture_output=True, text=True, errors="replace", check=False)
    lines = shown.stdout.splitlines()
    before = [line[1:].strip() for line in lines if line.startswith("-") and not line.startswith("---")]
    after = [line[1:].strip() for line in lines if line.startswith("+") and not line.startswith("+++")]
    return f"{' / '.join(before) or '(nothing)'} -> {' / '.join(after) or '(nothing)'}"


def _tail(output: str) -> str:
    """The last lines mutmut printed, without its progress, which it redraws with carriage returns."""
    lines = [line.rpartition("\r")[2].strip() for line in output.splitlines()]
    return " / ".join([line for line in lines if line][-5:]) or "mutmut printed nothing"
