"""The agent files a repo carries from each basis part it uses: the part's block in `AGENTS.md`, between markers, and
its skills in `.agents/skills/`, the conventions every agent reads. The part's `sync` command rewrites them, and the
repo's `tests/test_agent_files.py` fails when they no longer match the installed version. They are copies, not
symlinks, since Git on Windows checks a symlink out as a text file by default."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentFiles:
    """One part's agent files, as its installed version writes them."""

    part: str
    """The part's distribution, which names its block, such as `basis-job`."""
    command: str
    """The command whose `sync` rewrites them, such as `etl`."""
    block: str
    """The part's block of `AGENTS.md`, without its markers."""
    skills: dict[str, str]
    """Each skill's `SKILL.md`, by its folder's name."""

    @property
    def begin(self) -> str:
        return f"<!-- {self.part}:begin (written by `{self.command} sync`; edit outside the markers) -->"

    @property
    def end(self) -> str:
        return f"<!-- {self.part}:end -->"

    def marked(self) -> str:
        """The block between its markers."""
        return f"{self.begin}\n{self.block}{self.end}\n"


def skills(folder: Path) -> dict[str, str]:
    """Each skill's `SKILL.md` in `folder`, by its folder's name."""
    return {child.name: (child / "SKILL.md").read_text(encoding="utf-8") for child in sorted(folder.iterdir()) if (child / "SKILL.md").exists()}


def expected(repo: Path, files: AgentFiles) -> dict[Path, str]:
    """Each agent file's path in `repo`, and what it should hold."""
    agents = repo / "AGENTS.md"
    current = agents.read_text(encoding="utf-8") if agents.exists() else "# Agent guide\n\n"
    if files.begin in current and files.end in current:
        before, _, rest = current.partition(files.begin)
        _, _, after = rest.partition(files.end)
        updated = before + files.marked().rstrip("\n") + after
    else:
        updated = current.rstrip("\n") + "\n\n" + files.marked()
    found = {agents: updated}
    found |= {repo / ".agents" / "skills" / name / "SKILL.md": text for name, text in files.skills.items()}
    return found


def stale(repo: Path, files: AgentFiles) -> list[Path]:
    """The agent files that do not match the installed part."""
    return [path for path, text in expected(repo, files).items() if not path.exists() or path.read_text(encoding="utf-8") != text]


def write(repo: Path, files: AgentFiles) -> list[Path]:
    """Rewrite every stale agent file, and return them."""
    written: list[Path] = []
    for path, text in expected(repo, files).items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written
