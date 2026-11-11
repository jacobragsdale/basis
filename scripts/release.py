# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""basis's releases. The workspace has one version, the root pyproject.toml's `[project] version`: every member
carries it, and each depends on basis-core at exactly that version, so a repo's lock can only hold basis packages from
one release. A release publishes the members that say so in their own pyproject.toml, as `[tool.basis-release]
publish = true`; every member says true or false, and one that is not published is still built and checked at that
version. docs/releasing.md says how a release goes, and .github/workflows/release.yml runs these:

    uv run scripts/release.py check                   # refuse a member, a pin or the lock at another version
    uv run scripts/release.py check --tag v0.1.0      # and a tag that names another version, or none
    uv run scripts/release.py version 0.1.0           # set the version everywhere, relock, and write the sample's release
    uv run scripts/release.py packages                # the members a release publishes, a name per line
    uv run scripts/release.py gate COMMIT --repository OWNER/REPO   # refuse a commit off main, or one CI did not pass
    uv run scripts/release.py feed dist --port 8761   # dist/ as an index that proxies PyPI, as the firm's feed does
"""

import argparse
import functools
import hashlib
import html
import http.server
import io
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO, assert_never, override
from urllib.parse import urlsplit

import shared

ROOT = Path(__file__).resolve().parent.parent
CORE = "basis-core"
TOOL = "basis-release"
"""The `[tool.TOOL]` table each member declares `publish` in. Not `[tool.basis]`, whose unknown keys `basis` refuses."""
UNRELEASED = "0.0.0"
"""The version basis carries before its first release, which no tag names, as `basis.core.new.UNRELEASED` says."""
CI = "ci.yml"
"""The workflow whose run on a commit must pass before a tag of it releases."""
PYPI = "https://pypi.org/simple"
SAMPLE = Path("packages/api/src/basis/api/example/openapi.json")
"""The example API's document, which basis-ui pins; `version` writes the release's copy of it beside it, which the
example's contract test compares the sample with (basis.api.contract)."""
RELEASED = SAMPLE.with_name("openapi.released.json")
RELEASE_KEY = "x-basis-release"
"""The released document's first key, in place of basis's marker, as basis-api's `basis release-document` writes it."""
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
"""A PEP 508 requirement's name, at its start."""
_DISTRIBUTION = re.compile(r"^(?P<name>[^-/]+)-[^-/]+(?:\.tar\.gz|-[^/]+\.whl)$")
"""A wheel's or an sdist's file name, whose name part has `_` for each run of `-`, `_` and `.` (PEP 625, PEP 427)."""
_HEADER = re.compile(r"^\s*\[(?!\[)(?P<name>[^\]]*)\]\s*(?:#.*)?$")
"""A TOML table's header, with an optional comment after it; not an array of tables' `[[NAME]]`."""
_KEY = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=")
"""A bare key's line."""


class ReleaseError(Exception):
    """What this script cannot do as asked, such as read a file it needs; its message ends with the fix."""


@dataclass(frozen=True)
class Problem:
    reason: str
    fix: str


@dataclass(frozen=True)
class Member:
    """A workspace member, as its pyproject.toml declares it."""

    name: str
    """Normalized (PEP 503), as uv.lock spells it."""
    folder: str
    """Its folder, relative to the root, as uv's `members` globs find it."""
    version: str
    dependencies: tuple[str, ...]
    """`[project] dependencies`, as written."""
    publish: bool | None
    """`[tool.basis-release] publish`; None when it says nothing."""


@dataclass(frozen=True)
class Workspace:
    version: str
    """The root pyproject.toml's `[project] version`: the one place it is set."""
    dependencies: tuple[str, ...]
    """The root pyproject.toml's `[project] dependencies`: what a plain `uv sync` installs, which is what basis releases."""
    members: tuple[Member, ...]
    locked: Mapping[str, str]
    """Each package uv.lock locks, by its normalized name, with its version."""

    def published(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members if member.publish is True)


def problems(workspace: Workspace, *, tag: str | None = None) -> list[Problem]:
    """Each way the workspace is not one version, each member depending on basis-core at exactly it, every member
    saying whether a release publishes it, a published one needing only published ones, and the root's dependencies
    naming exactly the published ones; and `tag`, when given, naming another version, or the unreleased one."""
    version = workspace.version
    if tag == f"v{UNRELEASED}":
        fix = "set the release's version first, as `uv run scripts/release.py version minor` does, merge that, and tag the merge"
        return [Problem(f"the tag {tag} names no release: {UNRELEASED} is the version basis carries before its first", fix)]
    if not _VERSION.match(version):
        return [Problem(f"the root pyproject.toml's version, {version!r}, is not MAJOR.MINOR.PATCH", "set one with `uv run scripts/release.py version X.Y.Z`")]
    found = [problem] if (problem := _past_zero(version)) is not None else []
    if CORE not in {member.name for member in workspace.members}:
        found.append(Problem(f"no member of the workspace is {CORE}, which every other member depends on", "run this in a checkout of basis"))
    for member in workspace.members:
        found.extend(_member_problems(member, version, workspace.locked.get(member.name)))
    found.extend(_publishing_problems(workspace))
    if tag is not None and tag != f"v{version}":
        fix = f"tag the commit whose version it names, or set the version to the tag's, as `uv run scripts/release.py version {tag.removeprefix('v')}` does, in a commit of its own"
        found.append(Problem(f"the tag {tag} does not name the workspace's version, {version}", fix))
    return found


def _member_problems(member: Member, version: str, locked: str | None) -> list[Problem]:
    where = f"{member.folder}/pyproject.toml"
    setting = f"run `uv run scripts/release.py version {version}`, which sets every member's version and pin, and relocks"
    found: list[Problem] = []
    if member.version != version:
        found.append(Problem(f"{where} gives {member.name} version {member.version}, and the root pyproject.toml gives {version}", setting))
    pins = [dependency for dependency in member.dependencies if name_of(dependency) == CORE]
    if member.name != CORE and pins != [f"{CORE}=={version}"]:
        said = ", ".join(f"`{pin}`" for pin in pins) if pins else "nothing"
        found.append(Problem(f"{where} pins {CORE} as {said}, and every member depends on it at exactly the workspace's version, `{CORE}=={version}`", setting))
    if member.publish is None:
        found.append(Problem(f"{where} does not say whether a release publishes {member.name}", f"declare `[tool.{TOOL}] publish = true`, or false"))
    if locked != version:
        found.append(Problem(f"uv.lock holds {member.name} at {locked or 'no version'}, and the workspace is at {version}", "run `uv lock`, and commit uv.lock"))
    return found


def _publishing_problems(workspace: Workspace) -> list[Problem]:
    """A release that publishes nothing, a published member that needs one no release publishes, and root
    dependencies, which a plain `uv sync` installs, that are not exactly the published members."""
    names = {member.name for member in workspace.members}
    published = {member.name for member in workspace.published()}
    found: list[Problem] = []
    if not published:
        found.append(Problem("no member says that a release publishes it", f"declare `[tool.{TOOL}] publish = true` in each member a release publishes"))
    for member in workspace.published():
        if needed := sorted({name_of(dependency) for dependency in member.dependencies} & (names - published)):
            listed = ", ".join(needed)
            reason = f"a release publishes {member.name}, which depends on {listed}, which it does not publish, so an index holding {member.name} could not install it"
            found.append(Problem(reason, f"publish {listed} too, or not {member.name}"))
    released = {name_of(dependency) for dependency in workspace.dependencies} & names
    for member in workspace.members:
        if member.publish is False and member.name in released:
            reason = f"the root pyproject.toml's dependencies, which are what basis releases, list {member.name}, which says a release does not publish it"
            found.append(Problem(reason, f"take it out of them, or make its `[tool.{TOOL}] publish` true"))
        if member.publish is True and member.name not in released:
            reason = f"{member.name} says a release publishes it, and the root pyproject.toml's dependencies, which are what basis releases, do not list it"
            found.append(Problem(reason, f"add {member.name} to them, or make its `[tool.{TOOL}] publish` false"))
    return found


def _past_zero(version: str) -> Problem | None:
    if version.startswith("0."):
        return None
    fix = "keep 0.x until one does; the generators' templates already take `<MAJOR+1` from 1.0 (basis.core.new.upper_bound)"
    return Problem(f"the version is {version}, and basis stays at 0.x until a real repo runs it in production", fix)


def name_of(requirement: str) -> str:
    """A PEP 508 requirement's name, normalized (PEP 503); "" for one that does not start with a name."""
    found = _NAME.match(requirement)
    return shared.normalized(found.group(1)) if found is not None else ""


def next_version(current: str, asked: str) -> str:
    """`asked`, or for `patch` and `minor` the next such version after `current`."""
    if asked in {"patch", "minor"}:
        if (parts := _VERSION.match(current)) is None:
            msg = f"the root pyproject.toml's version, {current!r}, is not MAJOR.MINOR.PATCH, so it has no next {asked}: give the version itself, as X.Y.Z"
            raise ReleaseError(msg)
        major, minor, patch = (int(part) for part in parts.groups())
        return f"{major}.{minor}.{patch + 1}" if asked == "patch" else f"{major}.{minor + 1}.0"
    if not _VERSION.match(asked):
        msg = f"{asked!r} is no version: give one as X.Y.Z, such as 0.1.0, or `patch` or `minor`"
        raise ReleaseError(msg)
    return asked


def with_version(text: str, version: str, *, pin: bool, where: str = "pyproject.toml") -> str:
    """A pyproject.toml's `text`, with its `[project] version` set to `version`, and with `pin`, each basis-core
    requirement in its `[project] dependencies` made `basis-core==VERSION`. Everything else, comments included, stays,
    and the result must parse as exactly that change, or nothing is returned."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        msg = f"{where} cannot be read as TOML ({error}): fix what it says"
        raise ReleaseError(msg) from error
    lines = text.splitlines(keepends=True)
    starts = _top_level(lines)
    start, end = _project(lines, starts, where)
    keys = {match["key"]: number for number in range(start + 1, end) if starts[number] and (match := _KEY.match(lines[number])) is not None}
    if "version" not in keys:
        msg = f'{where}\'s [project] table gives no version: give it one, as version = "X.Y.Z"'
        raise ReleaseError(msg)
    edited = list(lines)
    edited[keys["version"]] = re.sub(r"""^(\s*version\s*=\s*)(?:"[^"\n]*"|'[^'\n]*')""", rf'\g<1>"{version}"', lines[keys["version"]], count=1)
    if pin and "dependencies" in keys:  # the array runs to the next line that starts outside it
        first = keys["dependencies"]
        last = next((number for number in range(first + 1, len(lines)) if starts[number]), len(lines))
        array = re.sub(rf'(?m)(^[ \t]*|\[[ \t]*|,[ \t]*)"{CORE}(?=[^A-Za-z0-9._-])[^"]*"', rf'\1"{CORE}=={version}"', "".join(lines[first:last]))
        edited[first:last] = [array]
    result = "".join(edited)
    try:
        parsed: object = tomllib.loads(result)
    except tomllib.TOMLDecodeError:
        parsed = None  # an edit that broke the file, refused below as any other edit that is not exactly the change
    if parsed != _expected(before, version, pin=pin):
        msg = f"{where}'s [project] version or basis-core requirement is written in a way this script does not edit: set it to {version} by hand"
        raise ReleaseError(msg)
    return result


def _top_level(lines: Sequence[str]) -> list[bool]:
    """For each line of a TOML document, whether it starts outside every array, inline table and multi-line string,
    where a table's header or a key can start."""
    starts: list[bool] = []
    depth, string = 0, ""
    for line in lines:
        starts.append(depth == 0 and not string)
        index = 0
        while index < len(line):
            if string:
                if string in {'"', '"""'} and line[index] == "\\":
                    index += 2
                elif line.startswith(string, index):
                    index, string = index + len(string), ""
                else:
                    index += 1
            elif line[index] == "#":
                break
            elif (opening := line[index : index + 3]) in {'"""', "'''"} or (opening := line[index]) in {'"', "'"}:
                index, string = index + len(opening), opening
            else:
                depth += {"[": 1, "{": 1, "]": -1, "}": -1}.get(line[index], 0)
                index += 1
        if string in {'"', "'"}:
            string = ""  # a one-line string ends with its line
    return starts


def _project(lines: Sequence[str], starts: Sequence[bool], where: str) -> tuple[int, int]:
    """The first and the end line of the `[project]` table."""
    headers = [number for number, line in enumerate(lines) if starts[number] and line.lstrip().startswith("[")]
    start = next((number for number in headers if (match := _HEADER.match(lines[number])) is not None and match["name"].strip().strip('"') == "project"), None)
    if start is None:
        msg = f"{where} has no [project] table: give each member one, as uv needs"
        raise ReleaseError(msg)
    return start, next((number for number in headers if number > start), len(lines))


def _expected(document: Mapping[str, object], version: str, *, pin: bool) -> dict[str, object]:
    project = _table(document, "project")
    project["version"] = version
    if pin and isinstance(dependencies := project.get("dependencies"), list):
        project["dependencies"] = [f"{CORE}=={version}" if isinstance(item, str) and name_of(item) == CORE else item for item in dependencies]
    return {**document, "project": project}


def read(root: Path) -> Workspace:
    """The workspace at `root`: its version, each member its `members` globs find, and what uv.lock locks."""
    pyproject = _toml(root / "pyproject.toml")
    project = _table(pyproject, "project")
    version = _string(project, "version", root / "pyproject.toml")
    workspace = _table(_table(_table(pyproject, "tool"), "uv"), "workspace")
    excluded = {path for pattern in _strings(workspace, "exclude") for path in root.glob(pattern)}
    folders = sorted({path.parent for pattern in _strings(workspace, "members") for path in root.glob(f"{pattern}/pyproject.toml")} - excluded)
    members = tuple(_member(root, folder) for folder in folders)
    lockfile = root / "uv.lock"
    entries = _toml(lockfile).get("package", []) if lockfile.exists() else []
    locked = {
        shared.normalized(name): number  # a walrus in a comprehension binds in the function, so not `version`
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and isinstance(name := entry.get("name"), str) and isinstance(number := entry.get("version"), str)
    }
    return Workspace(version, _strings(project, "dependencies"), members, locked)


def set_version(root: Path, asked: str, *, lock: Callable[[Path], int]) -> list[Problem]:
    """Set the workspace's version, every member's and each one's basis-core pin, relock with `lock`, and say what
    still disagrees. Every new text is worked out before any file is written, and when the relock fails, every file,
    uv.lock among them, goes back to what it was, so the workspace is never left at two versions."""
    workspace = read(root)
    version = next_version(workspace.version, asked)
    if (problem := _past_zero(version)) is not None:
        return [problem]
    edits = [(root / "pyproject.toml", False), *((root / member.folder / "pyproject.toml", True) for member in workspace.members)]
    lockfile = root / "uv.lock"
    before = {path: path.read_bytes() for path in [*(path for path, _ in edits), lockfile] if path.exists()}
    after = {path: with_version(before[path].decode("utf-8"), version, pin=pin, where=path.relative_to(root).as_posix()).encode("utf-8") for path, pin in edits}
    if (root / SAMPLE).is_file():
        released = root / RELEASED
        if released.exists():
            before[released] = released.read_bytes()
        after[released] = released_text((root / SAMPLE).read_text(encoding="utf-8"), f"v{version}").encode("utf-8")
    try:
        for path, text in after.items():
            path.write_bytes(text)
        relocked = lock(root) == 0
    except BaseException:
        _restore(before, lockfile)
        raise
    if not relocked:
        _restore(before, lockfile)
        for path in set(after) - set(before):
            path.unlink(missing_ok=True)
        fix = f"fix what it says, and set the version again, as `uv run scripts/release.py version {asked}` does"
        return [Problem("uv could not relock the workspace, as it says above, so every file is as it was", fix)]
    return problems(read(root))


def released_text(sample: str, tag: str) -> str:
    """The sample document `sample` as the release `tag` gives it, as basis-api's `contract.released_text` writes one."""
    try:
        document = json.loads(sample)
    except json.JSONDecodeError as error:
        msg = f"{SAMPLE} is not JSON ({error}): write it again, as `uv run python -m basis.api.example` does"
        raise ReleaseError(msg) from None
    given = {key: value for key, value in document.items() if key != "x-basis"}
    return json.dumps({RELEASE_KEY: tag, **given}, indent=2, ensure_ascii=False) + "\n"


def released_problems(root: Path, tag: str) -> list[Problem]:
    """That the sample's released document, where the workspace has the sample, is the release `tag`'s."""
    if not (root / SAMPLE).is_file():
        return []
    fix = f"set the version with `uv run scripts/release.py version {tag.removeprefix('v')}`, which writes it, in the release's commit"
    try:
        named = json.loads((root / RELEASED).read_text(encoding="utf-8")).get(RELEASE_KEY)
    except OSError, json.JSONDecodeError, AttributeError:
        named = None
    if named != tag:
        return [Problem(f"{RELEASED.as_posix()} is release {named}'s, not {tag}'s" if named is not None else f"{RELEASED.as_posix()} names no release", fix)]
    return []


def _restore(before: Mapping[Path, bytes], lockfile: Path) -> None:
    for path, text in before.items():
        path.write_bytes(text)
    if lockfile not in before:
        lockfile.unlink(missing_ok=True)


def write_surface(root: Path, version: str) -> int:
    """Record the release's surface and what it stores, as `scripts/surface.py write VERSION` does, from the
    workspace's code at its new version: what every later release is held to (`scripts/tests/test_surface.py`). A
    workspace without the script records nothing."""
    if not (root / "scripts" / "surface.py").is_file():
        return 0
    uv = shutil.which("uv")
    if uv is None:
        msg = "uv is not on the PATH, and the surface is recorded with it: install uv, as https://docs.astral.sh/uv/ says"
        raise ReleaseError(msg)
    return subprocess.run([uv, "run", "python", "scripts/surface.py", "write", version], cwd=root, env=shared.environment(), check=False).returncode


def _recorded(root: Path, surface: Callable[[Path, str], int]) -> list[Problem]:
    """That the release's surface is recorded, once its version is set."""
    if surface(root, read(root).version) == 0:
        return []
    reason = f"{root / 'scripts' / 'surface.py'} could not record the release's surface, as it says above"
    return [Problem(reason, "fix it, and run it again, as `uv run python scripts/surface.py write VERSION` does")]


def uv_lock(root: Path) -> int:
    uv = shutil.which("uv")
    if uv is None:
        msg = "uv is not on the PATH, and the version is set with it: install uv, as https://docs.astral.sh/uv/ says"
        raise ReleaseError(msg)
    return subprocess.run([uv, "lock"], cwd=root, env=shared.environment(), check=False).returncode


@dataclass(frozen=True)
class Run:
    """A run of the CI workflow on a commit, as GitHub's API gives it."""

    status: str
    """`queued`, `in_progress`, `completed`, and the like."""
    conclusion: str | None
    """`success`, `failure` and the like, once completed."""
    url: str


def gate(commit: str, *, on_main: bool, runs: Callable[[], Sequence[Run]], wait: Callable[[], None], attempts: int) -> list[Problem]:
    """Refuse to release `commit` unless it is on main and the CI workflow's newest run on it passed. While that run
    is queued or running, or not made yet, `wait`, and look again, up to `attempts` times."""
    delete = f"delete the tag, as `git push origin :refs/tags/TAG` does, and tag a commit of main that {CI} passed on"
    if not on_main:
        return [Problem(f"the tag names commit {commit}, which is not on main", delete)]
    found: Sequence[Run] = ()
    for attempt in range(attempts):
        if (found := runs()) and found[0].status == "completed":
            newest = found[0]
            return [] if newest.conclusion == "success" else [Problem(f"{CI}'s newest run on {commit} concluded {newest.conclusion}: {newest.url}", delete)]
        if attempt + 1 < attempts:
            wait()
    if not found:
        return [Problem(f"{CI} has no run on {commit}, as a commit that changes only docs gets none", delete)]
    fix = f"rerun this workflow once it passes, as `gh run rerun RUN_ID --failed` does, or {delete}"
    return [Problem(f"{CI}'s newest run on {commit} has not finished: {found[0].url}", fix)]


def on_main(commit: str) -> bool:
    """Whether `commit` is on origin's main, which needs the clone's history."""
    git = _tool("git")
    found = subprocess.run([git, "merge-base", "--is-ancestor", commit, "origin/main"], capture_output=True, text=True, check=False, env=shared.environment())
    if found.returncode not in {0, 1}:
        msg = f"git could not tell whether {commit} is on origin/main ({found.stderr.strip()}): fetch main's history, as `actions/checkout` with `fetch-depth: 0` does"
        raise ReleaseError(msg)
    return found.returncode == 0


def ci_runs(repository: str, commit: str) -> list[Run]:
    """The CI workflow's runs on `commit`, newest first, from GitHub's API through `gh`, which reads `GH_TOKEN`."""
    gh = _tool("gh")
    path = f"repos/{repository}/actions/workflows/{CI}/runs?head_sha={commit}&per_page=20"
    found = subprocess.run([gh, "api", path], capture_output=True, text=True, check=False)
    if found.returncode != 0:
        msg = f"gh could not list {CI}'s runs on {commit} ({found.stderr.strip()}): give the job `actions: read`, and `GH_TOKEN`"
        raise ReleaseError(msg)
    data: object = json.loads(found.stdout)
    listed = data.get("workflow_runs") if isinstance(data, dict) else None
    return [  # GitHub lists a workflow's runs newest first
        Run(status, conclusion if isinstance(conclusion := entry.get("conclusion"), str) else None, str(entry.get("html_url", "")))
        for entry in (listed if isinstance(listed, list) else [])
        if isinstance(entry, dict) and isinstance(status := entry.get("status"), str)
    ]


def _tool(name: str) -> str:
    if (found := shutil.which(name)) is None:
        msg = f"{name} is not on the PATH, and this needs it: install it"
        raise ReleaseError(msg)
    return found


def pages(files: Path) -> dict[str, bytes]:
    """A simple index's page (PEP 503) for each name the wheels and sdists in `files` hold, by its normalized name,
    linking each of its files, by its SHA-256, at `/files/FILE`."""
    links: dict[str, list[str]] = {}
    for path in sorted(files.iterdir()):
        if (found := _DISTRIBUTION.match(path.name)) is None:
            continue
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        link = f'<a href="/files/{html.escape(path.name)}#sha256={digest}">{html.escape(path.name)}</a><br>'
        links.setdefault(shared.normalized(found["name"]), []).append(link)
    return {name: "\n".join(["<!DOCTYPE html>", "<html><body>", *anchors, "</body></html>", ""]).encode() for name, anchors in links.items()}


class FeedServer(http.server.ThreadingHTTPServer):
    """An index of the wheels and sdists in `files`, on 127.0.0.1's `port` (0 for any), not yet serving: each name's
    page from memory, each file from its folder, and every name it does not hold sent to PyPI, as a feed that proxies
    PyPI does, so a repo whose one index this is takes basis from these files and everything else from PyPI."""

    def __init__(self, files: Path, *, port: int) -> None:
        self.pages = pages(files)
        if not self.pages:
            msg = f"{files} holds no wheel and no sdist: build them first, as `uv build --package NAME --out-dir dist` does"
            raise ReleaseError(msg)
        super().__init__(("127.0.0.1", port), functools.partial(_Feed, directory=str(files)))


class _Feed(http.server.SimpleHTTPRequestHandler):
    @override
    def send_head(self) -> io.BytesIO | BinaryIO | None:
        match urlsplit(self.path).path.strip("/").split("/"):
            case ["simple", name]:
                return self._page(shared.normalized(name))
            case ["files", name] if _DISTRIBUTION.match(name):
                self.path = f"/{name}"  # in the folder of files, which is this handler's
                return super().send_head()
            case _:
                self.send_error(HTTPStatus.NOT_FOUND)
                return None

    def _page(self, name: str) -> io.BytesIO | None:
        page = self.server.pages.get(name) if isinstance(self.server, FeedServer) else None
        if page is None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"{PYPI}/{name}/")
            self.end_headers()
            return None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        return io.BytesIO(page)


@dataclass(frozen=True)
class Check:
    tag: str | None


@dataclass(frozen=True)
class SetVersion:
    version: str


@dataclass(frozen=True)
class Packages:
    pass


@dataclass(frozen=True)
class Gate:
    commit: str
    repository: str
    attempts: int
    every: float


@dataclass(frozen=True)
class Feed:
    files: Path
    port: int


type Command = Check | SetVersion | Packages | Gate | Feed


def parse(argv: Sequence[str]) -> Command:
    parser = argparse.ArgumentParser(prog="release.py", description="Check, set and release basis's one version.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    check = verbs.add_parser("check", help="refuse a member, a pin or the lock at another version than the root's")
    check.add_argument("--tag", help="refuse a tag, such as v0.1.0, that names another version, or the unreleased one")
    version = verbs.add_parser("version", help="set the version everywhere, and relock")
    version.add_argument("version", help="X.Y.Z, or `patch` or `minor` for the next one")
    verbs.add_parser("packages", help="print each member a release publishes, a name per line")
    gating = verbs.add_parser("gate", help=f"refuse to release a commit that is not on main, or that {CI} did not pass on")
    gating.add_argument("commit", help="the commit a tag names")
    gating.add_argument("--repository", required=True, help="OWNER/REPO on GitHub, as GITHUB_REPOSITORY gives it")
    gating.add_argument("--attempts", type=int, default=60, help=f"how many times to look for {CI}'s finished run (default 60)")
    gating.add_argument("--every", type=float, default=30, help="seconds between them (default 30)")
    serve = verbs.add_parser("feed", help="serve a folder's wheels and sdists as an index that proxies PyPI")
    serve.add_argument("files", type=Path, help="the folder `uv build --out-dir` wrote")
    serve.add_argument("--port", type=int, default=8761, help="on 127.0.0.1 (default 8761)")
    args = parser.parse_args(argv)
    match args.verb:
        case "check":
            return Check(args.tag)
        case "version":
            return SetVersion(args.version)
        case "packages":
            return Packages()
        case "gate":
            return Gate(args.commit, args.repository, args.attempts, args.every)
        case _:
            return Feed(args.files, args.port)


def main(argv: Sequence[str], *, root: Path = ROOT, lock: Callable[[Path], int] = uv_lock, surface: Callable[[Path, str], int] = write_surface) -> int:
    command = parse(argv)
    try:
        match command:
            case Check(tag):
                workspace = read(root)
                found = problems(workspace, tag=tag) + (released_problems(root, tag) if tag is not None else [])
                if not found:
                    published = ", ".join(member.name for member in workspace.published())
                    sys.stdout.write(f"basis {workspace.version}: {len(workspace.members)} members at one version; a release publishes {published}\n")
            case SetVersion(version):
                found = set_version(root, version, lock=lock) or _recorded(root, surface)
                if not found:
                    sys.stdout.write(f"basis {read(root).version}: every member, each basis-core pin and uv.lock; commit them\n")
            case Packages():
                workspace = read(root)
                if not (found := problems(workspace)):
                    sys.stdout.write("".join(f"{member.name}\n" for member in workspace.published()))
            case Gate(commit, repository, attempts, every):
                found = gate(commit, on_main=on_main(commit), runs=lambda: ci_runs(repository, commit), wait=lambda: time.sleep(every), attempts=attempts)
                if not found:
                    sys.stdout.write(f"{commit} is on main, and {CI} passed on it\n")
            case Feed(files, port):
                return _serve(files, port)
            case _:
                assert_never(command)
    except ReleaseError as error:
        sys.stderr.write(f"ERROR: {error}\n")
        return 1
    for problem in found:
        sys.stderr.write(f"ERROR: {problem.reason}: {problem.fix}\n")
    return 1 if found else 0


def _serve(files: Path, port: int) -> int:
    server = FeedServer(files, port=port)
    sys.stdout.write(f"serving {files} at http://127.0.0.1:{server.server_port}/simple, and every other name from {PYPI}\n")
    sys.stdout.flush()
    server.serve_forever()
    return 0


def _member(root: Path, folder: Path) -> Member:
    path = folder / "pyproject.toml"
    data = _toml(path)
    project = _table(data, "project")
    publish = _table(_table(data, "tool"), TOOL).get("publish")
    if publish is not None and not isinstance(publish, bool):
        msg = f"{path}'s [tool.{TOOL}] publish is {publish!r}: make it true or false"
        raise ReleaseError(msg)
    name = shared.normalized(_string(project, "name", path))
    return Member(name, folder.relative_to(root).as_posix(), _string(project, "version", path), _strings(project, "dependencies"), publish)


def _toml(path: Path) -> dict[str, object]:
    try:
        data: object = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"{path} cannot be read as TOML ({error}): fix what it says"
        raise ReleaseError(msg) from error
    return _table({"": data}, "")


def _table(data: Mapping[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    return {str(name): item for name, item in value.items()} if isinstance(value, dict) else {}


def _string(data: Mapping[str, object], key: str, path: Path) -> str:
    if not isinstance(value := data.get(key), str):
        msg = f"{path} gives no {key} as a string: give it one, as uv needs"
        raise ReleaseError(msg)
    return value


def _strings(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
