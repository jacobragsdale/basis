"""The `basis` command, which basis-core provides and each part adds its verbs to, as `basis.core.verbs` describes.

It finds its repo, the nearest folder at or above the working directory with a `pyproject.toml`, reads the parts that
file declares as `[tool.basis] parts = ["job"]`, loads each one's entry point in the `basis.verbs` group, and runs
one verb: a part's own, or one of core's, which reach the parts through their `Part`. A verb that jobs and models
both have, such as `run`, is core's too (`SHARED`): it parses NAME, and RUN_ID where the verb takes one, and the part
that runs NAME parses the rest. A verb of a part the repo does not declare is refused, never offered. The verbs a developer runs on a clone, `check`, `sync` and `new`, point a clone
that names no hooks folder at the repo's hooks (`basis.core.hooks.ensure`); `run` and the rest, which a container or a
scheduler runs, leave git alone.

Every failure is one line on stderr, `ERROR: REASON: FIX`, and the exit code is 1, or 2 for a command line that does
not parse. A `BasisError`, which basis raises on purpose with the fix in its message, prints as that line alone, as
does a pydantic `ValidationError` of the caller's settings or params; any other exception is a defect, and its
traceback is logged before the line, as every exception's is with `BASIS_LOG_LEVEL=DEBUG`. `standalone` runs a
command of the same kind outside a repo, such as `basis-job`, which makes one."""

import argparse
import io
import json
import logging
import os
import sys
import tomllib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from importlib.metadata import EntryPoint, entry_points, version
from pathlib import Path
from typing import Literal, NoReturn, override

from pydantic import ValidationError

from basis.core import check, hooks, logs, settings, sync
from basis.core.errors import BasisError
from basis.core.verbs import Part, Runs, Shared, Verb, fail

GROUP = "basis.verbs"
PACKAGES = {"job": "basis-job", "api": "basis-api", "optim": "optim"}
"""The package each part comes in, for the fix when a repo declares a part that is not installed."""
CORE = ("check", "new", "sync", "docs")
"""Core's verbs, which no part may add: in the order `basis --help` lists them. `check` and `sync` are always listed,
since every repo has basis's own files, and each of the others when a declared part gives it something to do."""


@dataclass(frozen=True)
class Spec:
    """How core parses one of the verbs the parts that run things share, before the part that runs NAME reads the
    rest."""

    name: str
    """NAME's help, with `{noun}` for what the parts run, such as `job`, and `{path}` where a module path would do."""
    required: bool = False
    run_id: bool = False
    """Whether RUN_ID follows NAME."""
    alone: Literal["refuse", "each", "first", "names"] = "refuse"
    """What the verb does without NAME, where more than one declared part has it: refuse, hand the arguments to each
    part in turn, hand them to the first, or print the name of everything every part runs."""


SHARED = {
    "run": Spec("the {noun}'s name, as the repo lists it{path}", required=True),
    "describe": Spec("the {noun} to describe{path}; without it, every one the repo lists, by name", alone="names"),
    "resume": Spec("the {noun} whose run it is; without it, the run is found by its id", run_id=True),
    "replay": Spec("the {noun} whose run it is; without it, the run is found by its id", run_id=True),
    "regress": Spec("the {noun} whose runs to replay; without it, every one", alone="each"),
    "ddl": Spec("the {noun} whose tables to print{path}", alone="first"),
}
"""The verbs that jobs and models both have, which are core's, in the order `basis --help` lists them; each is listed
when a declared part's `Runs` has it."""

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
        for part in repo.parts.values():
            if part.renamed is not None:
                part.renamed(os.environ)
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
    if unknown := sorted(set(table) - {"parts", sync.APT}):
        msg = f"{pyproject}'s [tool.basis] has {', '.join(unknown)}, which basis does not read: remove it; basis reads parts, as {example}, and {sync.APT}"
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
    """Core's verbs, the verbs the parts that run things share, and each declared part's own, as `basis --help` lists
    them; a group with no verbs is left out."""
    owners = dict.fromkeys((*CORE, *SHARED), _CORE)
    for name, part in repo.parts.items():
        for verb in part.verbs:
            _claim(owners, verb.name, name, f"basis {verb.name}")
    running = [runs for runs in (part.runs for part in repo.parts.values()) if runs is not None]
    groups = [("In every basis repo", _core(repo)), (f"For its {' and '.join(f'{runs.noun}s' for runs in running)}", _shared(repo))]
    groups += [(f"From the {name} part", list(part.verbs)) for name, part in repo.parts.items()]
    return [(title, verbs) for title, verbs in groups if verbs]


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

    def agent_files() -> list[sync.AgentFiles]:
        return [read() for read in files]

    def run_check(n: argparse.Namespace) -> int:
        args = CheckArgs(staged=bool(n.staged))
        return check.check(repo.root, agent_files(), run=check.run_command, staged=args.staged)

    def run_sync(n: argparse.Namespace) -> int:
        return _sync(repo.root, agent_files(), SyncArgs(adopt=bool(n.adopt)))

    offered = {
        "check": Verb("check", "lint, format check, type check and test the repo, and check its framework files and hooks", _check_arguments, run_check),
        "new": Verb("new", f"add a {' or a '.join(kinds)} to the repo", partial(_new_arguments, kinds), lambda n: _new(repo, kinds[str(n.kind)], str(n.name))),
        "sync": Verb("sync", "rewrite the repo's framework and agent files from the installed version", _sync_arguments, run_sync),
        "docs": Verb("docs", "list the docs of the installed parts, or print one", _docs_arguments, lambda n: _docs(_pages(repo), n.page)),
    }
    given = {"check", "sync"}.union(*map(_gives, parts))
    return [offered[name] for name in CORE if name in given]


def _gives(part: Part) -> set[str]:
    """The core verbs `part` gives something to do, the shared verbs among them."""
    fields = {"docs": part.docs is not None, "new": bool(part.new)}
    shared: set[str] = set(_sides(part.runs)) if part.runs is not None else set()
    return {name for name, given in fields.items() if given} | shared


def _sides(runs: Runs) -> dict[str, Shared]:
    """The part's side of each verb in `SHARED` it has, by the verb's name."""
    sides = {"run": runs.run, "describe": runs.describe, "resume": runs.resume, "replay": runs.replay, "regress": runs.regress, "ddl": runs.ddl}
    return {name: side for name, side in sides.items() if side is not None}


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


@dataclass(frozen=True)
class _Dispatched(Verb):
    """A verb in `SHARED`, which reads its own command line: which part's parser reads it depends on its NAME, and
    argparse cannot hand on a command line whose first argument is an option."""

    dispatch: Callable[[Sequence[str]], int]


def _shared(repo: Repo) -> list[Verb]:
    """Each verb in `SHARED` a declared part has, dispatched to the parts that have it."""
    found: list[Verb] = []
    for name, spec in SHARED.items():
        sides = {part: (runs, side) for part, runs in _running(repo).items() if (side := _sides(runs).get(name)) is not None}
        if not sides:
            continue
        dispatch = partial(_dispatch, name, spec, sides)
        found.append(_Dispatched(name, _shared_help(sides), _rest, partial(_from_rest, dispatch), dispatch))
    return found


def _shared_help(sides: Mapping[str, tuple[Runs, Shared]]) -> str:
    """A shared verb's line in `basis --help`: the only part's help, or each part's, after what it runs."""
    if len(sides) == 1:
        return next(iter(sides.values()))[1].help
    return " | ".join(f"{runs.noun}s: {side.help}" for runs, side in sides.values())


def _running(repo: Repo) -> dict[str, Runs]:
    return {name: part.runs for name, part in repo.parts.items() if part.runs is not None}


def _rest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)


def _from_rest(dispatch: Callable[[Sequence[str]], int], namespace: argparse.Namespace) -> int:
    """A shared verb that argparse parsed, as when an option comes before it: `_execute` hands on every other."""
    return dispatch(namespace.rest)


type Sides = Mapping[str, tuple[Runs, Shared]]
"""Each part that has a shared verb, by name, with what it runs and its side of the verb."""


def _dispatch(verb: str, spec: Spec, sides: Sides, argv: Sequence[str]) -> int:
    """`basis VERB ARGV`: handed to the only part that has the verb, or to the one that runs NAME, whose parser reads
    every argument; without NAME, where several parts have it, as `spec.alone` says."""
    if len(sides) == 1:
        return _hand(verb, spec, sides, next(iter(sides)), argv)
    leading = next((index for index, argument in enumerate(argv) if argument.startswith("-")), len(argv))
    if leading >= (2 if spec.run_id else 1):  # NAME is given, and comes first
        return _hand(verb, spec, sides, _owner(sides, argv[0]), argv)
    if spec.run_id and leading == 1 and any(argv[0] in runs.names() for runs, _ in sides.values()):
        msg = f"basis {verb} {argv[0]} takes the id of one of its runs after the name: give it, as `basis {verb} {argv[0]} RUN_ID`"
        raise CommandError(msg, code=2)
    if "-h" in argv or "--help" in argv:
        return _help(verb, spec, sides)
    nouns = " or ".join(f"{runs.noun}'s" for runs, _ in sides.values())
    match spec.alone:
        case "names":
            return _print(_names(sides))
        case "each":
            return max(_each(verb, spec, sides, part, argv) for part in sides)
        case "first":
            return _hand(verb, spec, sides, next(iter(sides)), argv)
        case "refuse":
            where = "before the run's id" if spec.run_id else "first"
            msg = f"more than one part of this repo has `basis {verb}` ({', '.join(sides)}): give the {nouns} name {where}"
            raise CommandError(msg)


def _hand(verb: str, spec: Spec, sides: Sides, part: str, argv: Sequence[str]) -> int:
    """`argv`, parsed by `part`'s parser for `verb`: core's NAME and RUN_ID, and the part's own arguments."""
    runs, side = sides[part]
    parser = _Parser(prog=f"basis {verb}", description=side.help)
    metavar, path = (runs.noun.upper(), ", or its module path") if len(sides) == 1 else ("NAME", "")
    parser.add_argument("name", metavar=metavar, nargs=None if spec.required else "?", help=spec.name.format(noun=runs.noun, path=path))
    if spec.run_id:
        parser.add_argument("run_id", metavar="RUN_ID")
    side.arguments(parser)
    try:
        namespace = parser.parse_args(argv)
    except SystemExit as exited:  # --help prints, then exits
        return exited.code if isinstance(exited.code, int) else 0
    if namespace.name is None and spec.alone == "names":
        return _print(_names(sides))
    return side.handle(namespace)


def _each(verb: str, spec: Spec, sides: Sides, part: str, argv: Sequence[str]) -> int:
    """`part`'s side of `verb`, whose failure is its own line, so that every other part's runs too."""
    try:
        return _hand(verb, spec, sides, part, argv)
    except Exception as error:  # noqa: BLE001 -- each part's failure is its own line, and the other parts still run
        error.add_note(f"in the {part} part")
        return report(error)


def _help(verb: str, spec: Spec, sides: Sides) -> int:
    """What `basis VERB --help` prints where several parts have the verb, before NAME says whose arguments to give."""
    usage = f"basis {verb} [-h] {'NAME' if spec.required or spec.run_id else '[NAME]'}{' RUN_ID' if spec.run_id else ''} ..."
    lines = [f"With a {runs.noun}'s NAME, it does as the {part} part says: {side.help}." for part, (runs, side) in sides.items()]
    lines.append(f"`basis {verb} NAME --help` gives the arguments of NAME's part.")
    sys.stdout.write(f"usage: {usage}\n\n" + "\n".join(lines) + "\n")
    return 0


def _owner(sides: Sides, name: str) -> str:
    """The part that runs `name`, of those in `sides`."""
    owners = [part for part, (runs, _) in sides.items() if name in runs.names()]
    if len(owners) == 1:
        return owners[0]
    if owners:
        msg = f"the {' and '.join(owners)} parts both run something named {name!r}: rename one of them"
        raise CommandError(msg)
    names = sorted({found for runs, _ in sides.values() for found in runs.names()})
    nouns = " or ".join(f"{runs.noun}s" for runs, _ in sides.values())
    msg = f"nothing in this repo is named {name!r}: give one of {', '.join(names) or f'its {nouns}, once it has some'}"
    raise CommandError(msg)


def _names(sides: Sides) -> dict[str, str]:
    """Everything the parts in `sides` run, by name, with its module path."""
    found: dict[str, str] = {}
    for runs, _ in sides.values():
        for name, path in runs.names().items():
            if name in found:
                _owner(sides, name)  # refuses the name two parts run
            found[name] = path
    return found


def _print(value: object) -> int:
    sys.stdout.write(json.dumps(value, indent=2) + "\n")
    return 0


def _new(repo: Repo, make: Callable[[Path, str], Path], name: str) -> int:
    log.info("made %s", make(repo.root, name))
    hooks.ensure(repo.root)
    return 0


@dataclass(frozen=True)
class CheckArgs:
    staged: bool
    """Whether to check the files staged for a commit as well, as the commit hook does."""


@dataclass(frozen=True)
class SyncArgs:
    adopt: bool
    """Whether to replace each file of the repo's own at a path basis writes, after saving it as NAME.before-basis."""


def _sync(root: Path, files: Sequence[sync.AgentFiles], args: SyncArgs) -> int:
    done = sync.write(root, files, adopt=args.adopt)
    for path in done.changed:
        log.info("synced %s", path)
    for path in done.adopted:
        log.info("adopted %s, and saved what it held as %s%s, for you to compare and delete", path, path.name, sync.BEFORE)
    hooks.ensure(root)  # once the hooks are written
    return 1 if sum(fail(*problem) for problem in done.problems()) else 0


def _pages(repo: Repo) -> dict[str, Path]:
    """Every declared part's docs, by `PART/PAGE`."""
    return {f"{name}/{path.relative_to(part.docs).with_suffix('').as_posix()}": path for name, part in repo.parts.items() if part.docs is not None for path in sorted(part.docs.rglob("*.md"))}


def _docs(pages: Mapping[str, Path], page: str | None) -> int:
    """Every page's name, or the page `page` names, as `PART/PAGE` or as `PAGE` where only one part has it."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # a page is UTF-8, and a redirected Windows console is cp1252
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


def _check_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--staged", action="store_true", help="also check the files staged for a commit, as the commit hook does: no private key, none over 500 KB, YAML and TOML that parse")


def _sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adopt", action="store_true", help="replace each file of the repo's own at a path basis writes, after saving it as NAME.before-basis")


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


def positive(text: str) -> int:
    """A count of at least 1, as argparse's `type`."""
    if not text.isdecimal() or int(text) < 1:
        msg = f"give a whole number of 1 or more, not {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return int(text)


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
    if argv and isinstance(verb := verbs.get(argv[0]), _Dispatched):
        return verb.dispatch(argv[1:])
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
