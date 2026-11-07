# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Checks that nothing outside packages/optim needs optim, whose design may change, or which may go, before basis
releases it (docs/design.md). basis-core, basis-job and basis-api, their release, and the contract a UI relies on must
not change when it does.

    uv run scripts/without_optim.py            # the name check, then every check of the workspace without optim
    uv run scripts/without_optim.py --names    # the name check alone, in a second

The name check lists each line outside packages/optim that names optim, but those `ALLOWED` lists, and the files
`UNCHECKED` lists. Then the working tree, as it stands, is copied to a temporary folder without packages/optim and
relocked, and each package still locked must keep its version, so that optim's dependencies move nothing the other
packages are tested and released with. In that copy, `uv sync --locked --all-packages`, every pre-commit hook on every
file, and the unit tests must pass. CI runs it on every pull request (`.github/workflows/ci.yml`, `without-optim`).

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
from pathlib import Path

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
        "-p no:optim",  # keeps optim's pytest plugin out of the other parts' tests
        '"packages/optim/tests"',  # pytest's pythonpath, which a missing path does not fail
        '"packages/optim/src/optim"',  # coverage's source, which a missing path only warns of
    ),
    ".pre-commit-config.yaml": ("lint-imports-optim", "lint-imports (optim)", "--config packages/optim/pyproject.toml", "^packages/optim/"),
}
"""The lines outside packages/optim that may name optim, by the file and a text the line holds: each still works, and
changes nothing, when packages/optim is gone."""
ALLOWED_ANYWHERE = ("without_optim", "without-optim")
"""This check's own name, wherever it is run or described."""
UNSET = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_FROZEN", "UV_LOCKED", "PYTHONPATH")
"""Variables that would point the copy at the working tree's environment or its lock, and not at its own."""


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


def names(tools: Tools) -> list[tuple[str, str]]:
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
    path, _, text = line.partition(":")
    _, _, text = text.partition(":")  # the line's number
    return any(name in text for name in ALLOWED_ANYWHERE) or any(name in text for name in ALLOWED.get(path, ()))


def without_optim(tools: Tools, *, keep: bool) -> list[tuple[str, str]]:
    """The workspace's checks, in a copy of the working tree without packages/optim."""
    copy = Path(tempfile.mkdtemp(prefix="basis-without-optim-"))
    try:
        return _checked(tools, copy)
    finally:
        if keep:
            sys.stdout.write(f"kept the copy without optim: {copy}\n")
        else:
            shutil.rmtree(copy, onexc=_remove_read_only)


def _checked(tools: Tools, copy: Path) -> list[tuple[str, str]]:
    environment = {name: value for name, value in os.environ.items() if name not in UNSET and not name.startswith("GIT_")}  # a hook's GIT_DIR would point git at this clone
    _copy_working_tree(tools, copy, environment)
    before = locked(copy / "uv.lock")
    if step(copy, environment, "relock without optim", (tools.uv, "lock")) != 0:
        return [("uv could not lock the workspace without optim, as it says above", "make what needs optim generic, or move it into packages/optim")]
    failures: list[tuple[str, str]] = []
    if moved := [f"{name} {before.get(name, 'unlocked')} to {version}" for name, version in sorted(locked(copy / "uv.lock").items()) if before.get(name) != version]:
        failures.append((f"without optim, the lock moves {', '.join(moved)}", "optim's dependencies change what the other packages are tested with: loosen optim's bound, or theirs"))
    if step(copy, environment, "install every member but optim", (tools.uv, "sync", "--locked", "--all-packages")) != 0:
        return [*failures, ("uv could not install the workspace without optim, as it says above", "make what needs optim generic, or move it into packages/optim")]
    if step(copy, environment, "every pre-commit hook on every file, without optim", (tools.git, "add", "--all")) != 0:
        return [*failures, ("git could not stage the copy without optim, as it says above", "fix what it says")]
    if step(copy, environment, "", (tools.uv, "run", "--locked", "pre-commit", "run", "--all-files")) != 0:
        failures.append(("a pre-commit hook fails without optim, as it says above", "make what needs optim generic, or move it into packages/optim"))
    if step(copy, environment, "the unit tests, without optim", (tools.uv, "run", "--locked", "pytest")) != 0:
        failures.append(("the unit tests fail without optim, as pytest says above", "make what needs optim generic, or move it into packages/optim"))
    return failures


def _copy_working_tree(tools: Tools, copy: Path, environment: Mapping[str, str]) -> None:
    """Every file git tracks or would add, as it is now, but packages/optim; then a repository of its own, which
    pre-commit needs, with each file staged."""
    listed = subprocess.run((tools.git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"), cwd=ROOT, capture_output=True, check=True).stdout
    for name in sorted({os.fsdecode(entry) for entry in listed.split(b"\0") if entry}):
        source = ROOT / name
        if name == OPTIM or name.startswith(f"{OPTIM}/") or not source.is_file():  # a deleted file is listed until it is staged
            continue
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    subprocess.run((tools.git, "-c", "init.defaultBranch=main", "init", "--quiet"), cwd=copy, check=True, env=environment)


def locked(lockfile: Path) -> dict[str, str]:
    """Each package `lockfile` locks, by name, with its version."""
    packages = tomllib.loads(lockfile.read_text(encoding="utf-8")).get("package", [])
    return {str(package["name"]): str(package.get("version", "")) for package in packages}


def step(copy: Path, environment: Mapping[str, str], title: str, argv: Sequence[str]) -> int:
    if title:
        sys.stdout.write(f"\n== {title}\n")
    sys.stdout.flush()
    return subprocess.run(argv, cwd=copy, env=environment, check=False).returncode


def _remove_read_only(function: Callable[[str], object], path: str, _: BaseException) -> None:
    """Remove what Windows keeps read-only, such as git's objects."""
    Path(path).chmod(0o700)
    function(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
