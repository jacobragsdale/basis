import json
import os
from pathlib import Path

import pytest

from basis.core.sync import DENY, MARKER, REPO_FILES, AgentFiles, SyncError, core, expected, stale, write

FILES = AgentFiles("new-part", "the new block\n", {"job-new": "# Make a job\n"}, image="#   a job: run JOB --publish\n", formerly=("old-part",), retired=("tests/test_agent_files.py",))
NEW = "<!-- new-part:begin (written by `basis sync`; edit outside the markers) -->\nthe new block\n<!-- new-part:end -->"
OLD = "<!-- old-part:begin (written by `basis sync`; edit outside the markers) -->\nthe old block\n<!-- old-part:end -->"
GONE = "<!-- gone-part:begin (written by `etl sync`; edit outside the markers) -->\na part no longer installed\n<!-- gone-part:end -->"
BASIS = core().marked()
"""basis's own block, which every repo's `AGENTS.md` ends with when it has none yet."""


def agents_md(repo: Path, text: str) -> str:
    (repo / "AGENTS.md").write_text(text, encoding="utf-8")
    return expected(repo, [FILES])[repo / "AGENTS.md"]


def put(repo: Path, relative: str, text: str = "x\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("current", "synced"),
    [
        (f"# Agent guide\n\n{OLD}\n\n## Ours\n", f"# Agent guide\n\n{NEW}\n\n## Ours\n"),
        (f"# Agent guide\n\n{OLD}\n\n## Ours\n\n{NEW}\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
        (f"# Agent guide\n\n{NEW}\n\n{OLD}\n", f"# Agent guide\n\n{NEW}\n"),
        ("# Agent guide\n\n## Ours\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
        (f"# Agent guide\n\n{GONE}\n\n## Ours\n\n{NEW}\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
    ],
    ids=["former-name-in-place", "former-beside-current", "former-after-current", "none-yet", "a-part-no-longer-installed"],
)
def test_each_parts_block_is_current_and_every_other_block_sync_wrote_is_removed(tmp_path: Path, current: str, synced: str) -> None:
    assert agents_md(tmp_path, current) == f"{synced}\n{BASIS}"


def test_basiss_own_block_stays_where_it_is(tmp_path: Path) -> None:
    current = f"# Agent guide\n\n{BASIS}\n## Ours\n\n{NEW}\n"
    assert agents_md(tmp_path, current) == current


def test_a_repo_has_the_framework_files_each_opening_with_the_marker_and_the_dockerfile_lists_each_parts_command(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    for path in REPO_FILES:
        assert any(MARKER in line for line in (tmp_path / path).read_text(encoding="utf-8").splitlines()[:3]), path
    assert "#   a job: run JOB --publish\nENTRYPOINT" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (tmp_path / ".claude/skills/job-new/SKILL.md").read_text(encoding="utf-8") == (tmp_path / ".agents/skills/job-new/SKILL.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))["permissions"] == {"deny": list(DENY)}
    assert stale(tmp_path, [FILES]) == []


def test_sync_removes_what_basis_owns_and_no_part_writes_now(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    skill = put(tmp_path, ".agents/skills/job-port/SKILL.md")
    copy = put(tmp_path, ".claude/skills/job-port/SKILL.md")
    marked = put(tmp_path, "deploy/cronjob.yaml", f"# This file is {MARKER} from the installed version.\nkind: CronJob\n")
    retired = put(tmp_path, "tests/test_agent_files.py")
    assert stale(tmp_path, [FILES]) == [skill.parent, copy.parent, marked, retired]
    assert write(tmp_path, [FILES]) == [skill.parent, copy.parent, marked, retired]
    assert not any(path.exists() for path in (skill.parent, copy.parent, marked, retired))
    assert stale(tmp_path, [FILES]) == []


def test_sync_leaves_what_basis_does_not_own(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    own_skill = put(tmp_path, ".agents/skills/release-notes/SKILL.md")
    own_claude_skill = put(tmp_path, ".claude/skills/release-notes/SKILL.md")
    mentioned = put(tmp_path, "README.md", f"# Pricing\n\nA repo of jobs.\n\nIts Dockerfile is {MARKER}.\n")
    in_venv = put(tmp_path, ".venv/lib/notes.txt", f"{MARKER}\n")
    binary = tmp_path / "data.parquet"
    binary.write_bytes(b"PAR1\xff\xfe\x00" * 100)
    assert stale(tmp_path, [FILES]) == []
    assert write(tmp_path, [FILES]) == []
    assert all(path.exists() for path in (own_skill, own_claude_skill, mentioned, in_venv, binary))


@pytest.mark.parametrize("relative", [".agents/skills/job-new/SKILL.md", ".claude/skills/job-new/SKILL.md", "Dockerfile", "CLAUDE.md"])
def test_an_edited_file_is_stale_until_synced(tmp_path: Path, relative: str) -> None:
    write(tmp_path, [FILES])
    written = (tmp_path / relative).read_text(encoding="utf-8")
    edited = put(tmp_path, relative, written + "# my way\n")
    assert stale(tmp_path, [FILES]) == [edited]
    assert write(tmp_path, [FILES]) == [edited]
    assert edited.read_text(encoding="utf-8") == written


def test_a_file_checked_out_with_crlf_line_endings_is_stale_and_sync_writes_lf(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    hook = tmp_path / ".githooks/pre-commit"
    lf = hook.read_bytes()
    hook.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert stale(tmp_path, [FILES]) == [hook]
    write(tmp_path, [FILES])
    assert hook.read_bytes() == lf


@pytest.mark.skipif(os.name == "nt", reason="Git for Windows runs a hook whatever its mode")
def test_the_hooks_are_executable_and_one_that_is_not_is_stale(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    hooks = [tmp_path / ".githooks/pre-commit", tmp_path / ".githooks/pre-push"]
    assert all(os.access(hook, os.X_OK) for hook in hooks)
    hooks[1].chmod(0o644)
    assert stale(tmp_path, [FILES]) == [hooks[1]]
    write(tmp_path, [FILES])
    assert os.access(hooks[1], os.X_OK)


def test_claude_settings_keep_the_repos_own_keys_and_rules_and_gain_the_missing_deny_rules(tmp_path: Path) -> None:
    own = {"permissions": {"allow": ["Bash(uv run pytest *)"], "deny": ["Read(./.env)", DENY[0]]}, "env": {"BASIS_LOG_LEVEL": "DEBUG"}}
    settings = put(tmp_path, ".claude/settings.json", json.dumps(own))
    write(tmp_path, [FILES])
    assert json.loads(settings.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Bash(uv run pytest *)"], "deny": ["Read(./.env)", *DENY]}, "env": {"BASIS_LOG_LEVEL": "DEBUG"}}
    reformatted = put(tmp_path, ".claude/settings.json", json.dumps(json.loads(settings.read_text(encoding="utf-8")), indent=4))
    assert stale(tmp_path, [FILES]) == []
    assert write(tmp_path, [FILES]) == []
    assert reformatted.read_text(encoding="utf-8").startswith('{\n    "permissions"')


@pytest.mark.parametrize(("text", "problem"), [("{", "is not JSON"), ('{"permissions": {"deny": "Bash"}}', "is not Claude Code's settings")], ids=["not-json", "deny-not-a-list"])
def test_claude_settings_basis_cannot_read_say_how_to_fix_them(tmp_path: Path, text: str, problem: str) -> None:
    put(tmp_path, ".claude/settings.json", text)
    with pytest.raises(SyncError, match=rf"{problem}.*: fix it, or remove it, and run `uv run basis sync`"):
        stale(tmp_path, [FILES])
