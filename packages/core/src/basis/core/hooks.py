"""The git setting that makes a clone run a basis repo's commit and push hooks, which `basis sync` writes in `.githooks/`.
git runs a clone's hooks from `.git/hooks` unless `core.hooksPath` names another folder, and a clone never carries
its settings, so the verbs a developer runs on a clone, `basis check`, `sync` and `new`, call `ensure`: a fresh clone
runs its hooks from its first check. The verbs a container or a scheduler runs never touch git. `problem` says why a
clone would not run them, which `basis check` reports with the fix.

git finds the clone from the repo's folder, never from a `GIT_DIR` that a hook or `git rebase --exec` exports, so a
`basis` command run from inside one never writes another clone's settings."""

import logging
import os
import shutil
from pathlib import Path

from basis.core.clone import output as _git

FOLDER = ".githooks"
HOOKS = ("pre-commit", "pre-push")

log = logging.getLogger(__name__)


def ensure(root: Path) -> None:
    """Point the clone that holds the repo at `root` at its hooks, when no git setting names a hooks folder and the
    hooks exist. Without git, outside a clone, or where git does not take the setting, it does nothing, and
    `basis check` says why."""
    if not all((root / FOLDER / hook).is_file() for hook in HOOKS) or (git := shutil.which("git")) is None:
        return
    top = _git(git, root, "rev-parse", "--show-toplevel")
    if top is None or _git(git, root, "config", "--get", "core.hooksPath") is not None:
        return
    setting = _setting(root, top)
    if _git(git, root, "config", "core.hooksPath", setting) is not None:
        log.info("git runs this clone's hooks from %s now", setting)


def problem(root: Path) -> tuple[str, str] | None:
    """Why the clone that holds the repo at `root` would not run its hooks, and the fix; None when it would."""
    if (git := shutil.which("git")) is None:
        return "git is not on the PATH, so this repo's commit and push hooks cannot run", "install git, and put it on the PATH"
    top = _git(git, root, "rev-parse", "--show-toplevel")
    hooks = _git(git, root, "rev-parse", "--git-path", "hooks") if top is not None else None
    if top is None or hooks is None:
        return f"{root} is in no git clone, so its commit and push hooks never run", "run `git init` in it, or work in a clone of the repo"
    if (root / hooks).resolve() != (root / FOLDER).resolve():
        return f"git runs this clone's hooks from {hooks}, not {FOLDER}/, so the repo's commit and push hooks never run", f"run `git config core.hooksPath {_setting(root, top)}`"
    return None


def unexecutable(root: Path) -> list[tuple[str, str]]:
    """Each hook the clone's index holds without its executable bit, as Git for Windows commits one, whose file system
    keeps none, and which git in WSL or on Linux then skips, with the fix."""
    if (git := shutil.which("git")) is None:
        return []
    listed = _git(git, root, "ls-files", "--stage", "--", *(f"{FOLDER}/{hook}" for hook in HOOKS)) or ""
    modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listed.splitlines() if "\t" in line}
    return [
        (f"{name} is committed without its executable bit, so git in WSL or on Linux skips it", f"run `git update-index --chmod=+x -- {name}`, and commit, as the commit hook now does")
        for name, mode in sorted(modes.items())
        if mode != "100755"
    ]


def _setting(root: Path, top: str) -> str:
    """`core.hooksPath` for the repo at `root`, in the clone whose top is `top`: relative to the top, where git runs
    hooks from, so the setting holds wherever the clone is."""
    return Path(os.path.relpath((root / FOLDER).resolve(), Path(top).resolve())).as_posix()
