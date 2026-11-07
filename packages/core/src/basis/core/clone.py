"""What basis reads of the git clone a repo is in: where it is, the repo's folder in it, its commit, whether the repo's
folder holds changes no commit has, and the git folder its worktrees share, where `basis check --full` keeps what it
found for `basis review`. git finds the clone from the repo's folder, never from a `GIT_DIR` that a hook or `git rebase
--exec` exports, so a `basis` command run from inside one never reads or writes another clone."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LOCATION = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"})
"""The variables that point git at a clone other than the one its working directory is in."""


def output(git: str, root: Path, *argv: str) -> str | None:
    """What `git ARGV` prints, run in `root` with the git at `git`, or None when it fails."""
    environment = {name: value for name, value in os.environ.items() if name not in _LOCATION}
    found = subprocess.run([git, *argv], cwd=root, env=environment, capture_output=True, text=True, check=False)
    return found.stdout.strip() if found.returncode == 0 else None


@dataclass(frozen=True)
class Clone:
    """The clone the repo at `root` is in."""

    root: Path
    git: str
    """The git on the PATH."""
    top: Path
    prefix: str
    """The repo's folder in the clone, `/`-separated, with a trailing `/`, or empty at its top."""
    common: Path
    """The git folder every worktree of the clone shares."""

    def head(self) -> str | None:
        """The commit checked out, or None before the first."""
        return self.commit("HEAD")

    def commit(self, ref: str) -> str | None:
        """The full id of the commit `ref` names, or None when it names none."""
        return output(self.git, self.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")

    def clean(self) -> bool:
        """Whether the repo's folder holds nothing its commit does not: no change, staged or not, and no file git does
        not ignore and does not track."""
        return output(self.git, self.root, "status", "--porcelain", "--", ".") == ""


def of(root: Path) -> Clone | None:
    """The clone the repo at `root` is in, or None outside a clone or without git."""
    if (git := shutil.which("git")) is None:
        return None
    top = output(git, root, "rev-parse", "--show-toplevel")
    prefix = output(git, root, "rev-parse", "--show-prefix")
    common = output(git, root, "rev-parse", "--git-common-dir")
    if top is None or prefix is None or common is None:
        return None
    return Clone(root, git, Path(top), prefix, (root / common).resolve())
