"""The agent files a job repo carries, from the installed etl-framework: its block in `AGENTS.md`, between markers,
and the `etl-*` skills in `.agents/skills/`, the conventions every agent reads. `etl sync` rewrites them, and each
repo's `tests/test_agent_files.py` fails when they no longer match the installed version. They are copies, not
symlinks, since Git on Windows checks a symlink out as a text file by default."""

from pathlib import Path

from etl_framework.version import VERSION

_PACKAGE = Path(__file__).parent
BEGIN = "<!-- etl-framework:begin (written by `etl sync`; edit outside the markers) -->"
END = "<!-- etl-framework:end -->"


def block() -> str:
    return f"{BEGIN}\n{(_PACKAGE / 'templates' / 'agents.md').read_text(encoding='utf-8').replace('{version}', VERSION)}{END}\n"


def skills() -> dict[str, str]:
    """Each skill's `SKILL.md`, by its folder's name."""
    return {folder.name: (folder / "SKILL.md").read_text(encoding="utf-8") for folder in sorted((_PACKAGE / "skills").iterdir()) if (folder / "SKILL.md").exists()}


def expected(repo: Path) -> dict[Path, str]:
    """Each agent file's path in `repo`, and what it should hold."""
    agents = repo / "AGENTS.md"
    current = agents.read_text(encoding="utf-8") if agents.exists() else "# Agent guide\n\n"
    if BEGIN in current and END in current:
        before, _, rest = current.partition(BEGIN)
        _, _, after = rest.partition(END)
        updated = before + block().rstrip("\n") + after
    else:
        updated = current.rstrip("\n") + "\n\n" + block()
    files = {agents: updated}
    files |= {repo / ".agents" / "skills" / name / "SKILL.md": text for name, text in skills().items()}
    return files


def stale(repo: Path) -> list[Path]:
    """The agent files that do not match the installed etl-framework."""
    return [path for path, text in expected(repo).items() if not path.exists() or path.read_text(encoding="utf-8") != text]


def write(repo: Path) -> list[Path]:
    """Rewrite every stale agent file, and return them."""
    written: list[Path] = []
    for path, text in expected(repo).items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written
