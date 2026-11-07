"""The files `basis sync` writes into a repo from its parts' installed versions, and removes once no part writes them:
each part's block of `AGENTS.md`, between markers, and its skills in `.agents/skills/`, the conventions every agent
reads. They are copies, not symlinks, since Git on Windows checks a symlink out as a text file by default.

basis owns, in a repo:

- each block of `AGENTS.md` whose begin marker says `sync` writes it; what is outside the blocks is the repo's own;
- each folder of `.agents/skills/` whose name starts with a part's name and a dash, such as `job-new`;
- each file whose first lines carry `MARKER`;
- each path a part lists as `retired`: what an earlier version of it wrote before the marker existed.

`stale` lists what differs from what the parts write, whether to rewrite or to remove, which `basis check` reports,
and `write` makes the repo match, which `basis sync` does. Both take every part the repo has at once, since what one
part no longer writes may be another's."""

import os
import re
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

PARTS = ("job", "api", "optim")
"""basis's parts, by name: each owns the skill folders its name and a dash begin."""
MARKER = "managed by basis: `basis sync` rewrites it"
"""What a file basis writes carries in its first lines, in a comment of the file's own syntax."""
SKILLS = Path(".agents") / "skills"
_HEAD = 3
"""Lines a file's marker may be in: the first line can be one a format needs, such as a Dockerfile's `# syntax=`."""
_LINE = 512
"""The most of a line read for its marker, so a large file with no line breaks is never read whole."""
_UNREAD = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", "snapshots"}
"""Folders whose files basis never writes, so no marker is looked for in them."""
_BLOCK = re.compile(r"<!-- (?P<name>[\w.-]+):begin \(written by `[\w -]*sync`[^)]*\) -->.*?<!-- (?P=name):end -->(?P<tail>\n*)", re.DOTALL)


@dataclass(frozen=True)
class AgentFiles:
    """One part's agent files, as its installed version writes them."""

    part: str
    """The part's distribution, which names its block, such as `basis-job`."""
    block: str
    """The part's block of `AGENTS.md`, without its markers."""
    skills: dict[str, str]
    """Each skill's `SKILL.md`, by its folder's name, which starts with the part's name and a dash."""
    formerly: tuple[str, ...] = ()
    """Names the part's block had before, such as `etl-framework`, whose blocks `sync` replaces in place."""
    retired: tuple[str, ...] = ()
    """Files and folders, relative to the repo, that an earlier version of the part wrote without `MARKER`, and this
    one does not write: `sync` removes them."""

    @property
    def begin(self) -> str:
        return f"<!-- {self.part}:begin (written by `basis sync`; edit outside the markers) -->"

    @property
    def end(self) -> str:
        return f"<!-- {self.part}:end -->"

    def marked(self) -> str:
        """The block between its markers."""
        return f"{self.begin}\n{self.block}{self.end}\n"


def skills(folder: Path) -> dict[str, str]:
    """Each skill's `SKILL.md` in `folder`, by its folder's name."""
    return {child.name: (child / "SKILL.md").read_text(encoding="utf-8") for child in sorted(folder.iterdir()) if (child / "SKILL.md").exists()}


def expected(repo: Path, parts: Sequence[AgentFiles]) -> dict[Path, str]:
    """Each file the parts write in `repo`, and what it should hold."""
    agents = repo / "AGENTS.md"
    current = agents.read_text(encoding="utf-8") if agents.exists() else "# Agent guide\n\n"
    found = {agents: _agents(current, parts)}
    for files in parts:
        found.update({repo / SKILLS / name / "SKILL.md": text for name, text in files.skills.items()})
    return found


def unwritten(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What basis owns in `repo` that none of `parts` writes now."""
    wanted = expected(repo, parts)
    folders = [folder for folder in _children(repo / SKILLS) if folder.is_dir() and folder.name.startswith(tuple(f"{part}-" for part in PARTS))]
    found = {folder for folder in folders if folder / "SKILL.md" not in wanted}
    found |= {path for path in _marked(repo) if path not in wanted}
    found |= {repo / path for files in parts for path in files.retired if (repo / path).exists()}
    return sorted(found)


def stale(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What `write` would change: each file that does not hold what the parts write, and each that none of them writes."""
    differ = [path for path, text in expected(repo, parts).items() if not path.exists() or path.read_text(encoding="utf-8") != text]
    return differ + unwritten(repo, parts)


def write(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """Rewrite each file that does not hold what the parts write, remove each that none of them writes, and return
    them."""
    changed: list[Path] = []
    for path, text in expected(repo, parts).items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            changed.append(path)
    for path in unwritten(repo, parts):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        changed.append(path)
    return changed


def _agents(current: str, parts: Sequence[AgentFiles]) -> str:
    """`AGENTS.md` with each part's block current: in place of its own block, or else of one under a former name, or
    added at the end. Every other block that `sync` wrote is removed."""
    blocks = list(_BLOCK.finditer(current))
    kept: dict[int, AgentFiles] = {}
    for files in parts:
        own = [block for block in blocks if block["name"] == files.part]
        former = [block for block in blocks if block["name"] in files.formerly]
        if chosen := (own or former)[:1]:
            kept[chosen[0].start()] = files
    pieces: list[str] = []
    end = 0
    for block in blocks:
        pieces.append(current[end : block.start()])
        if (files := kept.get(block.start())) is not None:
            pieces.append(files.marked().rstrip("\n") + block["tail"])
        end = block.end()
    updated = "".join([*pieces, current[end:]])
    placed = {files.part for files in kept.values()}
    for files in parts:
        if files.part not in placed:
            updated = updated.rstrip("\n") + "\n\n" + files.marked()
    return updated.rstrip("\n") + "\n"


def _children(folder: Path) -> list[Path]:
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _marked(repo: Path) -> Iterator[Path]:
    """Each file in `repo` whose first lines carry `MARKER`, outside the folders basis never writes in."""
    for folder, names, files in os.walk(repo):
        names[:] = sorted(name for name in names if name not in _UNREAD and not (Path(folder) / name / "pyvenv.cfg").exists())
        for name in sorted(files):
            path = Path(folder) / name
            try:
                with path.open(encoding="utf-8") as opened:
                    head = [opened.readline(_LINE) for _ in range(_HEAD)]
            except UnicodeDecodeError, OSError:  # a binary or unreadable file carries no marker
                continue
            if any(MARKER in line for line in head):
                yield path
