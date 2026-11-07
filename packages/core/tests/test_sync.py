from pathlib import Path

import pytest

from basis.core.sync import MARKER, AgentFiles, expected, stale, write

FILES = AgentFiles("new-part", "the new block\n", {"job-new": "# Make a job\n"}, formerly=("old-part",), retired=("tests/test_agent_files.py",))
NEW = "<!-- new-part:begin (written by `basis sync`; edit outside the markers) -->\nthe new block\n<!-- new-part:end -->"
OLD = "<!-- old-part:begin (written by `basis sync`; edit outside the markers) -->\nthe old block\n<!-- old-part:end -->"
GONE = "<!-- gone-part:begin (written by `etl sync`; edit outside the markers) -->\na part no longer installed\n<!-- gone-part:end -->"


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
    assert agents_md(tmp_path, current) == synced


def test_sync_removes_what_basis_owns_and_no_part_writes_now(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    skill = put(tmp_path, ".agents/skills/job-port/SKILL.md")
    marked = put(tmp_path, "Dockerfile", f"# syntax=docker/dockerfile:1\n# This file is {MARKER} from the installed version.\n")
    retired = put(tmp_path, "tests/test_agent_files.py")
    assert stale(tmp_path, [FILES]) == [skill.parent, marked, retired]
    assert write(tmp_path, [FILES]) == [skill.parent, marked, retired]
    assert not marked.exists()
    assert not skill.parent.exists()
    assert not retired.exists()
    assert stale(tmp_path, [FILES]) == []


def test_sync_leaves_what_basis_does_not_own(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    own_skill = put(tmp_path, ".agents/skills/release-notes/SKILL.md")
    mentioned = put(tmp_path, "README.md", f"# Pricing\n\nA repo of jobs.\n\nIts Dockerfile is {MARKER}.\n")
    in_venv = put(tmp_path, ".venv/lib/notes.txt", f"{MARKER}\n")
    binary = tmp_path / "data.parquet"
    binary.write_bytes(b"PAR1\xff\xfe\x00" * 100)
    assert stale(tmp_path, [FILES]) == []
    assert write(tmp_path, [FILES]) == []
    assert all(path.exists() for path in (own_skill, mentioned, in_venv, binary))


def test_an_edited_skill_is_stale_until_synced(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    skill = put(tmp_path, ".agents/skills/job-new/SKILL.md", "# Make a job, my way\n")
    assert stale(tmp_path, [FILES]) == [skill]
    assert write(tmp_path, [FILES]) == [skill]
    assert skill.read_text(encoding="utf-8") == "# Make a job\n"
