"""The `basis` command, which basis-core provides and each part adds its verbs to, as `basis.core.verbs` describes.

It finds its repo, the nearest folder at or above the working directory with a `pyproject.toml`, reads the parts that
file declares as `[tool.basis] parts = ["job"]`, loads each one's entry point in the `basis.verbs` group, points a
clone that names no hooks folder at the repo's hooks (`basis.core.hooks.ensure`), and runs one verb: a part's own, or
one of core's, which reach the parts through their `Part`. A verb of a part the repo does not declare is refused,
never offered.

Every failure is one line on stderr, `ERROR: REASON: FIX`, and the exit code is 1, or 2 for a command line that does
not parse. A `BasisError`, which basis raises on purpose with the fix in its message, prints as that line alone, as
does a pydantic `ValidationError` of the caller's settings or params; any other exception is a defect, and its
traceback is logged before the line, as every exception's is with `BASIS_LOG_LEVEL=DEBUG`. `standalone` runs a
command of the same kind outside a repo, such as `basis-job`, which makes one."""

import argparse
import logging
import os
import sys
import tomllib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from importlib.metadata import EntryPoint, entry_points, version
from pathlib import Path
from typing import NoReturn, override

from pydantic import ValidationError

from basis.core import check, hooks, logs, settings, sync
from basis.core.errors import BasisError
from basis.core.verbs import Part, Regress, Replay, Runs, Verb, fail

GROUP = "basis.verbs"
PACKAGES = {"job": "basis-job", "api": "basis-api", "optim": "optim"}
"""The package each part comes in, for the fix when a repo declares a part that is not installed."""
CORE = ("check", "replay", "regress", "new", "sync", "docs")
"""Core's verbs, which no part may add: in the order `basis --help` lists them. `check` and `sync` are always listed,
since every repo has basis's own files, and each of the others when a declared part gives it something to do."""

log = logging.getLogger(__name__)

type Installed = Mapping[str, Callable[[], Part]]
"""Each installed part by name, loaded when called."""


class CommandError(BasisError):
    """A command that cannot run as given; it prints as its one line, and exits with `code`."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Repo:
    pyproject: Path
    parts: dict[str, Part]
    """Each part the repo declares, by name, in the order it declares them."""

    @property
    def root(self) -> Path:
        return self.pyproject.parent


def main() -> int:
    """The `basis` console script."""
    return run(sys.argv[1:], cwd=Path.cwd(), installed=installed())


def installed() -> dict[str, Callable[[], Part]]:
    """Every part an installed package lists in the `basis.verbs` group, by name."""
    return {entry.name: partial(_load, entry) for entry in entry_points(group=GROUP)}


def run(argv: Sequence[str], *, cwd: Path, installed: Installed) -> int:
    """`basis` with `argv`, in the repo at or above `cwd`, with the parts `installed` lists."""
    try:
        logs.setup()
        if list(argv[:1]) == ["--version"]:  # which needs no repo
            sys.stdout.write(f"{versions()}\n")
            return 0
        settings.refuse_renamed(os.environ)
        repo = find(cwd, installed)
        hooks.ensure(repo.root)
        groups = _groups(repo)
        verbs = {verb.name: verb for _, group in groups for verb in group}
        _refuse_undeclared(argv, repo, verbs, installed)
        return _execute(_parser("basis", _describe(repo, groups), verbs.values()), verbs, argv)
    except Exception as error:  # noqa: BLE001 -- the boundary where every failure becomes its one line
        return report(error)


def standalone(prog: str, description: str, verbs: Sequence[Verb], argv: Sequence[str] | None = None) -> int:
    """A command with `verbs` that runs outside a repo, such as `basis-job`, which makes one; it parses, runs and
    fails as `basis` does."""
    try:
        logs.setup()
        return _execute(_parser(prog, description, verbs), {verb.name: verb for verb in verbs}, sys.argv[1:] if argv is None else argv)
    except Exception as error:  # noqa: BLE001 -- the boundary where every failure becomes its one line
        return report(error)


def versions() -> str:
    """basis-core's version and each installed part's package's, as `--version` prints them."""
    found = {"basis-core": version("basis-core")}
    for entry in entry_points(group=GROUP):
        if entry.dist is not None:
            found.setdefault(entry.dist.name, entry.dist.version)
    return ", ".join(f"{name} {number}" for name, number in found.items())


def find(cwd: Path, installed: Installed) -> Repo:
    """The repo at or above `cwd`, with each part it declares loaded."""
    pyproject = next((folder / "pyproject.toml" for folder in (cwd, *cwd.parents) if (folder / "pyproject.toml").is_file()), None)
    if pyproject is None:
        msg = f"no pyproject.toml in {cwd} or a folder above it, so it is in no basis repo: run basis in one, or make one, as `uvx basis-job new repo NAME` does"
        raise CommandError(msg)
    parts: dict[str, Part] = {}
    for name in declared(pyproject):
        if name not in installed:
            package = PACKAGES.get(name, "the package that provides it")
            msg = f"{pyproject} declares the part {name!r}, which no installed package provides: add {package} to its [project] dependencies, and run `uv sync`"
            raise CommandError(msg)
        parts[name] = installed[name]()
    return Repo(pyproject, parts)


def declared(pyproject: Path) -> list[str]:
    """The parts `pyproject` declares, as `[tool.basis] parts = ["job"]`. A part is never inferred from a folder."""
    try:
        data: object = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"{pyproject} is not TOML: {error}: fix it"
        raise CommandError(msg) from error
    example = '[tool.basis] parts = ["job"], naming each part the repo has: job, api or optim'
    table = _table(_table(data, "tool"), "basis")
    if table is None:
        msg = f"{pyproject} declares no basis parts: add {example}"
        raise CommandError(msg)
    if unknown := sorted(set(table) - {"parts"}):
        msg = f"{pyproject}'s [tool.basis] has {', '.join(unknown)}, which basis does not read: remove it, and give {example}"
        raise CommandError(msg)
    names = table.get("parts")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        msg = f"{pyproject}'s [tool.basis] parts is not a list of part names: give {example}"
        raise CommandError(msg)
    found = [str(name) for name in names]
    if len(set(found)) != len(found):
        msg = f"{pyproject}'s [tool.basis] parts names a part twice: name each once"
        raise CommandError(msg)
    return found


def _table(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    found = {str(name): entry for name, entry in value.items()}.get(key)
    return {str(name): entry for name, entry in found.items()} if isinstance(found, dict) else None


def _load(entry: EntryPoint) -> Part:
    part: object = entry.load()()  # the entry point names a callable that takes nothing and returns the part
    if not isinstance(part, Part):
        msg = f"{entry.value}, the {entry.name} part's entry point in {GROUP}, gives a {type(part).__name__} rather than a Part: reinstall the part's package with `uv sync --reinstall`"
        raise CommandError(msg)
    return part


def _groups(repo: Repo) -> list[tuple[str, list[Verb]]]:
    """Core's verbs and each declared part's, as `basis --help` lists them."""
    groups = [("In every basis repo", _core(repo))]
    owners = dict.fromkeys(CORE, _CORE)
    for name, part in repo.parts.items():
        for verb in part.verbs:
            _claim(owners, verb.name, name, f"basis {verb.name}")
        groups.append((f"From the {name} part", list(part.verbs)))
    return groups


_CORE = "core"


def _claim(owners: dict[str, str], key: str, part: str, what: str) -> None:
    """Record that `part` adds `what`, as `key` in `owners`, unless something already does: core, another part, or
    the same part twice."""
    if (owner := owners.get(key)) is None:
        owners[key] = part
        return
    if owner == _CORE:
        msg = f"the {part} part adds `{what}`, which is core's: install every basis package from one release, with `uv sync`"
    elif owner == part:
        msg = f"the {part} part adds `{what}` twice, a defect in its package: install its package from a basis release, with `uv sync --reinstall`"
    else:
        msg = f"the {owner} and {part} parts both add `{what}`, and a repo cannot have both: declare one of them"
    raise CommandError(msg)


def _core(repo: Repo) -> list[Verb]:
    """Each of core's verbs that a declared part gives something to do."""
    parts = repo.parts.values()
    files = [part.files for part in parts if part.files is not None]
    kinds = _kinds(repo)
    saving = {name: part.runs for name, part in repo.parts.items() if part.runs is not None}

    def agent_files() -> list[sync.AgentFiles]:
        return [read() for read in files]

    def run_check(_: argparse.Namespace) -> int:
        return check.check(repo.root, agent_files(), run=check.run_command)

    def replay(n: argparse.Namespace) -> int:
        return _owner(saving, n.name).replay(Replay(n.name, str(n.run_id), n.params))

    def regress(n: argparse.Namespace) -> int:
        return _regress(saving, Regress(n.name, n.last, dict(n.label), n.snapshots))

    offered = {
        "check": Verb("check", "lint, format check, type check and test the repo, and check its framework files and hooks", _no_arguments, run_check),
        "replay": Verb("replay", "rerun a saved run's transform with today's code, and print how its outputs differ", _replay_arguments, replay),
        "regress": Verb("regress", "replay recent published runs with today's code, and list what changed", _regress_arguments, regress),
        "new": Verb("new", f"add a {' or a '.join(kinds)} to the repo", partial(_new_arguments, kinds), lambda n: _new(repo, kinds[str(n.kind)], str(n.name))),
        "sync": Verb("sync", "rewrite the repo's framework and agent files from the installed version", _no_arguments, lambda _: _sync(repo.root, agent_files())),
        "docs": Verb("docs", "list the docs of the installed parts, or print one", _docs_arguments, lambda n: _docs(_pages(repo), n.page)),
    }
    given = {"check", "sync"}.union(*map(_gives, parts))
    return [offered[name] for name in CORE if name in given]


def _gives(part: Part) -> set[str]:
    """The core verbs `part` gives something to do."""
    fields = {"docs": part.docs is not None, "new": bool(part.new), "replay": part.runs is not None, "regress": part.runs is not None}
    return {name for name, given in fields.items() if given}


def _offers(part: Part) -> set[str]:
    """Every verb `part` would add to a repo that declared it, its own and core's."""
    return {verb.name for verb in part.verbs} | _gives(part)


def _refuse_undeclared(argv: Sequence[str], repo: Repo, verbs: Collection[str], installed: Installed) -> None:
    """Refuse a verb that comes with an installed part the repo does not declare, saying how to declare it."""
    verb = next((argument for argument in argv if not argument.startswith("-")), None)
    if verb is None or verb in verbs:
        return
    for name, load in installed.items():
        if name in repo.parts:
            continue
        try:
            part = load()
        except Exception:  # a part that does not load offers no verb; argparse refuses the verb instead
            log.debug("the %s part does not load", name, exc_info=True)
            continue
        if verb in _offers(part):
            msg = f"`basis {verb}` comes with the {name} part, which {repo.pyproject} does not declare: add {name!r} to its [tool.basis] parts"
            raise CommandError(msg)


def _kinds(repo: Repo) -> dict[str, Callable[[Path, str], Path]]:
    kinds: dict[str, Callable[[Path, str], Path]] = {}
    owners: dict[str, str] = {}
    for name, part in repo.parts.items():
        for kind, make in part.new.items():
            _claim(owners, kind, name, f"basis new {kind}")
            kinds[kind] = make
    return kinds


def _owner(saving: Mapping[str, Runs], name: str | None) -> Runs:
    """The runs of the part that runs `name`: the only part that saves runs, or the one whose names hold it."""
    if len(saving) == 1:
        return next(iter(saving.values()))
    if name is None:
        msg = f"more than one part of this repo saves runs ({', '.join(saving)}): give the job's or model's name before the run's id"
        raise CommandError(msg)
    owners = [runs for runs in saving.values() if name in runs.names()]
    if len(owners) == 1:
        return owners[0]
    if owners:
        msg = f"more than one part runs something named {name!r}: rename one of them"
        raise CommandError(msg)
    names = sorted(found for runs in saving.values() for found in runs.names())
    msg = f"nothing in this repo is named {name!r}: give one of {', '.join(names) or 'its jobs or models, once it has some'}"
    raise CommandError(msg)


def _regress(saving: Mapping[str, Runs], args: Regress) -> int:
    if args.name is not None:
        return _owner(saving, args.name).regress(args)
    return max(runs.regress(args) for runs in saving.values())


def _new(repo: Repo, make: Callable[[Path, str], Path], name: str) -> int:
    log.info("made %s", make(repo.root, name))
    return 0


def _sync(root: Path, files: Sequence[sync.AgentFiles]) -> int:
    for path in sync.write(root, files):
        log.info("synced %s", path)
    hooks.ensure(root)  # which did nothing before the command if the hooks were not written yet
    return 0


def _pages(repo: Repo) -> dict[str, Path]:
    """Every declared part's docs, by `PART/PAGE`."""
    return {f"{name}/{path.relative_to(part.docs).with_suffix('').as_posix()}": path for name, part in repo.parts.items() if part.docs is not None for path in sorted(part.docs.rglob("*.md"))}


def _docs(pages: Mapping[str, Path], page: str | None) -> int:
    """Every page's name, or the page `page` names, as `PART/PAGE` or as `PAGE` where only one part has it."""
    if page is None:
        sys.stdout.write("".join(f"{name}\n" for name in pages))
        return 0
    found = [name for name in pages if name == page] or [name for name in pages if name.partition("/")[2] == page]
    if len(found) == 1:
        sys.stdout.write(pages[found[0]].read_text(encoding="utf-8"))
        return 0
    if found:
        return fail(f"more than one part has a page {page!r}", f"give one of {', '.join(found)}")
    return fail(f"no page {page!r}", f"give one of {', '.join(pages)}")


def _no_arguments(_: argparse.ArgumentParser) -> None:
    pass


def label(value: str) -> tuple[str, str]:
    """A `--label KEY=VALUE` argument, as argparse's `type`."""
    key, separator, text = value.partition("=")
    if not separator or not key:
        msg = f"a label is KEY=VALUE, not {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return key, text


def existing_file(text: str) -> Path:
    """A path to a file that exists, as argparse's `type`."""
    if not Path(text).is_file():
        msg = f"no file {text}: give the path of one that exists"
        raise argparse.ArgumentTypeError(msg)
    return Path(text)


def existing_folder(text: str) -> Path:
    """A path to a folder that exists, as argparse's `type`."""
    if not Path(text).is_dir():
        msg = f"no folder {text}: give the path of one that exists"
        raise argparse.ArgumentTypeError(msg)
    return Path(text)


def _positive(text: str) -> int:
    """A count of at least 1, as argparse's `type`."""
    if not text.isdecimal() or int(text) < 1:
        msg = f"give a whole number of 1 or more, not {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return int(text)


def _replay_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", metavar="NAME", nargs="?", help="the job or model whose run it is; without it, the run is found by its id")
    parser.add_argument("run_id", metavar="RUN_ID")
    parser.add_argument("--params", type=existing_file, help="other params to replay with, instead of the run's own")


def _regress_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", metavar="NAME", nargs="?", help="the job or model whose runs to replay; without it, every one")
    parser.add_argument("--last", type=_positive, default=10, help="how many published runs of each, newest first")
    parser.add_argument("--label", type=label, action="append", default=[], help="KEY=VALUE a run's labels must include; repeatable")
    parser.add_argument("--snapshots", help="the snapshot root to read, instead of BASIS_SNAPSHOT_ROOT")


def _new_arguments(kinds: Mapping[str, object], parser: argparse.ArgumentParser) -> None:
    parser.add_argument("kind", metavar="KIND", choices=sorted(kinds), help=f"what to add: {', '.join(sorted(kinds))}")
    parser.add_argument("name", metavar="NAME")


def _docs_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("page", metavar="PAGE", nargs="?", help="the page to print, as PART/PAGE, or PAGE where one part has it")


class _Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        msg = f"{self.prog}: {message}: `{self.prog} --help` says what it takes"
        raise CommandError(msg, code=2)


def _parser(prog: str, description: str, verbs: Iterable[Verb]) -> argparse.ArgumentParser:
    parser = _Parser(prog=prog, usage=f"{prog} [-h] [--version] VERB ...", description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=versions())
    commands = parser.add_subparsers(dest="verb", metavar="VERB", required=True, help=argparse.SUPPRESS)
    for verb in verbs:
        verb.arguments(commands.add_parser(verb.name, description=verb.help, help=verb.help))
    return parser


def _describe(repo: Repo, groups: Sequence[tuple[str, Sequence[Verb]]]) -> str:
    width = max(len(verb.name) for _, verbs in groups for verb in verbs)
    lines = [f"The verbs of {repo.root.name}, a basis repo whose parts are {', '.join(repo.parts)}."]
    for title, verbs in groups:
        lines += ["", f"{title}:", *(f"  {verb.name:<{width}}  {verb.help}" for verb in verbs)]
    return "\n".join([*lines, "", "`basis VERB --help` gives a verb's arguments."])


def _execute(parser: argparse.ArgumentParser, verbs: Mapping[str, Verb], argv: Sequence[str]) -> int:
    try:
        namespace = parser.parse_args(argv)
    except SystemExit as exited:  # --help and --version print, then exit
        return exited.code if isinstance(exited.code, int) else 0
    return verbs[str(namespace.verb)].handle(namespace)


def report(error: Exception) -> int:
    """Print `error` as its one line, after its traceback if it is a defect, and return its exit code."""
    if isinstance(error, CommandError):
        sys.stderr.write(f"ERROR: {error}\n")
        return error.code
    deliberate = isinstance(error, BasisError | ValidationError)
    if not deliberate or log.isEnabledFor(logging.DEBUG):
        log.error("failed", exc_info=error)
    message = one_line(error)
    if not deliberate:
        message = f"{type(error).__name__}: {message}: a defect; the log record above holds its traceback"
    sys.stderr.write(f"ERROR: {message}\n")
    return 1


def one_line(error: BaseException) -> str:
    """`error`'s message, with the notes added on its way up, on one line."""
    if isinstance(error, ValidationError):
        text = _invalid(error)
    elif isinstance(error, FileNotFoundError) and error.filename is not None:
        text = f"no file or folder {error.filename}: give a path that exists"
    else:
        text = str(error) or type(error).__name__
    notes = [str(note) for note in getattr(error, "__notes__", [])]
    return " ".join(": ".join([*notes, text]).split())


def _invalid(error: ValidationError) -> str:
    """A validation error as each value it refuses: for settings, the variable that sets it."""
    variable = {"Settings": settings.variable, "LogSettings": logs.variable}.get(error.title)
    problems = error.errors(include_url=False, include_input=False)
    if variable is not None:
        named = "; ".join(f"{variable(problem['loc'])}: {problem['msg']}" for problem in problems)
        return f"the settings are not valid: {named}: set each variable named, as .env.example shows"
    named = "; ".join(f"{'.'.join(map(str, problem['loc'])) or 'the value'}: {problem['msg']}" for problem in problems)
    return f"{error.title} is not valid: {named}: fix each value named"
