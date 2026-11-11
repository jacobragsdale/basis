"""`basis doctor`: whether this desktop is set up for basis, on Windows and in WSL, with the fix for each problem. It
checks uv and its version, git, that the repo's clone runs its hooks (pointing a clone that names no hooks folder at
them, as `basis check` does), the feed, and the settings. It runs in a repo or outside one, where it checks what the
desktop has alone. Each check passed prints one line on stdout, and each problem one `ERROR` line on stderr that ends
with its fix. It reads no database and no network, unless it is given an environment: `basis doctor --env qa` also
checks where that environment's settings come from (`basis.core.environments`), that each database's logins answer
a query, the write login's too, and that each folder and its run store are reachable.

The feed is the default index of the user's `uv.toml`: the design has each developer name the firm's feed there as
the default index, not an extra one, so a name the feed holds is never looked up on public PyPI. uv reads
`%APPDATA%\\uv\\uv.toml` on Windows and `$XDG_CONFIG_HOME/uv/uv.toml` (`~/.config/uv/uv.toml`) elsewhere, and a
system-wide file below it; `UV_CONFIG_FILE` replaces both, `UV_NO_CONFIG` turns them off, and `UV_DEFAULT_INDEX` or
`UV_INDEX_URL` overrides their default index. basis does not know the firm's feed: where the repo's `pyproject.toml`
names a default index, as a repo made with `--index` does, the user's must be the same one."""

import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from basis.core import calendars, environments, hooks, lake, qa, settings
from basis.core.databases import Database, open_database
from basis.core.errors import BasisError, ServiceError
from basis.core.settings import MssqlSettings, OracleSettings, Settings
from basis.core.storage import Root
from basis.core.sync import Problem
from basis.core.verbs import Part

UV = (0, 13, 6)
"""The oldest uv basis works with: the version of the build backend, uv_build, that its repos build with."""
GIT = (2, 9, 0)
"""The oldest git basis works with: the first to run hooks from the folder `core.hooksPath` names."""
PYPI = re.compile(r"^https?://(pypi\.org|pypi\.python\.org|files\.pythonhosted\.org)(/|$)")
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

type Run = Callable[[Sequence[str]], str | None]
"""Runs a command and returns what it prints, or None when it fails."""


@dataclass(frozen=True)
class Desktop:
    """What doctor reads of the machine it runs on, given so a test can be any machine."""

    environ: Mapping[str, str]
    system: str
    """`Windows`, `WSL` or `Linux`."""
    which: Callable[[str], str | None]
    run: Run
    home: Path

    @classmethod
    def here(cls) -> Desktop:
        system = platform.system()
        wsl = system == "Linux" and ("WSL_DISTRO_NAME" in os.environ or "microsoft" in platform.release().lower())
        return cls(os.environ, "WSL" if wsl else system, shutil.which, _output, Path.home())


def _output(argv: Sequence[str]) -> str | None:
    try:
        found = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return found.stdout.strip() if found.returncode == 0 else None


@dataclass(frozen=True)
class Found:
    """One check: what it found, and the problem, if any. A `note` is neither: a piece of the gates this desktop
    skips by choice, such as `regress` without a run store."""

    what: str
    problem: Problem | None = None
    note: bool = False


type Describe = Callable[[Exception], str]
"""A failure's one line, `REASON: FIX`, as `basis` prints it."""


def doctor(  # noqa: PLR0913 -- what it checks and how it reaches the databases, by keyword
    repo: Path | None,
    parts: Mapping[str, Part],
    desktop: Desktop,
    *,
    describe: Describe,
    unread: str | None = None,
    env: str | None = None,
    databases: Callable[[Settings, str], Database] = open_database,
    lakes: Callable[[str, Settings], lake.Lake] = lake.lake,
    today: date | None = None,
) -> int:
    """Check the desktop, and the repo at `repo` when doctor runs in one; `unread` says why a folder with a
    `pyproject.toml` is no basis repo basis can read. With `env`, also that environment's settings, each login of
    each database, opened by `databases`, each lake, opened by `lakes`, and its run store. 0 when nothing is wrong."""
    found = [Found(f"{desktop.system}, Python {platform.python_version()}"), _uv(desktop), _git(desktop), *_feed(repo, desktop)]
    found += [Found(note, note=True) for note in calendars.nearing_end(today or datetime.now(UTC).date())]
    if unread is not None:
        found.append(Found("the repo", _split(unread)))
    if repo is not None:
        found += [*_hooks(repo), *_where(repo, desktop), *_settings(repo, parts, desktop, describe)]
    if env is not None:
        found += _environment_of(env, repo, desktop, describe, databases=databases, lakes=lakes)
    problems = 0
    for entry in found:
        if entry.problem is None:
            sys.stdout.write(f"{'note' if entry.note else 'ok'}: {entry.what}\n")
            sys.stdout.flush()  # so the lines keep their order, on stdout and stderr
        else:
            sys.stderr.write(f"ERROR: {entry.problem[0]}: {entry.problem[1]}\n")
            problems += 1
    return 1 if problems else 0


def _split(line: str) -> Problem:
    """A failure's line as its reason and its fix, which it ends with after the last `: `."""
    reason, _, fix = line.rpartition(": ")
    return (reason, fix) if reason else (line, "fix it")


def _version(text: str) -> tuple[int, int, int] | None:
    found = _VERSION.search(text)
    return (int(found[1]), int(found[2]), int(found[3] or 0)) if found is not None else None


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def _uv(desktop: Desktop) -> Found:
    install = "install it for your user, with no admin rights, as https://docs.astral.sh/uv/getting-started/installation/ says, and open a new terminal"
    if (path := desktop.which("uv")) is None:
        return Found("uv", ("uv is not on the PATH, and basis runs every check through it", install))
    printed = desktop.run([path, "--version"]) or ""
    if (version := _version(printed)) is None:
        return Found("uv", (f"{path} --version printed {printed!r}, which names no version", f"reinstall uv: {install}"))
    if version < UV:
        return Found("uv", (f"uv is {_dotted(version)}, and basis needs {_dotted(UV)} or later", "run `uv self update`, or update it as your desktop installed it"))
    return Found(f"uv {_dotted(version)} at {path}")


def _git(desktop: Desktop) -> Found:
    fix = "install git (on Windows, Git for Windows, which also runs the hooks' sh), and put it on the PATH"
    if (path := desktop.which("git")) is None:
        return Found("git", ("git is not on the PATH, so the repo's commit and push hooks cannot run", fix))
    printed = desktop.run([path, "--version"]) or ""
    if (version := _version(printed)) is None:
        return Found("git", (f"{path} --version printed {printed!r}, which names no version", fix))
    if version < GIT:
        return Found("git", (f"git is {_dotted(version)}, which does not run hooks from the folder core.hooksPath names", f"update it to {_dotted(GIT)} or later"))
    return Found(f"git {_dotted(version)} at {path}")


def _hooks(repo: Path) -> list[Found]:
    hooks.ensure(repo)
    if (problem := hooks.problem(repo)) is not None:
        return [Found("the hooks", problem)]
    missing = [hook for hook in hooks.HOOKS if not (repo / hooks.FOLDER / hook).is_file()]
    skipped = [hook for hook in hooks.HOOKS if hook not in missing and os.name != "nt" and not os.access(repo / hooks.FOLDER / hook, os.X_OK)]
    if missing or skipped:
        names = ", ".join(f"{hooks.FOLDER}/{hook}" for hook in [*missing, *skipped])
        return [Found("the hooks", (f"{names} {'is' if len(missing) + len(skipped) == 1 else 'are'} missing or not executable, so git skips them", "run `uv run basis sync`, which writes them"))]
    return [Found(f"the clone runs the repo's hooks from {hooks.FOLDER}/")]


def _where(repo: Path, desktop: Desktop) -> list[Found]:
    """In WSL, a repo on a Windows drive shares one clone, and one `.venv`, with Windows, which uv rebuilds for each."""
    resolved = repo.resolve().as_posix()
    if desktop.system == "WSL" and re.match(r"^/mnt/[a-z]/", resolved):
        reason = f"{resolved} is on a Windows drive, where WSL's uv and Windows' share one .venv, rebuilding it for each, and WSL reads every file slowly"
        return [Found("the repo's folder", (reason, "clone the repo again in WSL's own filesystem, such as ~/repos, and work on it there"))]
    return []


def _feed(repo: Path | None, desktop: Desktop) -> list[Found]:
    environ = desktop.environ
    if environ.get("UV_NO_CONFIG", "") not in {"", "0", "false"}:
        return [Found("the feed", ("UV_NO_CONFIG is set, so uv reads no uv.toml, and takes packages from public PyPI unless told otherwise", "unset it"))]
    wanted = _repo_index(repo) if repo is not None else None
    for variable in ("UV_DEFAULT_INDEX", "UV_INDEX_URL"):
        if (url := environ.get(variable)) is not None:
            return [_index(url, f"{variable}, which overrides uv.toml's default index", wanted)]
    files = _uv_tomls(desktop)
    for path in files:
        if not path.is_file():
            continue
        try:
            data: object = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            return [Found("the feed", (f"{path} is not TOML ({error}), so uv stops at every command", "fix it"))]
        if (url := _default_index(data)) is not None:
            return [_index(url, str(path), wanted)]
    where = files[0] if files else Path("uv.toml")
    reason = f"no uv.toml names a default index (uv reads {', '.join(map(str, files))}), so uv takes packages from public PyPI, where anyone may publish a name the firm's feed holds"
    return [Found("the feed", (reason, f"add the firm's feed to {where} as [[index]] with url = FEED and default = true, as the feed's setup says"))]


def _uv_tomls(desktop: Desktop) -> list[Path]:
    """The uv.toml files uv reads, the user's before the system's."""
    environ = desktop.environ
    if (given := environ.get("UV_CONFIG_FILE")) is not None:
        return [Path(given)]
    if desktop.system == "Windows":
        user = [Path(environ["APPDATA"]) / "uv" / "uv.toml"] if "APPDATA" in environ else []
        return [*user, Path(environ.get("SYSTEMDRIVE", "C:") + "\\") / "ProgramData" / "uv" / "uv.toml"]
    config = Path(xdg) if (xdg := environ.get("XDG_CONFIG_HOME", "")) != "" else desktop.home / ".config"
    system = [Path(folder) / "uv" / "uv.toml" for folder in environ.get("XDG_CONFIG_DIRS", "").split(":") if folder] or [Path("/etc/uv/uv.toml")]
    return list(dict.fromkeys([config / "uv" / "uv.toml", *system]))


def _default_index(data: object) -> str | None:
    """The default index a uv.toml names: an `[[index]]` with `default = true`, or the older `index-url`."""
    if not isinstance(data, dict):
        return None
    indexes = data.get("index")
    for entry in indexes if isinstance(indexes, list) else []:
        if isinstance(entry, dict) and entry.get("default") is True and isinstance(url := entry.get("url"), str):
            return url
    url = data.get("index-url")
    return url if isinstance(url, str) else None


def _repo_index(repo: Path) -> str | None:
    """The default index the repo's `pyproject.toml` names in `[[tool.uv.index]]`, if any."""
    try:
        tool: object = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8")).get("tool")
    except OSError, tomllib.TOMLDecodeError:
        return None
    uv = tool.get("uv") if isinstance(tool, dict) else None
    return _default_index(uv)


def _index(url: str, where: str, wanted: str | None) -> Found:
    if PYPI.match(url):
        return Found("the feed", (f"{where} makes public PyPI the default index, not the firm's feed", "make the feed the default index there"))
    if wanted is not None and url.rstrip("/") != wanted.rstrip("/"):
        return Found("the feed", (f"{where} makes {url} the default index, and the repo's pyproject.toml names {wanted}", "make the repo's index the default there"))
    return Found(f"the feed: {url}, the default index in {where}")


def _settings(repo: Path, parts: Mapping[str, Part], desktop: Desktop, describe: Describe) -> list[Found]:
    """The settings every command reads, from the process's environment, and the QA tier's, from its file."""
    return [*_environment(repo, desktop, describe), *_qa(repo, parts, desktop, describe)]


def _environment(repo: Path, desktop: Desktop, describe: Describe) -> list[Found]:
    found: list[Found] = []
    try:
        settings.refuse_renamed(desktop.environ)
        configured = Settings().snapshot_root
    except (ValidationError, BasisError) as error:
        return [Found("the settings", _split(describe(error)))]
    if configured is not None:
        location = settings.locate(configured, repo)
        try:
            reachable = Root.at(location).is_dir()
        except (OSError, ValueError) as error:
            reachable, location = False, f"{location} ({' '.join(str(error).split())})"
        problem = (f"BASIS_SNAPSHOT_ROOT is {location}, which this machine cannot reach, so the push hook skips regress", "correct it, or unset it") if not reachable else None
        found.append(Found(f"the run store regress replays: {location}", problem))
    else:
        found.append(Found("BASIS_SNAPSHOT_ROOT is not set, so the push hook skips regress", note=True))
    return found


PING: dict[type[MssqlSettings | OracleSettings], str] = {MssqlSettings: "SELECT 1 AS answered", OracleSettings: "SELECT 1 AS answered FROM DUAL"}
"""The query each dialect's logins are checked with, which reads no table."""


def _environment_of(  # noqa: PLR0913 -- the environment, and how doctor reaches what it configures
    given: str, repo: Path | None, desktop: Desktop, describe: Describe, *, databases: Callable[[Settings, str], Database], lakes: Callable[[str, Settings], lake.Lake]
) -> list[Found]:
    """`basis doctor --env ENV`: where the environment's settings come from, whether each of its databases' logins
    answers, whether each lake's container is reachable, and renames, where the environment may be written, and
    whether its run store is reachable."""
    try:
        environment = environments.load(given, Settings, windows=desktop.system == "Windows", home=desktop.home)
    except (ValidationError, BasisError) as error:
        return [Found(f"the {given} environment", _split(describe(error)))]
    env, configured = environment.name, environment.settings
    where = str(environment.source) if environment.source is not None else "the process's environment"
    found: list[Found] = [Found(f"{env}'s settings, from {where}")]
    if env == "prod":
        writes = (
            "may be written here: the process sets BASIS_PROD_WRITES=1"
            if environment.writes
            else "is read-only here, since the process does not set BASIS_PROD_WRITES=1, as only a deployment that writes does"
        )
        found.append(Found(f"prod {writes}", note=True))
    for name in sorted(configured.databases):
        found += _logins(env, name, configured, where, databases)
    for name, shared in sorted(configured.folders.items()):
        try:
            reachable = Path(shared.path).is_dir()
        except OSError:
            reachable = False
        fix = f"mount it, or correct BASIS_FOLDERS__{name.upper()}__PATH in {where}"
        problem = (f"{env}'s folder {name}, at {shared.path}, is not there or not reachable from here", fix) if not reachable else None
        found.append(Found(f"{env}'s folder {name}: {shared.path}", problem))
    for name in sorted(configured.lakes):
        what = f"{env}'s lake {name}"
        try:
            store = lakes(name, configured)
            checked = lake.check(store, rename=environment.writes)
        except BasisError as error:
            found.append(Found(what, (f"{what} cannot be archived to ({' '.join(str(error).split())})", f"check BASIS_LAKES__{name.upper()}__* in {where}")))
            continue
        found.append(Found(f"{what}: {checked}"))
    if configured.snapshot_root is None:
        found.append(Found(f"{env} names no run store, BASIS_SNAPSHOT_ROOT, so `basis run --env {env}` cannot save its runs", note=True))
    else:
        location = settings.locate(configured.snapshot_root, repo if repo is not None else Path.cwd())
        try:
            reachable = Root.at(location).is_dir()
        except (OSError, ValueError) as error:
            reachable, location = False, f"{location} ({' '.join(str(error).split())})"
        problem = (f"{env}'s run store, BASIS_SNAPSHOT_ROOT, is {location}, which this machine cannot reach", f"correct it in {where}, or reach it from here") if not reachable else None
        found.append(Found(f"{env}'s run store: {location}", problem))
    return found


def _logins(env: str, name: str, configured: Settings, where: str, databases: Callable[[Settings, str], Database]) -> list[Found]:
    """Whether each login of the database `name` answers a query that reads no table: its read login's, and its write
    login's where it has one."""
    found = configured.databases[name]
    database = databases(configured, name)
    logins = [("read", False), *([("write", True)] if found.write is not None else [])]
    checked: list[Found] = []
    for login, writes in logins:
        what = f"{env}'s database {name}: its {login} login"
        try:
            with database.statements(writes=writes) as statements:
                statements.query(PING[type(found)], {}, timeout_s=found.timeout_s)
        except (ServiceError, BasisError) as error:
            reason = f"{what} does not answer ({type(error).__name__}: {' '.join(str(error).split())})"
            checked.append(Found(what, (reason, f"check BASIS_DATABASES__{name.upper()}__* in {where}, and that this machine may reach the database")))
            continue
        checked.append(Found(f"{what} answers"))
    return checked


def _qa(repo: Path, parts: Mapping[str, Part], desktop: Desktop, describe: Describe) -> list[Found]:
    try:
        declared = qa.declared(repo / "pyproject.toml", parts)
    except BasisError as error:
        return [Found("the QA tier", _split(describe(error)))]
    if declared is None:
        return [Found("the repo declares no QA tier, so the push hook runs none", note=True)]
    source = environments.user_file("qa", desktop.environ, windows=desktop.system == "Windows", home=desktop.home)
    if not source.is_file():
        problem = (f"the repo declares a QA tier, and this machine has no {source}, so the push hook skips it", "write it with QA's databases and store, as the repo's .env.example lists")
        return [Found("the QA tier", problem)]
    try:
        read = settings.read(source)
    except ValidationError as error:
        reason, fix = _split(describe(error))
        return [Found("the QA tier", (f"{source}: {reason}", fix))]
    problems = qa.problems(read, declared, source=source)
    return [Found("the QA tier", problem) for problem in problems] or [Found(f"the QA settings in {source}, each pointing at QA")]
