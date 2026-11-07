import os
from collections.abc import Callable
from pathlib import Path

import pytest

from basis.core.hygiene import LIMIT_KB, problems

type Git = Callable[..., str]
"""conftest's `git` fixture."""

KEY = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH " + "PRIVATE KEY-----\n"  # in pieces, so this file holds none


@pytest.fixture
def clone(tmp_path: Path, git: Git) -> Path:
    """A clone with nothing committed yet, as a repo's first commit finds it."""
    git(tmp_path, "init", "--quiet")
    return tmp_path


def stage(clone: Path, git: Git, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    git(clone, "add", *files)


def test_staged_files_that_pass_have_no_problems(clone: Path, git: Git) -> None:
    stage(
        clone,
        git,
        {
            "deploy/cronjob.yaml": "kind: CronJob\n---\nkind: Secret\ndata: !Ref Store\n",  # several documents, and a tag no loader knows
            "pyproject.toml": '[project]\nname = "loads"\n',
            "fixture.csv": "a\n" * (LIMIT_KB * 512),  # exactly the limit
            "notes.md": "Keys stay out: never commit a `BEGIN RSA` line.\n",
        },
    )
    assert problems(clone) == []


def test_each_problem_is_one_line_with_its_fix(clone: Path, git: Git) -> None:
    stage(
        clone,
        git,
        {
            "keys/deploy": KEY,
            "data.parquet": b"\0" * (LIMIT_KB * 1024 + 1),
            "deploy.yml": "steps:\n  - run: one\n - run: two\n",
            "settings.toml": "[tool\nname = 1\n",
            "latin.toml": "name = 'caf\xe9'\n".encode("latin-1"),
        },
    )
    found = problems(clone)
    assert [reason.split(" (")[0] for reason, _ in found] == [
        "data.parquet is 500 KB, over the 500 KB a staged file may be",
        "deploy.yml is not YAML",
        "keys/deploy holds a private key",
        "latin.toml is not TOML",
        "settings.toml is not TOML",
    ]
    assert "at line 3, column 2)" in found[1][0]
    assert all(fix == "fix it, and stage it again" for _, fix in found[3:])
    assert dict(found)["keys/deploy holds a private key"] == "unstage it with `git rm --cached keys/deploy`, keep the key out of the repo, and replace it if it was ever pushed"
    assert dict(found)["data.parquet is 500 KB, over the 500 KB a staged file may be"].startswith("unstage it with `git rm --cached data.parquet`")


def test_only_what_is_staged_counts_as_it_is_staged(clone: Path, git: Git) -> None:
    stage(clone, git, {"settings.toml": "name = 1\n"})
    (clone / "settings.toml").write_text("[broken\n", encoding="utf-8")  # changed, not staged
    (clone / "untracked.yaml").write_text(": : :\n", encoding="utf-8")
    assert problems(clone) == []


def test_the_index_git_commits_from_is_the_one_read(clone: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    """`git commit -a` and `git commit PATH` stage into a temporary index, which they name in `GIT_INDEX_FILE`."""
    stage(clone, git, {"settings.toml": "name = 1\n"})
    (clone / "settings.toml").write_text("[broken\n", encoding="utf-8")
    monkeypatch.setenv("GIT_INDEX_FILE", str(clone / ".git" / "commit-index"))
    git(clone, "add", "settings.toml")
    assert [reason.split(" (")[0] for reason, _ in problems(clone)] == ["settings.toml is not TOML"]
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert problems(clone) == []


def test_without_git_it_says_to_install_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    assert problems(tmp_path) == [("git is not on the PATH, so the staged files cannot be read", "install git, and put it on the PATH")]
    assert os.environ["PATH"].endswith("no-tools")
