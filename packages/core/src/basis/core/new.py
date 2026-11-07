"""What `new` shares across parts: a repo made from basis's shared templates and the part's own, with the sources its
basis packages come from, so a new repo matches the basis version that made it. The part adds its own files after."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from functools import partial
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

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
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
"""A git commit's full id: SHA-1, or SHA-256 in a repository that uses it."""


class NewError(BasisError, ValueError):
    """A repo or a part that cannot be made as asked."""


class NoReleaseError(BasisError, LookupError):
    """A repo made with neither an index nor a checkout, by a basis that has no release to take its packages from."""


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
    """As `uvx NAME` installs it. uv records no direct URL for a package from an index (PEP 610), so not which index."""
    FILES = "a folder or an archive"
    """Such as a checkout's member, installed editable, a wheel's file, or another version control system's checkout."""


def installed_from(name: str) -> Commit | Installed:
    """The commit the installed distribution `name` came from, or else whether it came from an index."""
    try:
        recorded = distribution(name).read_text("direct_url.json")
    except PackageNotFoundError:
        return Installed.FILES
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


def repo(  # noqa: PLR0913 -- the part says what it adds and where its packages come from, by keyword
    path: Path, *, templates: Path, values: Mapping[str, str], distributions: Mapping[str, str], version: str, index: str | None, checkout: Path | None
) -> None:
    """A new folder at `path`, holding the shared templates and the part's `templates`, each with `{NAME}` replaced by
    `values[NAME]` and `{source}` by where the repo's basis packages come from (see `sources`): with neither an index
    nor a checkout, the tag or the commit the part's package was installed from, if it came from git."""
    if path.exists():
        msg = f"{path} already exists: give a new folder"
        raise NewError(msg)
    if not _REPO.match(path.name):
        msg = f"{path.name!r} is no repo's name: give one of lower-case letters, digits, '.', '_' and '-'"
        raise NewError(msg)
    source = sources(distributions, version=version, index=index, checkout=checkout, installed=partial(installed_from, next(iter(distributions))))
    path.mkdir(parents=True)
    for folder in (TEMPLATES, templates):
        write_templates(path, folder, {**values, "source": source})


def sources(distributions: Mapping[str, str], *, version: str, index: str | None, checkout: Path | None, installed: Callable[[], Commit | Installed]) -> str:
    """The `pyproject.toml` lines that say where a repo's basis packages come from: `index`; a checkout of basis at
    `checkout`, installed editable; or, with neither, where the part was installed from, which `installed` reads only
    then. From git, that is the release's tag when uvx was given `version`'s tag, and otherwise the commit, since a
    branch or another tag moves on, and a new repo matches what made it. From a folder or an archive, it is basis's
    GitHub repository at `version`'s tag, which needs a release. From an index it is refused: uv does not record which
    index, and a new repo would take its basis packages from somewhere other than that feed, such as public GitHub.
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
        name = next(iter(distributions))
        msg = (
            f"{name} came from an index, and uv does not record which, so the new repo cannot take its basis packages from the same feed:"
            f" pass --index with the feed's URL, as `uvx {name} new repo NAME --index URL` does"
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
    """Initialise git, point it at the repo's hooks, and install a new repo's dependencies; its first commit is the
    author's."""
    run(["git", "init", "--quiet", "--initial-branch=main"], repo)
    hooks.ensure(repo)
    run(["uv", "sync", "--quiet"], repo)


def run(argv: list[str], cwd: Path) -> None:
    try:
        subprocess.run(argv, cwd=cwd, check=True)
    except FileNotFoundError as error:
        msg = f"{argv[0]} is not on the PATH, and a new repo needs it: install it, and run `{' '.join(argv)}` in {cwd}"
        raise NewError(msg) from error
    except subprocess.CalledProcessError as error:
        msg = f"`{' '.join(argv)}` failed in {cwd}, as it says above: fix what it says, and run it there again"
        raise NewError(msg) from error
