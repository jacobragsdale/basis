"""`basis upgrade`: take a basis release in one step. It relocks the basis packages with uv, within the versions the
repo's `pyproject.toml` allows, runs `basis sync` and `basis check --full` with what it locked, and prints what
changed: each basis package's version, and each file the upgrade changed. Repos pull new versions, and nothing pushes
them, so this is how a repo takes one; its `regress` says whether the repo still behaves the same.

It starts from a folder with no uncommitted change, so what it changed is a diff of its own, for a commit of its own.
`basis sync` and `basis check --full` run through `uv run`, so the newly locked basis does them, not the one that
started the upgrade. The check leaves the QA tier out, since the relock is not yet a commit, and QA would publish a
run of it that no record names, then publish again when it is pushed: the push hook runs QA once it is committed."""

import tomllib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import override

from basis.core import clone
from basis.core.check import Runner
from basis.core.sync import Problem
from basis.core.verbs import fail

type Changed = Callable[[Path], list[str] | None]
"""The paths the repo at a folder has changed since its commit, or None outside a clone."""


@dataclass(frozen=True)
class Locked:
    version: str
    source: str
    """Where uv takes it from, as the lockfile says: an index, a git commit, or a folder."""

    @override
    def __str__(self) -> str:
        return f"{self.version} ({self.source})"


def locked(lockfile: Path, names: Collection[str]) -> dict[str, Locked]:
    """Each of `names` that `lockfile` locks, by name."""
    packages: object = tomllib.loads(lockfile.read_text(encoding="utf-8")).get("package")
    found: dict[str, Locked] = {}
    for entry in packages if isinstance(packages, list) else []:
        if isinstance(entry, dict) and (name := entry.get("name")) in names and isinstance(name, str):
            source = entry.get("source")
            where = next((f"{kind} {value}" for kind, value in source.items()) if isinstance(source, dict) else iter(()), "unknown source")
            found[name] = Locked(str(entry.get("version", "unknown")), where)
    return found


def changed_files(repo: Path) -> list[str] | None:
    """The paths the repo's folder has changed, relative to it, or None outside a clone."""
    found = clone.of(repo)
    return found.changed() if found is not None else None


def upgrade(repo: Path, packages: Collection[str], *, run: Runner, changed: Changed = changed_files, write: Callable[[str], None]) -> int:
    """Relock the `packages` of basis the repo at `repo` locks, sync, check in full, and say what changed."""
    ready = _before(repo, packages, changed)
    if isinstance(ready, tuple):
        return fail(*ready)
    before = ready
    again = "run `uv run basis upgrade` again"
    if run(["uv", "lock", "--quiet", *(argument for name in before for argument in ("--upgrade-package", name))], repo) != 0:
        return fail("uv could not relock basis's packages, as it says above", f"fix what it says, and {again}")
    after = locked(repo / "uv.lock", packages)
    if moved := [f"{name} {before[name]} -> {after[name]}" for name in before if name in after and after[name] != before[name]]:
        write(f"basis upgrade: {'; '.join(moved)}")
    else:
        write(f"basis upgrade: the lock already holds the newest basis pyproject.toml allows: {', '.join(f'{name} {found}' for name, found in after.items())}")
    if run(_uv_run("sync"), repo) != 0:
        return fail("basis sync failed after relocking, as it says above", "fix what it says, and run `uv run basis sync`, then `uv run basis check --full`")
    checked = run(_uv_run("check", "--full", "--no-qa"), repo)
    files = changed(repo) or []
    write(f"basis upgrade: changed {', '.join(files)}" if files else "basis upgrade: changed no file")
    if files:
        write("basis upgrade: commit the upgrade and push it: the push hook runs the QA tier on it, and records the check for `uv run basis review`")
    if checked != 0:
        return fail("the repo fails basis check --full after the upgrade, as above", "fix what it says, or set the upgrade aside with `git stash --include-untracked`")
    return 0


def _before(repo: Path, packages: Collection[str], changed: Changed) -> dict[str, Locked] | Problem:
    """What the lock holds of `packages` before the upgrade, or why the upgrade cannot start, and the fix."""
    found = changed(repo)
    again = "run `uv run basis upgrade` again"
    if found is None:
        return f"{repo} is in no git clone, so what basis upgrade changes could not be told apart", "run it in a clone of the repo"
    if found:
        return f"the repo has changes no commit holds ({', '.join(found)}), and basis upgrade's changes are a diff of their own", f"commit or stash them, and {again}"
    if not (repo / "uv.lock").is_file():
        return "the repo has no uv.lock to relock", f"run `uv lock`, commit uv.lock, and {again}"
    if not (before := locked(repo / "uv.lock", packages)):
        return "uv.lock locks no basis package", "add the part's package to pyproject.toml's [project] dependencies, and run `uv lock`"
    return before


def _uv_run(*argv: str) -> Sequence[str]:
    return ["uv", "run", "--locked", "--quiet", "basis", *argv]
