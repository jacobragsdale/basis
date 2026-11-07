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
    uv run scripts/release.py check --tag v0.1.0      # and a tag that names another version
    uv run scripts/release.py version 0.1.0           # set the version everywhere, and relock; or `patch`, `minor`
    uv run scripts/release.py packages                # the members a release publishes, a name per line
    uv run scripts/release.py feed dist --port 8761   # dist/ as an index that proxies PyPI, as the firm's feed does
"""

import argparse
import functools
import hashlib
import html
import http.server
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, assert_never, override

ROOT = Path(__file__).resolve().parent.parent
CORE = "basis-core"
TOOL = "basis-release"
"""The `[tool.TOOL]` table each member declares `publish` in. Not `[tool.basis]`, whose unknown keys `basis` refuses."""
PYPI = "https://pypi.org/simple"
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
"""A PEP 508 requirement's name, at its start."""
_DISTRIBUTION = re.compile(r"^(?P<name>[^-]+)-[^-]+(?:\.tar\.gz|-.+\.whl)$")
"""A wheel's or an sdist's file name, whose name part has `_` for each run of `-`, `_` and `.` (PEP 625, PEP 427)."""
_UNSET = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_FROZEN", "UV_LOCKED")
"""Variables `uv run` sets for this script's own environment, which would point `uv lock` away from the workspace."""


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
    """Each package uv.lock locks, by name, with its version."""

    def published(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members if member.publish is True)


def problems(workspace: Workspace, *, tag: str | None = None) -> list[Problem]:
    """Each way the workspace is not one version, each member depending on basis-core at exactly it, every member
    saying whether a release publishes it, a published one needing only published ones, and the root's dependencies
    naming exactly the published ones; and `tag`, when given, naming another version."""
    version = workspace.version
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
    fix = "keep 0.x, or change the generators' templates in the same commit, since a generated repo takes basis as `>=0.MINOR,<0.NEXT`"
    return Problem(f"the version is {version}, and basis stays at 0.x until a real repo runs it in production", fix)


def name_of(requirement: str) -> str:
    """A PEP 508 requirement's name, normalized (PEP 503); "" for one that does not start with a name."""
    found = _NAME.match(requirement)
    return normalized(found.group(1)) if found is not None else ""


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


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


def with_version(text: str, version: str, *, pin: bool) -> str:
    """A pyproject.toml's `text`, with its `[project] version` set to `version`, and with `pin`, each of its
    `[project]` requirements of basis-core made `basis-core==VERSION`. Everything else, comments included, stays."""
    lines = text.splitlines(keepends=True)
    start = next((number for number, line in enumerate(lines) if line.strip() == "[project]"), None)
    if start is None:
        msg = "a pyproject.toml has no [project] table: give each member one, as uv needs"
        raise ReleaseError(msg)
    end = next((number for number in range(start + 1, len(lines)) if lines[number].lstrip().startswith("[")), len(lines))
    project = "".join(lines[start:end])
    project, count = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', project, count=1)
    if count != 1:
        msg = 'a pyproject.toml\'s [project] table gives no version: give it one, as version = "X.Y.Z"'
        raise ReleaseError(msg)
    if pin:  # a requirement is an array's element, never a key's value, as basis-core's own `name = "basis-core"` is
        project = re.sub(rf'(?m)(^[ \t]*|\[[ \t]*|,[ \t]*)"{CORE}(?=[^A-Za-z0-9._-])[^"]*"', rf'\1"{CORE}=={version}"', project)
    return "".join([*lines[:start], project, *lines[end:]])


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
        name: number  # a walrus in a comprehension binds in the function, so not `version`
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and isinstance(name := entry.get("name"), str) and isinstance(number := entry.get("version"), str)
    }
    return Workspace(version, _strings(project, "dependencies"), members, locked)


def set_version(root: Path, asked: str, *, lock: Callable[[Path], int]) -> list[Problem]:
    """Set the workspace's version, every member's and each one's basis-core pin, relock with `lock`, and say what
    still disagrees."""
    workspace = read(root)
    version = next_version(workspace.version, asked)
    if (problem := _past_zero(version)) is not None:
        return [problem]
    for path, pin in [(root / "pyproject.toml", False), *((root / member.folder / "pyproject.toml", True) for member in workspace.members)]:
        path.write_bytes(with_version(path.read_bytes().decode("utf-8"), version, pin=pin).encode("utf-8"))
    if lock(root) != 0:
        return [Problem("uv could not relock the workspace, as it says above", "fix what it says, and run `uv lock`")]
    return problems(read(root))


def uv_lock(root: Path) -> int:
    uv = shutil.which("uv")
    if uv is None:
        msg = "uv is not on the PATH, and the version is set with it: install uv, as https://docs.astral.sh/uv/ says"
        raise ReleaseError(msg)
    environment = {name: value for name, value in os.environ.items() if name not in _UNSET}
    return subprocess.run([uv, "lock"], cwd=root, env=environment, check=False).returncode


def index(files: Path, tree: Path) -> list[str]:
    """Lay each wheel and sdist in `files` out under `tree` as a simple index (PEP 503): `simple/NAME/index.html`
    linking each of NAME's files, by its SHA-256, in `files/`. Returns the names it holds."""
    (tree / "files").mkdir(parents=True)
    links: dict[str, list[str]] = {}
    for path in sorted(files.iterdir()):
        if (found := _DISTRIBUTION.match(path.name)) is None:
            continue
        shutil.copyfile(path, tree / "files" / path.name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        links.setdefault(normalized(found["name"]), []).append(f'<a href="/files/{html.escape(path.name)}#sha256={digest}">{html.escape(path.name)}</a><br>')
    for name, anchors in links.items():
        (tree / "simple" / name).mkdir(parents=True)
        page = "\n".join(["<!DOCTYPE html>", "<html><body>", *anchors, "</body></html>", ""])
        (tree / "simple" / name / "index.html").write_text(page, encoding="utf-8")
    return sorted(links)


class _Feed(http.server.SimpleHTTPRequestHandler):
    """The index `index` laid out, which sends every name it does not hold to PyPI, as a feed that proxies PyPI does:
    so a repo whose one index this is takes basis from its files and everything else from PyPI."""

    @override
    def send_head(self) -> io.BytesIO | BinaryIO | None:
        parts = self.path.partition("?")[0].strip("/").split("/")
        if len(parts) == 2 and parts[0] == "simple" and not (Path(self.directory) / "simple" / normalized(parts[1])).is_dir():  # noqa: PLR2004 -- /simple/NAME/
            self.send_response(302)
            self.send_header("Location", f"{PYPI}/{normalized(parts[1])}/")
            self.end_headers()
            return None
        return super().send_head()


def feed(files: Path, tree: Path, *, port: int) -> http.server.ThreadingHTTPServer:
    """An index of `files`, laid out in `tree`, on 127.0.0.1's `port` (0 for any), not yet serving."""
    if not index(files, tree):
        msg = f"{files} holds no wheel and no sdist: build them first, as `uv build --package NAME --out-dir dist` does"
        raise ReleaseError(msg)
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(_Feed, directory=str(tree)))


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
class Feed:
    files: Path
    port: int


type Command = Check | SetVersion | Packages | Feed


def parse(argv: Sequence[str]) -> Command:
    parser = argparse.ArgumentParser(prog="release.py", description="Check, set and release basis's one version.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    check = verbs.add_parser("check", help="refuse a member, a pin or the lock at another version than the root's")
    check.add_argument("--tag", help="refuse a tag, such as v0.1.0, that names another version")
    version = verbs.add_parser("version", help="set the version everywhere, and relock")
    version.add_argument("version", help="X.Y.Z, or `patch` or `minor` for the next one")
    verbs.add_parser("packages", help="print each member a release publishes, a name per line")
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
        case _:
            return Feed(args.files, args.port)


def main(argv: Sequence[str], *, root: Path = ROOT, lock: Callable[[Path], int] = uv_lock) -> int:
    command = parse(argv)
    try:
        match command:
            case Check(tag):
                workspace = read(root)
                found = problems(workspace, tag=tag)
                if not found:
                    published = ", ".join(member.name for member in workspace.published())
                    sys.stdout.write(f"basis {workspace.version}: {len(workspace.members)} members at one version; a release publishes {published}\n")
            case SetVersion(version):
                found = set_version(root, version, lock=lock)
                if not found:
                    sys.stdout.write(f"basis {read(root).version}: every member, each basis-core pin and uv.lock; commit them\n")
            case Packages():
                workspace = read(root)
                if not (found := problems(workspace)):
                    sys.stdout.write("".join(f"{member.name}\n" for member in workspace.published()))
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
    with tempfile.TemporaryDirectory(prefix="basis-feed-") as tree:
        server = feed(files, Path(tree), port=port)
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
    return Member(_string(project, "name", path), folder.relative_to(root).as_posix(), _string(project, "version", path), _strings(project, "dependencies"), publish)


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
