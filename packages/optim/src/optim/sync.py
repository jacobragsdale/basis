"""The agent files `optim sync` writes into a model repo, from the installed `optim`: the core's one-page contract, as
a marked block in `AGENTS.md`, and the core's skills, copied into `.agents/skills/optim-*`. They are the open
conventions every agent reads, Copilot's included, so no repo holds a vendor's own files. The skills are copies rather
than symlinks because Git on Windows checks a symlink out as a plain text file unless Developer Mode and
`core.symlinks` are on. The repo's `tests/test_agent_files.py` fails when the files do not match the installed `optim`,
so an upgrade brings its guidance with it."""

from pathlib import Path

import optim

PACKAGE = Path(__file__).parent
SKILLS = PACKAGE / "skills"
BEGIN, END = "<!-- optim:begin -->", "<!-- optim:end -->"
SKILL_DIRS = (Path(".agents") / "skills",)


def block() -> str:
    """The marked block, stamped with the installed version, so an upgrade makes it stale."""
    contract = (PACKAGE / "templates" / "contract.md").read_text(encoding="utf-8").replace("{version}", optim.__version__)
    return f"{BEGIN}\n{contract.rstrip()}\n{END}"


def expected(repo: Path) -> dict[Path, str]:
    """Every agent file, by its path in the repo, as `optim sync` would write it. `AGENTS.md` keeps what the repo wrote
    before and after the block exactly as written."""
    agents = repo / "AGENTS.md"
    current = agents.read_text(encoding="utf-8") if agents.exists() else ""
    before, found, rest = current.partition(BEGIN)
    files = {Path("AGENTS.md"): f"{before}{block()}{rest.partition(END)[2]}" if found else f"{current.rstrip()}\n\n{block()}\n".lstrip("\n")}
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        for directory in SKILL_DIRS:
            files[directory / skill.parent.name / "SKILL.md"] = skill.read_text(encoding="utf-8")
    return files


def stale(repo: Path) -> list[Path]:
    """The agent files that do not match the installed `optim`, and skill copies it no longer ships."""
    wrong = [path for path, text in expected(repo).items() if not (repo / path).exists() or (repo / path).read_text(encoding="utf-8") != text]
    shipped = {skill.parent.name for skill in SKILLS.glob("*/SKILL.md")}
    return wrong + [found.relative_to(repo) for directory in SKILL_DIRS for found in sorted((repo / directory).glob("optim-*")) if found.name not in shipped]


def sync(repo: Path) -> list[Path]:
    """Write every stale agent file, and delete skill copies the installed `optim` no longer ships. Returns the paths
    it changed."""
    changed = stale(repo)
    files = expected(repo)
    for path in changed:
        target = repo / path
        if path in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[path], encoding="utf-8", newline="\n")
        else:
            for file in sorted(target.rglob("*"), reverse=True):
                file.unlink() if file.is_file() else file.rmdir()
            target.rmdir()
    return changed
