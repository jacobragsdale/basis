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
- each file a part makes from the repo's own code, its `made`, such as an API's `openapi.json`, which carries `MARKER`
  in its first lines too: a JSON file as its first key, `"x-basis"`;
- each path a part lists as `retired`: what an earlier version of it wrote before the marker existed, such as the
  `etl-*` skills etl-framework wrote, which a repo's first `basis sync` removes.

`CLAUDE.md` and `.claude/settings.json` carry no marker: the one holds a single line, and the other is JSON, which has
no comments. A file the repo already holds at one of these paths, which basis did not write, is the repo's own until
it is adopted: one without the marker, a skill's `SKILL.md` among them, a `CLAUDE.md` that holds anything else, or
settings that hold none of basis's rules. So a skill without the marker is basis's only when a part lists it as
`retired`; anywhere else it is the repo's own, removed never, and replaced only when adopted. `write` replaces it only when told to adopt it, after saving it beside itself as `NAME.before-basis`.

`plan` works out, once, what basis writes in a repo and what stops it: files the repo's own, files basis cannot read,
and files a part cannot make, each of which leaves every other file to be checked and written. A part makes a file
only at a path relative to the repo, outside the folders basis never reads, that neither basis nor another part
writes. `stale` lists what differs, whether to rewrite or to remove, which `basis check` reports, and `write` makes
the repo match, which `basis sync` does. Both take every part the repo has at once, since what one part no longer
writes may be another's."""

import json
import logging
import os
import re
import shutil
import stat
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath

from basis.core import guard, tools
from basis.core.errors import BasisError
from basis.core.hooks import FOLDER, HOOKS
from basis.core.project import ProjectError, project
from basis.core.tools import Tools

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
AGENT_HOOKS: dict[Path, guard.Agent] = {Path(".cursor") / "hooks.json": "cursor", Path(".codex") / "hooks.json": "codex"}
"""Each agent's project hooks file, beside Claude Code's, which `.claude/settings.json` holds (`basis.core.guard`)."""
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
UNREAD = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".hypothesis", "mutants", "snapshots"})
"""The folders basis never reads, in a repo it walks: version control, environments, caches, mutation testing's copy and
the run store's local snapshots. sync, the gates, mutation testing and the layers share this one list."""
_UNREAD = UNREAD
"""Folders whose files basis never writes, so no marker is looked for in them."""
_BLOCK = re.compile(r"<!-- (?P<name>[\w.-]+):begin \(written by `[\w -]*sync`[^)]*\) -->.*?<!-- (?P=name):end -->(?P<tail>\n*)", re.DOTALL)

log = logging.getLogger(__name__)

type Problem = tuple[str, str]
"""A reason and its fix, which `basis` prints as one `ERROR` line."""


@dataclass(frozen=True)
class AgentFiles:
    """What one part writes in a repo, as its installed version writes it: its agent files, its lines of the repo's
    Dockerfile, and the files it makes from the repo's code."""

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
    tools: Tools | None = None
    """The part's share of the repo's ruff, ty and pytest settings, which `sync` writes where every declared part
    gives one (`basis.core.tools`); None leaves a repo of the part its own settings."""
    made: Mapping[str, Callable[[], str]] = field(default_factory=dict[str, Callable[[], str]])
    """Each file the part makes from the repo's own code, such as an API's `openapi.json`, by its path in the repo,
    `/`-separated, and the function that makes its text, whose first lines carry `MARKER`. Only `plan` calls it, once
    per call; a `SyncError` it raises is that file's one problem, and every other file is still checked and written."""

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
        super().__init__(reason, fix)


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
    refusals: tuple[Problem, ...] = ()
    """Each file a part would make that basis refuses to write, with why: a path no part may make."""

    def stale(self) -> list[Path]:
        """Each file that does not hold what basis writes, or a hook git would skip, and each that nothing writes."""
        return [path for path, text in self.files.items() if not _holds(self.repo, path, text)] + self.unwritten

    def problems(self) -> list[Problem]:
        """What stops basis writing some of its files: one line for the files of the repo's own, and one for each it
        cannot read, once however many files it stops."""
        return list(dict.fromkeys([*self.refusals, *self.unreadable.values(), *_unclaimed(self.repo, list(self.unclaimed))]))


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
    refusals: tuple[Problem, ...] = ()
    """Each file a part would make that basis refuses to write: `Plan.refusals`."""

    def problems(self) -> list[Problem]:
        return list(dict.fromkeys([*self.refusals, *self.unwritable.values(), *_unclaimed(self.repo, self.refused)]))


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
        **{repo / path: partial(_agent_hooks, repo / path, agent) for path, agent in AGENT_HOOKS.items()},
    }
    if tools.owned([files.tools for files in parts]):
        made.update(_tool_files(repo, [files.tools for files in parts if files.tools is not None]))
    for files in parts:
        for name, text in files.skills.items():
            for path in (repo / folder / name / "SKILL.md" for folder in (SKILLS, CLAUDE_SKILLS)):
                made[path] = lambda path=path, part=files.part, name=name, text=text: (_skill(part, name, text), _carries_marker(path))
    from_code, refused = _from_code(repo, parts, set(made))
    made.update(from_code)
    written: dict[Path, str] = {}
    unclaimed: dict[Path, str] = {}
    unreadable: dict[Path, Problem] = {}
    for path, make in made.items():
        try:
            text, claimed = make()
        except SyncError as error:
            unreadable[path] = error.problem
            continue
        except Exception as error:  # a defect of whatever makes the file: its line, after its traceback, and every other file is still made
            name = path.relative_to(repo).as_posix()
            log.exception("could not make %s", name)
            unreadable[path] = (f"{name} could not be made, {type(error).__name__}: {' '.join(str(error).split())}", "a defect; the log record above holds its traceback")
            continue
        (written if claimed or not path.exists() else unclaimed)[path] = text
    kept = set(made) | {path for path, _ in refused}  # a file there that basis refuses to write is still not removed
    return Plan(repo, written, unclaimed, unreadable, _unwritten(repo, parts, kept), tuple(problem for _, problem in refused))


def stale(repo: Path, parts: Sequence[AgentFiles]) -> list[Path]:
    """What `write` would change: each file that does not hold what basis and the parts write, or a hook git would
    skip, and each file that none of them writes. Files of the repo's own and files basis cannot read are not among
    them: `Plan.problems` says what they are."""
    return plan(repo, parts).stale()


def write(repo: Path, parts: Sequence[AgentFiles], *, adopt: bool = False) -> Synced:
    """Rewrite each file that does not hold what basis and the parts write, remove each that none of them writes, and
    with `adopt`, replace each file of the repo's own after saving it as `NAME.before-basis`."""
    found = plan(repo, parts)
    done = Synced(repo, unwritable=dict(found.unreadable), refusals=found.refusals)
    for path, text in found.files.items():
        recased = _recase(path)
        if recased or not _holds(repo, path, text):
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
        read = project(pyproject)
    except ProjectError as error:
        msg = f"{pyproject} is not TOML, so the Dockerfile's packages are unknown ({error.toml})"
        raise SyncError(msg, "fix it") from error
    names = read.apt_packages if read is not None and read.apt_packages is not None else []
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


def _tool_files(repo: Path, shares: Sequence[Tools]) -> dict[Path, Callable[[], tuple[str, bool]]]:
    """`ruff.toml`, `ty.toml` and `pytest.ini`, from basis's settings, the parts' shares and the repo's exceptions; a
    file of the repo's own at one of their paths is left alone, as any is."""

    def made(name: str) -> Callable[[], tuple[str, bool]]:
        def make() -> tuple[str, bool]:
            try:
                found = tools.exceptions(repo / "pyproject.toml") if (repo / "pyproject.toml").is_file() else []
            except tools.ToolsError as error:
                raise SyncError(str(error).rpartition(": ")[0], str(error).rpartition(": ")[2]) from error
            text = {"ruff.toml": tools.ruff_toml(MARKER, shares, found), "ty.toml": tools.ty_toml(MARKER, found), "pytest.ini": tools.pytest_ini(MARKER, shares)}[name]
            return text, _carries_marker(repo / name)

        return make

    return {repo / name: made(name) for name in tools.FILES}


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


def _from_code(repo: Path, parts: Sequence[AgentFiles], taken: set[Path]) -> tuple[dict[Path, Callable[[], tuple[str, bool]]], list[tuple[Path, Problem]]]:
    """What each part makes from the repo's code, by its path in `repo`, and each file no part may make, with why: a
    path outside the repo or in a folder basis never reads, one basis writes itself, which `taken` holds, or one that
    two parts make. Paths are compared ignoring case, as Windows and macOS compare them. A file refused for two reasons
    is two problems."""
    makers: dict[str, list[tuple[str, str, Callable[[], str]]]] = defaultdict(list)
    refused: list[tuple[Path, Problem]] = []
    own = {path.relative_to(repo).as_posix().casefold() for path in taken}
    for files in parts:
        for given, make in files.made.items():
            relative, wrong = _relative(repo, given)
            if wrong is not None:
                refused.append((repo / given, (f"the {files.part} part makes {given!r}, {wrong}", _DEFECT)))
            elif relative.casefold() in own:
                refused.append((repo / relative, (f"the {files.part} part makes {relative}, which basis writes itself", _DEFECT)))
            else:
                makers[relative.casefold()].append((relative, files.part, make))
    making: dict[Path, Callable[[], tuple[str, bool]]] = {}
    for found in makers.values():
        relative = found[0][0]
        if len(found) > 1:
            refused.append((repo / relative, (f"the {' and '.join(part for _, part, _ in found)} parts each make {relative}", _DEFECT)))
        else:
            making[repo / relative] = _making(repo / relative, found[0][1], found[0][2])
    return making, refused


_DEFECT = "a defect in basis: install every basis package from one release, with `uv sync --reinstall`"


def _relative(repo: Path, given: str) -> tuple[str, str | None]:
    """`given` as the `/`-separated path relative to `repo` that it names, or why no part may make it."""
    posix = PurePosixPath(given)
    if not posix.parts or "\\" in given or posix.is_absolute() or PureWindowsPath(given).drive:
        return given, "which is not a file's path relative to the repo"
    if ".." in posix.parts:
        return given, "which leads out of the repo"
    folder = repo
    for name in posix.parts[:-1]:
        if _unread(folder, name):
            return given, "which is in a folder basis never writes in"
        folder /= name
    return posix.as_posix(), None


def _making(path: Path, part: str, make: Callable[[], str]) -> Callable[[], tuple[str, bool]]:
    """`make`, for `plan`: the text `part` makes at `path`, which must carry the marker, and whether the file there, if
    any, is basis's."""

    def made() -> tuple[str, bool]:
        text = make()
        if not any(MARKER in line[:_LINE] for line in text.splitlines()[:_HEAD]):
            msg = f"the {part} part made {path.name} without basis's marker in its first lines"
            raise SyncError(msg, _DEFECT)
        return text, _carries_marker(path)

    return made


def _unwritten(repo: Path, parts: Sequence[AgentFiles], wanted: set[Path]) -> list[Path]:
    """What basis owns in `repo` that neither it nor any of `parts` writes now: none of `wanted`, nor a file the
    filesystem holds as one of them, as a case-insensitive one holds `OpenAPI.json` as `openapi.json`. A skill is its
    folder. A part's `retired` path is removed only where it holds nothing basis writes now."""
    folded: dict[str, list[Path]] = defaultdict(list)
    for path in wanted:
        folded[str(path).casefold()].append(path)

    def kept(path: Path) -> bool:
        return path in wanted or any(other.exists() and path.exists() and path.samefile(other) for other in folded.get(str(path).casefold(), []))

    def holds_wanted(path: Path) -> bool:
        prefix = str(path).casefold() + os.sep
        return kept(path) or any(key.startswith(prefix) for key in folded)

    skills = {repo / SKILLS, repo / CLAUDE_SKILLS}
    found = {path.parent if path.name == "SKILL.md" and path.parent.parent in skills else path for path in _marked(repo) if not kept(path)}
    found |= {repo / path for files in parts for path in files.retired if (repo / path).exists() and not holds_wanted(repo / path)}
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


def _recase(path: Path) -> bool:
    """Rename the file a case-insensitive filesystem, as Windows' is, holds under another case of `path`'s name, so
    that it, and git, name it as basis writes it; whether one was."""
    if not path.exists():
        return False
    other = next((child for child in path.parent.iterdir() if child.name != path.name and child.name.casefold() == path.name.casefold() and child.samefile(path)), None)
    if other is None:
        return False
    other.rename(path)  # a no-op where both names are links to the one file, as on a case-sensitive filesystem
    return other.name not in {child.name for child in path.parent.iterdir()}


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
    wanted = guard.hooks("claude")
    if not path.exists():
        return _json({"$schema": SCHEMA, "permissions": {"deny": list(DENY)}, "hooks": wanted}), True
    current = _read(path)
    data = _json_object(path, current, "Claude Code's settings, an object whose permissions.deny is a list of rules")
    if not isinstance(permissions := data.get("permissions", {}), dict) or not isinstance(deny := permissions.get("deny", []), list):
        msg = f"{path} is not Claude Code's settings, an object whose permissions.deny is a list of rules"
        raise SyncError(msg, "fix it, or remove it, and run `uv run basis sync`")
    claimed = any(rule in deny for rule in DENY) or guard.ours(data.get("hooks"))
    hooks_now = _merged_hooks(path, data.get("hooks", {}), wanted)
    missing = [rule for rule in DENY if rule not in deny]
    if not missing and hooks_now == data.get("hooks"):
        return current, claimed
    return _json({**data, "permissions": {**permissions, "deny": [*deny, *missing]}, "hooks": hooks_now}), claimed


def _agent_hooks(path: Path, agent: guard.Agent) -> tuple[str, bool]:
    """An agent's hooks file with basis's hooks (`basis.core.guard`), as it is when it has them, and otherwise with them
    merged in, every other key and hook kept; always basis's to write, since nothing of the repo's is lost."""
    wanted = guard.hooks(agent)
    shape: dict[str, object] = {"version": 1} if agent == "cursor" else {}
    if not path.exists():
        return _json({**shape, "hooks": wanted}), True
    current = _read(path)
    data = _json_object(path, current, f"{agent}'s hooks, a JSON object")
    hooks_now = _merged_hooks(path, data.get("hooks", {}), wanted)
    if hooks_now == data.get("hooks") and all(key in data for key in shape):
        return current, True
    return _json({**shape, **data, "hooks": hooks_now}), True


def _json_object(path: Path, text: str, shape: str) -> dict[str, object]:
    fix = "fix it, or remove it, and run `uv run basis sync`"
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as error:
        msg = f"{path} is not JSON, so basis cannot add its rules and hooks to it ({error})"
        raise SyncError(msg, fix) from error
    if not isinstance(data, dict):
        msg = f"{path} is not {shape}"
        raise SyncError(msg, fix)
    return {str(key): value for key, value in data.items()}


def _merged_hooks(path: Path, given: object, wanted: Mapping[str, list[dict[str, object]]]) -> dict[str, object]:
    """`given`, an agent's hooks by event, with basis's own as `wanted` gives them, in place of any earlier version of
    them, after the repo's own."""
    if not isinstance(given, dict) or not all(isinstance(entries, list) for entries in given.values()):
        msg = f"{path}'s hooks are not a list of hooks for each event"
        raise SyncError(msg, "fix it, or remove it, and run `uv run basis sync`")
    merged: dict[str, list[object]] = {str(event): [entry for entry in entries if not guard.ours(entry)] for event, entries in given.items() if isinstance(entries, list)}
    for event, entries in wanted.items():
        merged[event] = [*merged.get(event, []), *entries]
    return {event: entries for event, entries in merged.items() if entries}


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


def walk(repo: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    """The repo's folders, as `os.walk` gives them, in order, and never one basis does not read: one of `UNREAD`, a
    virtual environment whatever its name, or another checkout, a folder that holds a `.git` of its own, as a
    worktree an agent works in under `.claude/worktrees/` does, whose files are that checkout's and not the repo's.
    Sync, the gates and mutation testing all walk the repo by it; a caller may prune `names` further in place."""
    for folder, names, files in os.walk(repo):
        here = Path(folder)
        names[:] = sorted(name for name in names if not _unread(here, name))
        yield here, names, sorted(files)


def _marked(repo: Path) -> Iterator[Path]:
    """Each file in `repo` whose first lines carry `MARKER`, outside the folders basis never writes in."""
    for folder, _, files in walk(repo):
        for name in files:
            path = folder / name
            if path.exists() and _carries_marker(path):
                yield path


def _unread(folder: Path, name: str) -> bool:
    """Whether the folder `name` in `folder` is one basis never reads or writes in, nor looks for its marker in: one of
    `UNREAD`, a virtual environment, whatever its name, or another checkout."""
    return name in _UNREAD or (folder / name / "pyvenv.cfg").exists() or (folder / name / ".git").exists()
