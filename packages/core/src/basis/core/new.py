"""What `new` shares across parts: a repo made from basis's shared templates and the part's own, with the sources its
basis packages come from, so a new repo matches the basis version that made it. The part adds its own files after."""

import json
import keyword
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import partial
from importlib.metadata import PackageNotFoundError, distribution
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from basis.core import hooks
from basis.core.errors import BasisError

GIT = "https://github.com/jacobragsdale/basis"
"""Where a repo's basis packages come from, at the version's tag, when neither an index nor a checkout is given and
the part was installed from a folder or an archive."""
FOLDER = "packages/core"
"""basis-core's folder in a checkout of basis."""
UNRELEASED = "0.0.0"
"""The version basis carries until its first release, which no tag names."""
TEMPLATES = Path(__file__).parent / "templates" / "repo"
"""The files every part's repo holds."""
_DOTTED = {"gitignore", "dockerignore", "env.example", "python-version"}
_REPO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
OWN_NAMES = frozenset({"basis", "jobs", "tests", "snapshots"})
"""Names a repo's own top-level package may not take: basis's namespace and the folders basis puts in a repo. A part's
own package, once installed, is refused as any installed package is."""
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
"""A git commit's full id: SHA-1, or SHA-256 in a repository that uses it."""


class NewError(BasisError, ValueError):
    """A repo or a part that cannot be made as asked."""


class NoReleaseError(BasisError, LookupError):
    """A repo made with neither an index nor a checkout, by a basis that has no release to take its packages from."""


class NotInstalledError(BasisError, LookupError):
    """A distribution that is not installed, so where it came from cannot be read."""


@dataclass(frozen=True)
class Commit:
    """The commit of a git repository holding basis that a part's package was installed from, as `uvx --from
    'git+URL@REF#subdirectory=packages/job'` installs it."""

    url: str
    commit: str
    """The commit's full id, whatever ref named it: a branch moves on, and a new repo matches what made it."""
    revision: str | None = None
    """The ref uvx was given, such as `v0.7.0` or `main`, which uv records as `requested_revision`; None when none was."""


class Installed(Enum):
    """Where a part's package was installed from, when not from a git commit."""

    INDEX = "an index"
    """As `uvx NAME` installs it, or from a `--find-links` folder: uv records no direct URL for either (PEP 610), so not
    which."""
    FILES = "a folder or an archive"
    """Such as a checkout's member, installed editable, a wheel's file, or another version control system's checkout."""


def installed_from(name: str) -> Commit | Installed:
    """The commit the installed distribution `name` came from, or else whether it came from an index."""
    try:
        recorded = distribution(name).read_text("direct_url.json")
    except PackageNotFoundError as error:
        msg = f"{name} is not installed here, so where its new repo's basis packages come from cannot be read: reinstall {name}, or give --index or --framework-path"
        raise NotInstalledError(msg) from error
    if recorded is None:
        return Installed.INDEX
    commit = commit_of(recorded, name)
    return commit if commit is not None else Installed.FILES


def commit_of(direct_url: str, name: str) -> Commit | None:
    """The commit the distribution `name`'s `direct_url.json` (PEP 610) names, or None when it was installed from a
    folder, an archive, or another version control system. A file that is not what PEP 610 describes, such as a git
    install with no full commit id, is refused, since where the repo's packages come from would be a guess."""
    fix = f"reinstall {name}, as `uvx --refresh --from 'git+URL@REF#subdirectory=FOLDER' {name}` does, or give --index or --framework-path"
    try:
        data: object = json.loads(direct_url)
    except json.JSONDecodeError as error:
        msg = f"{name}'s direct_url.json, which says where it was installed from, is not JSON ({error}): {fix}"
        raise NewError(msg) from error
    if not isinstance(data, dict) or not isinstance(url := data.get("url"), str):
        msg = f"{name}'s direct_url.json, which says where it was installed from, gives no url: {fix}"
        raise NewError(msg)
    if (vcs := data.get("vcs_info")) is None:
        return None
    if not isinstance(vcs, dict) or not isinstance(kind := vcs.get("vcs"), str):
        msg = f"{name}'s direct_url.json has a vcs_info that names no version control system: {fix}"
        raise NewError(msg)
    if kind != "git":
        return None
    if not isinstance(commit := vcs.get("commit_id"), str) or not _COMMIT.match(commit):
        msg = f"{name}'s direct_url.json says it came from git, and gives no full commit id: {fix}"
        raise NewError(msg)
    if (revision := vcs.get("requested_revision")) is not None and not isinstance(revision, str):
        msg = f"{name}'s direct_url.json gives a requested_revision that is not a ref: {fix}"
        raise NewError(msg)
    return Commit(url, commit, revision)


@contextmanager
def building(path: Path) -> Generator[Path]:
    """The folder to make a new repo at `path` in: a hidden one beside it, renamed to `path` once the block returns,
    and removed if it raises, so a repo that cannot be made leaves nothing behind for the retry to find. Installing it
    comes after, in place, since a virtual environment does not move."""
    if path.exists():
        msg = f"{path} already exists: give a new folder"
        raise NewError(msg)
    if not _REPO.match(path.name):
        msg = f"{path.name!r} is no repo's name: give one of lower-case letters, digits, '.', '_' and '-'"
        raise NewError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    try:
        yield folder
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    folder.rename(path)


def repo(folder: Path, *, templates: Path, values: Mapping[str, str], distributions: Mapping[str, str], version: str, index: str | None, checkout: Path | None) -> None:
    """The shared templates and the part's `templates` in `folder`, which `building` gives, each with `{NAME}`
    replaced by `values[NAME]`, `{upper}` by the first version the repo does not take (`upper_bound`), and `{source}`
    by where the repo's basis packages come from (see `sources`): with neither an index nor a checkout, the tag or the
    commit the part's package was installed from, if it came from git."""
    source = sources(distributions, version=version, index=index, checkout=checkout, installed=partial(installed_from, next(iter(distributions))))
    for templated in (TEMPLATES, templates):
        write_templates(folder, templated, {**values, "upper": upper_bound(version), "source": source})


def upper_bound(version: str) -> str:
    """The first version a repo made at `version` does not take: the next minor before 1.0, where a minor may break the
    contract, and the next major from 1.0, so the repo takes the fixes and features that keep it."""
    major, minor, *_ = (int(part) for part in [*re.findall(r"\d+", version)[:2], "0"])
    return f"0.{minor + 1}" if major == 0 else str(major + 1)


def check_package(name: str, what: str) -> None:
    """Refuse `name` for a repo's own top-level package, named for its `what`, before anything is written: a Python
    keyword, which cannot be imported; a standard library module's name, or one this environment already imports,
    which it would shadow; or one of basis's own (`OWN_NAMES`)."""
    if keyword.iskeyword(name):
        reason = "is a Python keyword, which no import can name"
    elif name in sys.stdlib_module_names:
        reason = "is a standard library module's name, which the package would shadow"
    elif name in OWN_NAMES:
        reason = "is one of basis's own names in a repo"
    elif find_spec(name) is not None:
        reason = "is the name of a package the repo's environment already holds, which the package would shadow"
    else:
        return
    msg = f"{what} {name!r} {reason}: give another name"
    raise NewError(msg)


def sources(distributions: Mapping[str, str], *, version: str, index: str | None, checkout: Path | None, installed: Callable[[], Commit | Installed]) -> str:
    """The `pyproject.toml` lines that say where a repo's basis packages come from: `index`; a checkout of basis at
    `checkout`, installed editable; or, with neither, where the part was installed from, which `installed` reads only
    then. From git, that is the release's tag when uvx was given `version`'s tag, and otherwise the commit, since a
    branch or another tag moves on, and a new repo matches what made it; `install` checks that the tag still names that
    commit. From a folder or an archive, it is basis's GitHub repository at `version`'s tag, which needs a release.
    From an index or a `--find-links` folder it is refused: uv does not record which, and a new repo would take its
    basis packages from somewhere other than that feed, such as public GitHub.
    `distributions` are the part's packages, by name, each with its folder in a checkout. basis-core is not among them:
    no repo depends on it by name, and each part's own workspace source brings it from the same checkout or commit, so
    a source for it would go unused.

    The index replaces PyPI for the whole repo (`default = true`), so a name the index lacks fails rather than being
    taken from PyPI, where anyone may publish it; the firm's feed proxies PyPI for every other package."""
    if index is not None and checkout is not None:
        msg = "--index and --framework-path both say where the repo's basis packages come from: give one of them"
        raise NewError(msg)
    if index is not None:
        comment = "# Every package comes from this index, which proxies PyPI; a name it lacks fails, rather than coming from PyPI."
        return f'{comment}\n[[tool.uv.index]]\nname = "internal"\nurl = "{index}"\ndefault = true\n'
    if checkout is not None:
        root = checkout.resolve()
        if missing := [folder for folder in (*distributions.values(), FOLDER) if not (root / folder / "pyproject.toml").exists()]:
            msg = f"{checkout} is not a checkout of basis: it has no {', '.join(missing)}; give the folder that holds packages/"
            raise NewError(msg)
        lines = [f'{name} = {{ path = "{(root / folder).as_posix()}", editable = true }}' for name, folder in distributions.items()]
    elif isinstance(origin := installed(), Commit):
        ref = f'tag = "v{version}"' if origin.revision == f"v{version}" else f'rev = "{origin.commit}"'
        lines = [f'{name} = {{ git = "{origin.url}", subdirectory = "{folder}", {ref} }}' for name, folder in distributions.items()]
    elif origin is Installed.INDEX:
        name, folder = next(iter(distributions.items()))
        ref = "" if version == UNRELEASED else f"@v{version}"
        msg = (
            f"{name} was installed from an index or a --find-links folder, which uv does not record, so the new repo cannot take its basis packages"
            f" from the same place: rerun the same command with `--index URL`, naming the feed, or install {name} from git,"
            f" as `uvx --from 'git+{GIT}{ref}#subdirectory={folder}'` does"
        )
        raise NewError(msg)
    elif version == UNRELEASED:
        name, folder = next(iter(distributions.items()))
        command = f"uvx --from 'git+{GIT}#subdirectory={folder}' {name} new repo NAME"
        msg = f"basis has no release yet, so no tag holds its packages: run {name} from git, as `{command}` does, or clone {GIT} and pass --framework-path with the clone's folder"
        raise NoReleaseError(msg)
    else:
        lines = [f'{name} = {{ git = "{GIT}", subdirectory = "{folder}", tag = "v{version}" }}' for name, folder in distributions.items()]
    return "[tool.uv.sources]\n" + "\n".join(lines) + "\n"


def write_templates(repo: Path, templates: Path, values: Mapping[str, str]) -> None:
    """Each file under `templates`, rendered, at its place in `repo`: `NAME.in` as `NAME`, and a file a repo keeps
    under a dotted name, such as `gitignore`, as `.gitignore`."""
    for template in sorted(templates.rglob("*")):
        if template.is_dir() or "__pycache__" in template.parts:
            continue
        relative = template.relative_to(templates)
        name = f".{relative.name}" if relative.name in _DOTTED else relative.name.removesuffix(".in")
        target = repo / relative.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(template.read_text(encoding="utf-8"), values), encoding="utf-8")


def render(text: str, values: Mapping[str, str]) -> str:
    """`text` with each `{NAME}` replaced by `values[NAME]`, in the order `values` gives them."""
    for name, value in values.items():
        text = text.replace(f"{{{name}}}", value)
    return text


def install(repo: Path) -> None:
    """Initialise git, point it at the repo's hooks, install a new repo's dependencies, and check that each tag its
    basis packages come from names the commit that made it (`check_tags`); its first commit is the author's."""
    run(["git", "init", "--quiet", "--initial-branch=main"], repo)
    hooks.ensure(repo)
    run(["uv", "sync", "--quiet"], repo)
    check_tags(repo)


def check_tags(repo: Path, installed: Callable[[str], Commit | Installed] = installed_from) -> None:
    """Refuse a new repo whose uv.lock takes a package from a git tag at another commit than the one that package was
    installed from here, to make the repo: a tag moved since, or a fork's tag of the same name. A package from no tag,
    or not installed from git here, is not checked."""
    packages: object = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8")).get("package")
    for entry in packages if isinstance(packages, list) else []:
        if not isinstance(entry, dict):
            continue
        name, source = entry.get("name"), entry.get("source")
        git = source.get("git") if isinstance(source, dict) else None
        if not isinstance(name, str) or not isinstance(git, str):
            continue
        url, _, locked = git.partition("#")  # URL?subdirectory=FOLDER&tag=TAG#COMMIT, as uv locks a git source
        tags = parse_qs(urlsplit(url).query).get("tag", [])
        if not tags:
            continue
        try:
            origin = installed(name)
        except NotInstalledError:
            continue
        if isinstance(origin, Commit) and origin.commit != locked:
            msg = (
                f"the new repo's uv.lock takes {name} from tag {tags[0]} at commit {locked}, and the {name} installed here, which made it, came from commit {origin.commit},"
                f" so the tag has moved since, or names another repository's commit: delete {repo}, and make it again with `@{origin.commit}`"
                f" in place of `@{tags[0]}` in the same command, or find out where the tag points"
            )
            raise NewError(msg)


def run(argv: list[str], cwd: Path) -> None:
    try:
        subprocess.run(argv, cwd=cwd, check=True)
    except FileNotFoundError as error:
        msg = f"{argv[0]} is not on the PATH, and a new repo needs it: install it, and run `{' '.join(argv)}` in {cwd}"
        raise NewError(msg) from error
    except subprocess.CalledProcessError as error:
        msg = f"`{' '.join(argv)}` failed in {cwd}, as it says above: fix what it says, and run it there again"
        raise NewError(msg) from error
