# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Checks that nothing outside packages/optim needs optim, whose design may change, or which may go, before basis
releases it (docs/design.md). basis-core, basis-job and basis-api, their release, and the contract a UI relies on must
not change when it does.

    uv run scripts/without_optim.py            # the name check, then every check of the workspace without optim
    uv run scripts/without_optim.py --names    # the name check alone, in a second

The name check lists each line outside packages/optim that names optim, but the files `UNCHECKED` lists and the lines
`ALLOWED` lists, by file. Then the working tree, as it stands, is copied to a temporary folder, and its dependencies are
resolved afresh twice, with optim and without it, against one moment of the index: the lock's `exclude-newer`, or the
time the check starts. Each package both resolutions hold must get the same version from each, and the one without
optim must hold no package the other does not, so that optim's dependencies change nothing the other packages are
tested and released with. A relock on top of the lock could not show it, since uv keeps a locked version that still
satisfies everything. Then packages/optim is removed from the copy, the lock relocked without it, and
`uv sync --locked --all-packages`, every pre-commit hook on every file, and the unit tests must pass. CI runs it on every
pull request (`.github/workflows/ci.yml`, `without-optim`).

What this cannot see is a mechanism in basis-core shaped by optim: one that only one part uses must be one a job could
use as it stands. That is the reviewer's to judge."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import shared

ROOT = Path(__file__).resolve().parent.parent
OPTIM = "packages/optim"
NAMED = r"(^|[^A-Za-z0-9])optim([^A-Za-z]|$)"
"""optim's name as a word, in any case, and as the start of `optim_`, `optim-` or `optim.`; never `optimize`."""
UNCHECKED = (
    OPTIM,
    "docs",  # the design and the roadmap, which say what optim is and when it releases
    "AGENTS.md",  # the root's, whose checks include optim's generated repo
    "README.md",
    "CLAUDE.md",
    "uv.lock",  # which lists every member
    ".github/workflows/optim.yml",  # optim's own CI
    "scripts/without_optim.py",
    "packages/job/docs/design.md",  # etl-framework's design page, whose history names portfolio-optim
)
ALLOWED: Mapping[str, tuple[str, ...]] = {
    "pyproject.toml": (
        "CI's without-optim",  # the header, which says what checks the root
        "-p no:optim",  # keeps optim's pytest plugin out of the other parts' tests
        '"packages/optim/tests"',  # pytest's pythonpath, which a missing path does not fail
        '"packages/optim/src/optim"',  # coverage's source, which a missing path only warns of
        "import no optim, which may change or go",  # the import contract's name
        'forbidden_modules = ["optim"]',  # which holds when optim is not installed
    ),
    ".pre-commit-config.yaml": ("lint-imports-optim", "lint-imports (optim)", "--config packages/optim/pyproject.toml", "^packages/optim/"),
    ".github/workflows/ci.yml": ("without-optim:", "scripts/without_optim.py"),  # the job that runs this check
}
"""The lines outside packages/optim that may name optim, by the file and a text the line holds: each still works, and
changes nothing, when packages/optim is gone."""

type Problem = tuple[str, str]
"""A reason and its fix, printed as one `ERROR` line."""


@dataclass(frozen=True)
class Tools:
    git: str
    uv: str


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="without_optim.py", description="Check that nothing outside packages/optim needs optim.")
    parser.add_argument("--names", action="store_true", help="run the name check alone")
    parser.add_argument("--keep", action="store_true", help="keep the copy without optim, and print where it is")
    args = parser.parse_args(argv)
    git, uv = shutil.which("git"), shutil.which("uv")
    if git is None or uv is None:
        sys.stderr.write("ERROR: git or uv is not on the PATH, and this check runs both: install them, as https://docs.astral.sh/uv/ says\n")
        return 1
    tools = Tools(git, uv)
    failures = names(tools)
    if not args.names:
        failures += without_optim(tools, keep=args.keep)
    sys.stdout.flush()  # what the checks printed, before the ERROR lines that end it
    for reason, fix in failures:
        sys.stderr.write(f"ERROR: {reason}: {fix}\n")
    return 1 if failures else 0


def names(tools: Tools) -> list[Problem]:
    """Each line outside packages/optim that names optim, but those allowed."""
    argv = (tools.git, "grep", "--untracked", "-n", "-I", "-i", "-E", NAMED, "--", ".", *(f":(exclude){path}" for path in UNCHECKED))
    found = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
    if found.returncode not in {0, 1}:  # 1: no line matched
        return [("git grep could not search the working tree", found.stderr.strip() or "run it from a clone of basis")]
    lines = [line for line in found.stdout.splitlines() if not allowed(line)]
    if not lines:
        return []
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    count = f"{len(lines)} line{'s' if len(lines) != 1 else ''}"
    return [
        (
            f"{count} outside {OPTIM} name optim, as listed above",
            "make what each needs generic, so it holds for any part, or move it into packages/optim; ALLOWED in scripts/without_optim.py lists the few that must name it",
        )
    ]


def allowed(line: str) -> bool:
    """Whether a line `git grep -n` printed is one `ALLOWED` lists for its file."""
    path, _, rest = line.partition(":")
    _, _, text = rest.partition(":")  # after the line's number
    return any(name in text for name in ALLOWED.get(path, ()))


def without_optim(tools: Tools, *, keep: bool) -> list[Problem]:
    """The resolutions with optim and without it, and the workspace's checks without it, in a copy of the working tree."""
    copy = Path(tempfile.mkdtemp(prefix="basis-without-optim-"))
    try:
        return _checked(tools, copy)
    finally:
        if keep:
            sys.stdout.write(f"kept the copy without optim: {copy}\n")
        else:
            shutil.rmtree(copy, onexc=_remove_read_only)


def _checked(tools: Tools, copy: Path) -> list[Problem]:
    environment = shared.environment()  # not the working tree's environment, its lock, another moment of the index, or a hook's GIT_DIR
    _copy_working_tree(tools, copy)
    lock = (copy / "uv.lock").read_bytes()
    moment = exclude_newer(lock) or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    with_optim = _resolved(tools, copy, environment, moment, title=f"resolve afresh with optim, against the index as of {moment}")
    shutil.rmtree(copy / OPTIM, onexc=_remove_read_only)
    without = _resolved(tools, copy, environment, moment, title="resolve afresh without optim, against the same index")
    if with_optim is None or without is None:
        fix = "fix what it says; without optim, make what needs it generic, or move it into packages/optim"
        return [("uv could not resolve the workspace afresh, with optim or without it, as it says above", fix)]
    failures: list[Problem] = []
    if moved := [f"{name} {with_optim.get(name, 'unresolved')} to {version}" for name, version in sorted(without.items()) if with_optim.get(name) != version]:
        failures.append((f"without optim, the workspace resolves {', '.join(moved)}", "optim's dependencies change what the other packages are tested with: loosen optim's bound, or theirs"))
    (copy / "uv.lock").write_bytes(lock)
    if step(copy, environment, "relock without optim, keeping every version the lock holds", (tools.uv, "lock")) != 0:
        return [*failures, ("uv could not lock the workspace without optim, as it says above", "make what needs optim generic, or move it into packages/optim")]
    if step(copy, environment, "install every member but optim", (tools.uv, "sync", "--locked", "--all-packages")) != 0:
        return [*failures, ("uv could not install the workspace without optim, as it says above", "make what needs optim generic, or move it into packages/optim")]
    for command in ((tools.git, "-c", "init.defaultBranch=main", "init", "--quiet"), (tools.git, "add", "--all")):  # pre-commit checks the files git tracks
        subprocess.run(command, cwd=copy, env=environment, check=True)
    if step(copy, environment, "every pre-commit hook on every file, without optim", (tools.uv, "run", "--locked", "pre-commit", "run", "--all-files")) != 0:
        failures.append(("a pre-commit hook fails without optim, as it says above", "make what needs optim generic, or move it into packages/optim"))
    if step(copy, environment, "the unit tests, without optim", (tools.uv, "run", "--locked", "pytest")) != 0:
        failures.append(("the unit tests fail without optim, as pytest says above", "make what needs optim generic, or move it into packages/optim"))
    return failures


def _copy_working_tree(tools: Tools, copy: Path) -> None:
    """Every file git tracks or would add, as it is now, packages/optim's among them."""
    listed = subprocess.run((tools.git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"), cwd=ROOT, capture_output=True, check=True).stdout
    for name in sorted({os.fsdecode(entry) for entry in listed.split(b"\0") if entry}):
        source = ROOT / name
        if not source.is_file():  # a deleted file is listed until it is staged
            continue
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def exclude_newer(lock: bytes) -> str | None:
    """The moment of the index the lock was resolved against, if it records one."""
    options = tomllib.loads(lock.decode("utf-8")).get("options", {})
    found = options.get("exclude-newer") if isinstance(options, dict) else None
    return found if isinstance(found, str) else None


def _resolved(tools: Tools, copy: Path, environment: Mapping[str, str], moment: str, *, title: str) -> dict[str, str] | None:
    """Each package a fresh resolution of the copy's workspace locks, by name, with its version; None if uv fails."""
    (copy / "uv.lock").unlink(missing_ok=True)
    if step(copy, environment, title, (tools.uv, "lock", "--exclude-newer", moment)) != 0:
        return None
    return locked(copy / "uv.lock")


def locked(lockfile: Path) -> dict[str, str]:
    """Each package `lockfile` locks, by name, with its version."""
    packages = tomllib.loads(lockfile.read_text(encoding="utf-8")).get("package", [])
    return {str(package["name"]): str(package.get("version", "")) for package in packages}


def step(copy: Path, environment: Mapping[str, str], title: str, argv: Sequence[str]) -> int:
    sys.stdout.write(f"\n== {title}\n")
    sys.stdout.flush()
    return subprocess.run(argv, cwd=copy, env=environment, check=False).returncode


def _remove_read_only(function: Callable[[str], object], path: str, _: BaseException) -> None:
    """Remove what Windows keeps read-only, such as git's objects."""
    Path(path).chmod(0o700)
    function(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
