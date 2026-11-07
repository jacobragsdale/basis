from pathlib import Path

from basis.core.sync import AgentFiles, expected

FILES = AgentFiles("new-part", "the new block\n", {}, formerly=("old-part",))
NEW = "<!-- new-part:begin (written by `basis sync`; edit outside the markers) -->\nthe new block\n<!-- new-part:end -->"
OLD = "<!-- old-part:begin (written by `basis sync`; edit outside the markers) -->\nthe old block\n<!-- old-part:end -->"


def agents_md(repo: Path, text: str) -> str:
    (repo / "AGENTS.md").write_text(text, encoding="utf-8")
    return expected(repo, FILES)[repo / "AGENTS.md"]


def test_a_block_under_a_former_name_is_replaced_where_it_stands(tmp_path: Path) -> None:
    assert agents_md(tmp_path, f"# Agent guide\n\n{OLD}\n\n## Ours\n") == f"# Agent guide\n\n{NEW}\n\n## Ours\n"


def test_a_block_under_a_former_name_beside_the_current_one_is_removed(tmp_path: Path) -> None:
    assert agents_md(tmp_path, f"# Agent guide\n\n{OLD}\n\n## Ours\n\n{NEW}\n") == f"# Agent guide\n\n## Ours\n\n{NEW}\n"
    assert agents_md(tmp_path, f"# Agent guide\n\n{NEW}\n\n{OLD}\n") == f"# Agent guide\n\n{NEW}\n"


def test_a_repo_with_no_block_gets_one_at_the_end(tmp_path: Path) -> None:
    assert agents_md(tmp_path, "# Agent guide\n\n## Ours\n") == f"# Agent guide\n\n## Ours\n\n{NEW}\n"
