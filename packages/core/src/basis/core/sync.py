"""The files `basis sync` writes into a repo from the installed versions of basis and its parts, and removes once
none of them writes them. They are copies, not symlinks, since Git on Windows checks a symlink out as a text file by
default, and they are written with LF line endings and compared byte for byte, as `.gitattributes` checks them out.

basis owns, in a repo:

- the framework files, `REPO_FILES`: the Dockerfile, whose last lines are each part's `image`, the deploy pipeline,
  the commit and push hooks, and `.gitattributes`;
- each block of `AGENTS.md` whose begin marker says `sync` writes it: basis's own, `CORE`, and each part's; what is
  outside the blocks is the repo's own;
- `CLAUDE.md`, which holds only `@AGENTS.md`: Claude Code reads `AGENTS.md` only through it;
- each folder of `.agents/skills/` whose name starts with a part's name and a dash, such as `job-new`, and its copy in
  `.claude/skills/`, the one skills folder Claude Code reads;
- the rules `DENY` in `.claude/settings.json`, which keeps every other key and rule it holds;
- each file whose first lines carry `MARKER`;
- each path a part lists as `retired`: what an earlier version of it wrote before the marker existed.

`CLAUDE.md` and `.claude/settings.json` carry no marker: the one holds a single line, and the other is JSON, which has
no comments. `stale` lists what differs from what basis writes, whether to rewrite or to remove, which `basis check`
reports, and `write` makes the repo match, which `basis sync` does. Both take every part the repo has at once, since
what one part no longer writes may be another's."""

import json
import os
import re
import shutil
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from basis.core.errors import BasisError
from basis.core.hooks import FOLDER, HOOKS

PARTS = ("job", "api", "optim")
"""basis's parts, by name: each owns the skill folders its name and a dash begin."""
MARKER = "managed by basis: `basis sync` rewrites it"
"""What a file basis writes carries in its first lines, in a comment of the file's own syntax."""
CORE = "basis"
"""The name of basis's own block of `AGENTS.md`."""
TEMPLATES = Path(__file__).parent / "templates" / "sync"
REPO_FILES = {"Dockerfile": "Dockerfile", "azure-pipelines.yml": "azure-pipelines.yml", ".gitattributes": "gitattributes", **{f"{FOLDER}/{hook}": hook for hook in HOOKS}}
"""Each framework file, by its path in a repo, and its template in `TEMPLATES`, whose `{image}` the parts' lines
replace. A template's name has no leading dot, so no tool takes it for this repo's own file."""
EXECUTABLE = frozenset(f"{FOLDER}/{hook}" for hook in HOOKS)
"""The files git runs, which it skips unless they are executable: on Windows, Git runs a hook whatever its mode."""
SKILLS = Path(".agents") / "skills"
CLAUDE_SKILLS = Path(".claude") / "skills"
CLAUDE_SETTINGS = Path(".claude") / "settings.json"
DENY = (
    "Bash(git commit *--no-verify*)",
    "Bash(git push *--no-verify*)",
    "Bash(git * commit *--no-verify*)",
    "Bash(git * push *--no-verify*)",
    "Bash(git commit -n *)",
    "Bash(git commit * -n)",
)  # fmt: skip
"""Claude Code's rules against skipping the hooks: `--no-verify` anywhere in a commit or a push, after `git` alone or
after options such as `-C DIR`, and `-n`, a commit's short form of it, as its first option or its last; a rule for
`-n` between them would refuse a commit whose message holds ` -n `. A rule matches the command as an agent writes it,
not every way of running git, so the hooks are as strong as `AGENTS.md`'s rule to keep them."""
SCHEMA = "https://json.schemastore.org/claude-code-settings.json"
_HEAD = 3
"""Lines a file's marker may be in: the first line can be one a format needs, such as a Dockerfile's `# syntax=`."""
_LINE = 512
"""The most of a line read for its marker, so a large file with no line breaks is never read whole."""
_UNREAD = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", "snapshots"}
"""Folders whose files basis never writes, so no marker is looked for in them."""
_BLOCK = re.compile(r"<!-- (?P<name>[\w.-]+):begin \(written by `[\w -]*sync`[^)]*\) -->.*?<!-- (?P=name):end -->(?P<tail>\n*)", re.DOTALL)


@dataclass(frozen=True)
class AgentFiles:
    """What one part writes in a repo, as its installed version writes it: its agent files, and its lines of the
    repo's Dockerfile."""

    part: str
    """The part's distribution, which names its block, such as `basis-job`."""
    block: str
    """The part's block of `AGENTS.md`, without its markers."""
    skills: dict[str, str]
    """Each skill's `SKILL.md`, by its folder's name, which starts with the part's name and a dash."""
    image: str = ""
    """The part's lines of the Dockerfile, before its entry point: a comment giving the command a container of the
    part runs, such as a job's `run JOB --publish`, and any instruction the part needs."""
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


class SyncError(BasisError, ValueError):
    """A file basis writes part of, which it cannot read."""


def core() -> AgentFiles:
    """basis's own block of `AGENTS.md`, which every repo has, whatever its parts."""
    return AgentFiles(CORE, (TEMPLATES / "agents.md").read_text(encoding="utf-8"), {})


def expected(repo: Path, parts: Sequence[AgentFiles]) -> dict[Path, str]:
    """Each file basis and `parts` write in `repo`, and what it should hold."""
    agents = repo / "AGENTS.md"
    current = _read(agents).replace("\r\n", "\n") if agents.exists() else "# Agent guide\n\n"
    image = "".join(files.image for files in parts)
    found = {repo / path: (TEMPLATES / template).read_text(encoding="utf-8").replace("{image}", image) for path, template in REPO_FILES.items()}
    found[agents] = _agents(current, [*parts, core()])
    found[repo / "CLAUDE.md"] = "@AGENTS.md\n"
    found[repo / CLAUDE_SETTINGS] = _claude_settings(repo / CLAUDE_SETTINGS)
    for files in parts:
        for name, text in files.skills.items():
            found.update({repo / folder / name / "SKILL.md": text for folder in (SKILLS, CLAUDE_SKILLS)})
    return found


def unwritten(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What basis owns in `repo` that neither it nor any of `parts` writes now."""
    wanted = expected(repo, parts)
    prefixes = tuple(f"{part}-" for part in PARTS)
    folders = [folder for skills in (SKILLS, CLAUDE_SKILLS) for folder in _children(repo / skills) if folder.is_dir() and folder.name.startswith(prefixes)]
    found = {folder for folder in folders if folder / "SKILL.md" not in wanted}
    found |= {path for path in _marked(repo) if path not in wanted}
    found |= {repo / path for files in parts for path in files.retired if (repo / path).exists()}
    return sorted(found)


def stale(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What `write` would change: each file that does not hold what basis and the parts write, or a hook git would
    skip, and each file that none of them writes."""
    differ = [path for path, text in expected(repo, parts).items() if not _holds(repo, path, text)]
    return differ + unwritten(repo, parts)


def write(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """Rewrite each file that does not hold what basis and the parts write, remove each that none of them writes, and
    return them."""
    changed: list[Path] = []
    for path, text in expected(repo, parts).items():
        if not _holds(repo, path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
            if _runs(repo, path):
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            changed.append(path)
    for path in unwritten(repo, parts):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        changed.append(path)
    return changed


def _holds(repo: Path, path: Path, text: str) -> bool:
    """Whether `path` holds `text`, byte for byte, and git would run it if it is a hook."""
    if not path.is_file() or _read(path) != text:
        return False
    return not _runs(repo, path) or os.name == "nt" or bool(path.stat().st_mode & stat.S_IXUSR)


def _runs(repo: Path, path: Path) -> bool:
    return path.relative_to(repo).as_posix() in EXECUTABLE


def _read(path: Path) -> str:
    """`path`'s text as it is, so a line ending other than LF is a difference."""
    return path.read_text(encoding="utf-8", newline="")


def _claude_settings(path: Path) -> str:
    """`.claude/settings.json` with every rule of `DENY`: as it is when it has them all, and otherwise with the missing
    ones added and every other key and rule kept."""
    if not path.exists():
        return _json({"$schema": SCHEMA, "permissions": {"deny": list(DENY)}})
    current = _read(path)
    try:
        data: object = json.loads(current)
    except json.JSONDecodeError as error:
        msg = f"{path} is not JSON, so basis cannot add its rules to it: {error}: fix it, or remove it, and run `uv run basis sync`"
        raise SyncError(msg) from error
    if not isinstance(data, dict) or not isinstance(permissions := data.get("permissions", {}), dict) or not isinstance(deny := permissions.get("deny", []), list):
        msg = f"{path} is not Claude Code's settings, an object whose permissions.deny is a list of rules: fix it, or remove it, and run `uv run basis sync`"
        raise SyncError(msg)
    if not (missing := [rule for rule in DENY if rule not in deny]):
        return current
    return _json({**data, "permissions": {**permissions, "deny": [*deny, *missing]}})


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


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
