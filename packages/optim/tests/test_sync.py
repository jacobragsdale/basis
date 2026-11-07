"""`optim sync`: the contract block in a model repo's `AGENTS.md`, which it rewrites, and what the repo wrote around it,
which it keeps as written."""

from pathlib import Path

from optim import sync


def test_what_a_repo_writes_around_the_block_is_kept_as_written(tmp_path: Path) -> None:
    sync.sync(tmp_path)
    agents = tmp_path / "AGENTS.md"
    written = f"# Agent guide\n\nNotes before.\n\n{agents.read_text()}\n## This repo\n\nNotes after, below a blank line.\n"
    agents.write_text(written)

    assert sync.stale(tmp_path) == []
    assert sync.sync(tmp_path) == []
    assert agents.read_text() == written


def test_a_stale_block_is_rewritten_and_the_notes_around_it_kept(tmp_path: Path) -> None:
    sync.sync(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(f"Notes before.\n\n{agents.read_text().replace('Written by', 'Once written by')}\n## Notes after\n")

    assert sync.sync(tmp_path) == [Path("AGENTS.md")]
    assert agents.read_text() == f"Notes before.\n\n{sync.block()}\n\n## Notes after\n"
