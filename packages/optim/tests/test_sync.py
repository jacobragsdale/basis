"""What `basis sync` writes into a model repo from the installed optim: optim's block of `AGENTS.md`, which it rewrites
and keeps what the repo wrote around, and the `optim-*` skills, which it copies and removes once optim ships them no
more."""

from dataclasses import replace
from pathlib import Path

import optim
from basis.core import sync as core_sync
from optim import sync


def test_the_block_is_the_contract_of_the_installed_optim() -> None:
    files = sync.agent_files()

    assert files.part == "optim"
    assert files.block.startswith(f"## The optim contract\n\nWritten from optim {optim.__version__}.")
    assert set(files.skills) == {path.name for path in sync.SKILLS.iterdir() if (path / "SKILL.md").is_file()}
    assert all(name.startswith("optim-") for name in files.skills)


def test_what_a_repo_writes_around_the_block_is_kept_as_written(tmp_path: Path) -> None:
    sync.write(tmp_path)
    agents = tmp_path / "AGENTS.md"
    written = f"# Agent guide\n\nNotes before.\n\n{agents.read_text().removeprefix('# Agent guide').lstrip()}\n## This repo\n\nNotes after, below a blank line.\n"
    agents.write_text(written, encoding="utf-8", newline="")  # LF, as basis writes it, on Windows too

    assert sync.stale(tmp_path) == []
    assert sync.write(tmp_path) == []
    assert agents.read_text() == written


def test_a_stale_block_is_rewritten_and_the_notes_around_it_kept(tmp_path: Path) -> None:
    sync.write(tmp_path)
    agents = tmp_path / "AGENTS.md"
    current = agents.read_text()
    agents.write_text(f"{current}\n## Notes after\n".replace("Written from optim", "Once written from optim"))

    assert sync.write(tmp_path) == [tmp_path / "AGENTS.md"]
    assert agents.read_text() == f"{current}\n## Notes after\n"


def test_a_skill_optim_no_longer_ships_is_removed_from_both_skills_folders(tmp_path: Path) -> None:
    earlier = sync.agent_files()  # an earlier optim, which shipped one skill more
    core_sync.write(tmp_path, [replace(earlier, skills={**earlier.skills, "optim-retired": "---\nname: optim-retired\ndescription: Gone.\n---\n"})])

    assert sorted(path.relative_to(tmp_path).as_posix() for path in sync.write(tmp_path)) == [".agents/skills/optim-retired", ".claude/skills/optim-retired"]
    assert not (tmp_path / ".agents/skills/optim-retired").exists()
