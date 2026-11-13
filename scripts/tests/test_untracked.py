"""`scripts/untracked.py`: `pre-commit run --all-files` checks only what git tracks, so the files it does not are refused
there, by name, with the fix."""

import subprocess
from pathlib import Path

import pytest
import untracked


def test_a_file_git_neither_tracks_nor_ignores_is_named(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S607 -- git from PATH
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    for name in ("ignored.py", "new.py", "tracked.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=tmp_path, check=True)  # noqa: S607
    assert untracked.untracked(tmp_path) == ["new.py"]
    subprocess.run(["git", "add", "--intent-to-add", "new.py"], cwd=tmp_path, check=True)  # noqa: S607
    assert untracked.untracked(tmp_path) == []


def test_the_workspace_fails_while_one_is_there(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(untracked, "untracked", lambda _: ["packages/dash/src/basis/dash/new.py"])
    assert untracked.main() == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("ERROR: pre-commit checks the files git tracks alone, and these are untracked: git add them")
    assert lines[1:] == ["packages/dash/src/basis/dash/new.py"]
    monkeypatch.setattr(untracked, "untracked", lambda _: [])
    assert untracked.main() == 0


def test_the_hook_runs_it_wherever_all_files_are_checked() -> None:
    config = (untracked.ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "entry: uv run python scripts/untracked.py" in config
    assert "files: ^\\.pre-commit-config\\.yaml$" in config
