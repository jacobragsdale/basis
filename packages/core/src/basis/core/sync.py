"""The files `basis sync` writes into a repo from the installed versions of basis and its parts, and removes once
none of them writes them. They are copies, not symlinks, since Git on Windows checks a symlink out as a text file by
default, and they are written with LF line endings and compared byte for byte, as `.gitattributes` checks them out.

basis owns, in a repo:

- the framework files, `REPO_FILES`: the Dockerfile, whose last lines are each part's `image` and whose apt packages
  include those `[tool.basis] apt-packages` names, the deploy pipeline, the commit and push hooks, and
  `.gitattributes`;
- each block of `AGENTS.md` whose begin marker says `sync` writes it: basis's own, `CORE`, and each part's; what is
  outside the blocks is the repo's own;
- `CLAUDE.md`, which holds only `@AGENTS.md`: Claude Code reads `AGENTS.md` only through it;
- each skill in `.agents/skills/` that carries `MARKER`, and its copy in `.claude/skills/`, the one skills folder
  Claude Code reads: `sync` writes the marker first in the front matter of each `SKILL.md`, in the `metadata` map that
  the Agent Skills spec keeps for a client's own data (`SKILL_KEY`), so that a skill is basis's by what it holds,
  whatever its name, and whichever part wrote it, installed or not;
- the rules `DENY` in `.claude/settings.json`, which keeps every other key and rule it holds;
- each file whose first lines carry `MARKER`;
- each path a part lists as `retired`: what an earlier version of it wrote before the marker existed, such as the
  `etl-*` skills etl-framework wrote, which a repo's first `basis sync` removes.

`CLAUDE.md` and `.claude/settings.json` carry no marker: the one holds a single line, and the other is JSON, which has
no comments. A file the repo already holds at one of these paths, which basis did not write, is the repo's own until
it is adopted: one without the marker, a skill's `SKILL.md` among them, a `CLAUDE.md` that holds anything else, or
settings that hold none of basis's rules. So a skill without the marker is basis's only when a part lists it as
`retired`; anywhere else it is the repo's own, removed never, and replaced only when adopted. `write` replaces it only when told to adopt it, after saving it beside itself as `NAME.before-basis`.

`plan` works out, once, what basis writes in a repo and what stops it: files the repo's own, and files basis cannot
read, each of which leaves every other file to be checked and written. `stale` lists what differs, whether to rewrite
or to remove, which `basis check` reports, and `write` makes the repo match, which `basis sync` does. Both take every
part the repo has at once, since what one part no longer writes may be another's."""

import json
import os
import re
import shutil
import stat
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from basis.core.errors import BasisError
from basis.core.hooks import FOLDER, HOOKS

MARKER = "managed by basis: `basis sync` rewrites it"
"""What a file basis writes carries in its first lines, in a comment of the file's own syntax, or for a skill, in its
front matter's `metadata`."""
SKILL_KEY = "basis"
"""The key, in a skill's front matter `metadata`, whose value is `MARKER`. Claude Code and the other agents leave
`metadata` alone, as the Agent Skills spec says clients do."""
CORE = "basis"
"""The name of basis's own block of `AGENTS.md`."""
TEMPLATES = Path(__file__).parent / "templates" / "sync"
REPO_FILES = {"Dockerfile": "Dockerfile", "azure-pipelines.yml": "azure-pipelines.yml", ".gitattributes": "gitattributes", **{f"{FOLDER}/{hook}": hook for hook in HOOKS}}
"""Each framework file, by its path in a repo, and its template in `TEMPLATES`, whose `{image}` the parts' lines
replace, and `{apt-packages}` the repo's. A template's name has no leading dot, so no tool takes it for this repo's
own file."""
EXECUTABLE = frozenset(f"{FOLDER}/{hook}" for hook in HOOKS)
"""The files git runs, which it skips unless they are executable: on Windows, Git runs a hook whatever its mode."""
SKILLS = Path(".agents") / "skills"
CLAUDE_SKILLS = Path(".claude") / "skills"
CLAUDE_SETTINGS = Path(".claude") / "settings.json"
CLAUDE_MD = "@AGENTS.md\n"
APT = "apt-packages"
"""The `[tool.basis]` key naming the Debian packages a repo's image installs beside basis's own, such as `libpq5`."""
BEFORE = ".before-basis"
"""What `write` adds to the name of a file of the repo's own that it adopts, to keep it for its owner to compare."""
DENY = (
    "Bash(git commit *--no-verify*)",
    "Bash(git push *--no-verify*)",
    "Bash(git * commit *--no-verify*)",
    "Bash(git * push *--no-verify*)",
    "Bash(git commit -n*)",
    "Bash(git commit -an*)",
    "Bash(git commit * -n)",
    "Bash(git commit * -n -*)",
    "Bash(git commit * -nm *)",
    "Bash(git commit * -an *)",
    "Bash(git commit * -anm *)",
)  # fmt: skip
"""Claude Code's rules against skipping the hooks. They refuse `--no-verify` anywhere in a commit or a push, after
`git` alone or after options such as `-C DIR`; and `-n`, a commit's short form of it: as the first option, alone or
bundled (`-n`, `-nm`, `-an`, `-anm`), as the last, before another option, and in the bundles `-nm`, `-an` and `-anm`
after other options. `-n` alone before a message is left alone, since a rule for it would refuse a commit whose
message holds ` -n `. A rule matches the command as an agent writes it, not every way of running git: the rules are
best effort, and the push hook, which checks every pushed commit, is the backstop."""
SCHEMA = "https://json.schemastore.org/claude-code-settings.json"
_APT_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+(=[A-Za-z0-9.+~:-]+)?$")
"""A Debian package's name, with an optional `=VERSION`: what may go in the image's `apt-get install`."""
_HEAD = 3
"""Lines a file's marker may be in: the first line can be one a format needs, such as a Dockerfile's `# syntax=`."""
_LINE = 512
"""The most of a line read for its marker, so a large file with no line breaks is never read whole."""
_UNREAD = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", "snapshots"}
"""Folders whose files basis never writes, so no marker is looked for in them."""
_BLOCK = re.compile(r"<!-- (?P<name>[\w.-]+):begin \(written by `[\w -]*sync`[^)]*\) -->.*?<!-- (?P=name):end -->(?P<tail>\n*)", re.DOTALL)

type Problem = tuple[str, str]
"""A reason and its fix, which `basis` prints as one `ERROR` line."""


@dataclass(frozen=True)
class AgentFiles:
    """What one part writes in a repo, as its installed version writes it: its agent files, and its lines of the
    repo's Dockerfile."""

    part: str
    """The part's distribution, which names its block, such as `basis-job`."""
    block: str
    """The part's block of `AGENTS.md`, without its markers."""
    skills: dict[str, str]
    """Each skill's `SKILL.md`, by its folder's name, which starts with the part's name and a dash, so that no two
    parts' skills share a folder; `sync` adds the marker to its front matter (`stamped`)."""
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


class SyncError(BasisError, ValueError):
    """A file basis writes, or writes part of, which it cannot work out what to write in."""

    def __init__(self, reason: str, fix: str) -> None:
        super().__init__(f"{reason}: {fix}")
        self.problem: Problem = (reason, fix)


@dataclass(frozen=True)
class Plan:
    """What basis writes in a repo, worked out once, and what stops it writing some of it."""

    repo: Path
    files: dict[Path, str]
    """Each file basis writes, and what it should hold: all but those in `unclaimed` and `unreadable`."""
    unclaimed: dict[Path, str]
    """Files the repo already holds at paths basis writes, which basis did not write, and what basis would write
    there."""
    unreadable: dict[Path, Problem]
    """Files basis cannot work out what to write in, each with why and the fix."""
    unwritten: list[Path]
    """What basis owns that nothing writes now."""

    def stale(self) -> list[Path]:
        """Each file that does not hold what basis writes, or a hook git would skip, and each that nothing writes."""
        return [path for path, text in self.files.items() if not _holds(self.repo, path, text)] + self.unwritten

    def problems(self) -> list[Problem]:
        """What stops basis writing some of its files: one line for the files of the repo's own, and one for each it
        cannot read, once however many files it stops."""
        return list(dict.fromkeys([*self.unreadable.values(), *_unclaimed(self.repo, list(self.unclaimed))]))


@dataclass(frozen=True)
class Synced:
    """What `write` did, and what it could not do."""

    repo: Path
    changed: list[Path] = field(default_factory=list[Path])
    """Each file written or removed."""
    adopted: list[Path] = field(default_factory=list[Path])
    """Each file of the repo's own that was replaced, after being saved beside itself as `NAME.before-basis`."""
    refused: list[Path] = field(default_factory=list[Path])
    """Each file of the repo's own left alone, since `write` was not told to adopt it."""
    unwritable: dict[Path, Problem] = field(default_factory=dict[Path, Problem])
    """Each file left alone because basis cannot read it, or cannot keep what it holds now."""

    def problems(self) -> list[Problem]:
        return list(dict.fromkeys([*self.unwritable.values(), *_unclaimed(self.repo, self.refused)]))


def core() -> AgentFiles:
    """basis's own block of `AGENTS.md`, which every repo has, whatever its parts."""
    return AgentFiles(CORE, (TEMPLATES / "agents.md").read_text(encoding="utf-8"), {})


def skills(folder: Path) -> dict[str, str]:
    """Each skill's `SKILL.md` in `folder`, by its folder's name."""
    return {child.name: (child / "SKILL.md").read_text(encoding="utf-8") for child in sorted(folder.iterdir()) if (child / "SKILL.md").exists()}


def plan(repo: Path, parts: Sequence[AgentFiles]) -> Plan:
    """What basis and `parts` write in `repo`, and what stops them."""
    agents = repo / "AGENTS.md"
    current = _read(agents).replace("\r\n", "\n") if agents.exists() else "# Agent guide\n\n"
    image = "".join(files.image for files in parts)
    made: dict[Path, Callable[[], tuple[str, bool]]] = {
        **{repo / path: _template(repo, repo / path, template, image) for path, template in REPO_FILES.items()},
        agents: lambda: (_agents(current, [*parts, core()]), True),
        repo / "CLAUDE.md": lambda: (CLAUDE_MD, not (repo / "CLAUDE.md").exists() or _read(repo / "CLAUDE.md").replace("\r\n", "\n") == CLAUDE_MD),
        repo / CLAUDE_SETTINGS: lambda: _claude_settings(repo / CLAUDE_SETTINGS),
    }
    for files in parts:
        for name, text in files.skills.items():
            for path in (repo / folder / name / "SKILL.md" for folder in (SKILLS, CLAUDE_SKILLS)):
                made[path] = lambda path=path, part=files.part, name=name, text=text: (_skill(part, name, text), _carries_marker(path))
    written: dict[Path, str] = {}
    unclaimed: dict[Path, str] = {}
    unreadable: dict[Path, Problem] = {}
    for path, make in made.items():
        try:
            text, claimed = make()
        except SyncError as error:
            unreadable[path] = error.problem
            continue
        (written if claimed or not path.exists() else unclaimed)[path] = text
    return Plan(repo, written, unclaimed, unreadable, _unwritten(repo, parts, set(made)))


def stale(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What `write` would change: each file that does not hold what basis and the parts write, or a hook git would
    skip, and each file that none of them writes. Files of the repo's own and files basis cannot read are not among
    them: `Plan.problems` says what they are."""
    return plan(repo, parts).stale()


def write(repo: Path, parts: Sequence[AgentFiles], *, adopt: bool = False) -> Synced:
    """Rewrite each file that does not hold what basis and the parts write, remove each that none of them writes, and
    with `adopt`, replace each file of the repo's own after saving it as `NAME.before-basis`."""
    found = plan(repo, parts)
    done = Synced(repo, unwritable=dict(found.unreadable))
    for path, text in found.files.items():
        if not _holds(repo, path, text):
            _put(repo, path, text)
            done.changed.append(path)
    for path, text in found.unclaimed.items():
        before = path.with_name(path.name + BEFORE)
        if not adopt:
            done.refused.append(path)
        elif before.exists():
            name = before.relative_to(repo).as_posix()
            done.unwritable[path] = (f"{name} is there from an earlier adoption", "compare it with what basis wrote, delete it, and run `uv run basis sync --adopt` again")
        else:
            path.rename(before)
            _put(repo, path, text)
            done.adopted.append(path)
            done.changed.append(path)
    for path in found.unwritten:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        done.changed.append(path)
    return done


def apt_packages(repo: Path) -> tuple[str, ...]:
    """The Debian packages the repo's `pyproject.toml` names as `[tool.basis] apt-packages`, beside basis's own."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    fix = f'give [tool.basis] {APT} = ["libpq5"], a list of Debian package names, each with an optional =VERSION, and run `uv run basis sync`'
    try:
        tool: object = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool")
    except tomllib.TOMLDecodeError as error:
        msg = f"{pyproject} is not TOML, so the Dockerfile's packages are unknown ({error})"
        raise SyncError(msg, "fix it") from error
    table = tool.get("basis") if isinstance(tool, dict) else None
    names = table.get(APT, []) if isinstance(table, dict) else []
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        msg = f"{pyproject}'s [tool.basis] {APT} is not a list of package names"
        raise SyncError(msg, fix)
    if bad := [str(name) for name in names if not _APT_PACKAGE.match(str(name))]:
        one = len(bad) == 1
        msg = f"{pyproject}'s [tool.basis] {APT} has {', '.join(map(repr, bad))}, which {'is no Debian package name' if one else 'are not Debian package names'}"
        raise SyncError(msg, fix)
    return tuple(str(name) for name in names)


def _template(repo: Path, path: Path, template: str, image: str) -> Callable[[], tuple[str, bool]]:
    """The text of the framework file at `path`, and whether the file the repo holds there, if any, is basis's: one
    that carries the marker."""

    def make() -> tuple[str, bool]:
        text = (TEMPLATES / template).read_text(encoding="utf-8").replace("{image}", image)
        if "{apt-packages}" in text:
            text = text.replace("{apt-packages}", "".join(f" {name}" for name in apt_packages(repo)))
        return text, _carries_marker(path)

    return make


def stamped(text: str) -> str:
    """A skill's `SKILL.md` as `sync` writes it: `text`, with `MARKER` first in its front matter, as the `metadata`
    entry `SKILL_KEY`, so that the marker is in the file's first lines. A `SKILL.md` without front matter, or with a
    `metadata` map of its own, is refused, with a ValueError."""
    if not text.startswith("---\n"):
        msg = "it has no front matter, from which an agent reads its name and description"
        raise ValueError(msg)
    if re.search(r"^metadata:", text.partition("\n---")[0], re.MULTILINE):
        msg = "it has a metadata map of its own, where basis writes its marker"
        raise ValueError(msg)
    return f'---\nmetadata:\n  {SKILL_KEY}: "{MARKER}"\n{text.removeprefix("---\n")}'


def _skill(part: str, name: str, text: str) -> str:
    try:
        return stamped(text)
    except ValueError as error:
        reason = f"{part}'s skill {name} cannot carry basis's marker: {error}"
        raise SyncError(reason, f"a defect in {part}: install it from a basis release, with `uv sync --reinstall`") from error


def _unwritten(repo: Path, parts: Sequence[AgentFiles], wanted: set[Path]) -> list[Path]:
    """What basis owns in `repo` that neither it nor any of `parts` writes now: none of `wanted`. A skill is its
    folder."""
    skills = {repo / SKILLS, repo / CLAUDE_SKILLS}
    found = {path.parent if path.name == "SKILL.md" and path.parent.parent in skills else path for path in _marked(repo) if path not in wanted}
    found |= {repo / path for files in parts for path in files.retired if (repo / path).exists()}
    return sorted(found)


def _unclaimed(repo: Path, paths: list[Path]) -> list[Problem]:
    if not paths:
        return []
    names = ", ".join(path.relative_to(repo).as_posix() for path in paths)
    one = len(paths) == 1
    return [
        (
            f"{names} {'is' if one else 'are'} the repo's own, at {'a path' if one else 'paths'} basis writes, so basis leaves {'it' if one else 'them'} alone",
            "run `uv run basis sync --adopt`, which saves each beside itself as NAME.before-basis, for you to compare and delete, and writes basis's",
        )
    ]


def _put(repo: Path, path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    if _runs(repo, path):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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


def _carries_marker(path: Path) -> bool:
    """Whether `path` is a file whose first lines carry `MARKER`, or no file at all."""
    if not path.exists():
        return True
    try:
        with path.open(encoding="utf-8") as opened:
            return any(MARKER in opened.readline(_LINE) for _ in range(_HEAD))
    except UnicodeDecodeError, OSError:  # a binary or unreadable file carries no marker
        return False


def _claude_settings(path: Path) -> tuple[str, bool]:
    """`.claude/settings.json` with every rule of `DENY` (as it is when it has them all, and otherwise with the missing
    ones added and every other key and rule kept), and whether the file there is one basis wrote: one that holds at
    least one of its rules."""
    if not path.exists():
        return _json({"$schema": SCHEMA, "permissions": {"deny": list(DENY)}}), True
    current = _read(path)
    fix = "fix it, or remove it, and run `uv run basis sync`"
    try:
        data: object = json.loads(current)
    except json.JSONDecodeError as error:
        msg = f"{path} is not JSON, so basis cannot add its rules to it ({error})"
        raise SyncError(msg, fix) from error
    if not isinstance(data, dict) or not isinstance(permissions := data.get("permissions", {}), dict) or not isinstance(deny := permissions.get("deny", []), list):
        msg = f"{path} is not Claude Code's settings, an object whose permissions.deny is a list of rules"
        raise SyncError(msg, fix)
    claimed = any(rule in deny for rule in DENY)
    if not (missing := [rule for rule in DENY if rule not in deny]):
        return current, claimed
    return _json({**data, "permissions": {**permissions, "deny": [*deny, *missing]}}), claimed


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


def _marked(repo: Path) -> Iterator[Path]:
    """Each file in `repo` whose first lines carry `MARKER`, outside the folders basis never writes in."""
    for folder, names, files in os.walk(repo):
        names[:] = sorted(name for name in names if name not in _UNREAD and not (Path(folder) / name / "pyvenv.cfg").exists())
        for name in sorted(files):
            path = Path(folder) / name
            if path.exists() and _carries_marker(path):
                yield path
