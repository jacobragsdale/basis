"""Every file of the workspace git does not track and does not ignore, one line each, and exit 1 where there is any:
`pre-commit run --all-files` checks the files git tracks alone (`git ls-files`), so a new file nobody has added yet
passes it, and is checked first by the commit hook, which then flags what the run before it said nothing of.

    uv run python scripts/untracked.py

`.pre-commit-config.yaml` runs it where that file is among the files checked, which `--all-files` always is and a
commit only when it changes the hooks: so the two check the same files, and a commit of some files beside others not
yet added is never stopped by them."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def untracked(root: Path) -> list[str]:
    """Each file under `root` git neither tracks nor ignores, as git names it."""
    listed = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root, capture_output=True, check=True)  # noqa: S607 -- git from PATH, as the hooks run it
    return sorted(name for name in listed.stdout.decode("utf-8").split("\0") if name)


def main() -> int:
    if not (found := untracked(ROOT)):
        return 0
    fix = "git add them, or git add --intent-to-add to check them and leave them out of the next commit, or ignore them"
    sys.stderr.write(f"ERROR: pre-commit checks the files git tracks alone, and these are untracked: {fix}\n")
    sys.stderr.writelines(f"{name}\n" for name in found)
    return 1


if __name__ == "__main__":
    sys.exit(main())
