"""The `basis` command, which basis-core provides and each part adds its verbs to, as `basis.core.verbs` describes.

It finds its repo, the nearest folder at or above the working directory with a `pyproject.toml`, reads the parts that
file declares as `[tool.basis] parts = ["job"]`, loads each one's entry point in the `basis.verbs` group, and runs
one verb: a part's own, or one of core's, which reach the parts through their `Part`. A verb that every part that runs
things by name has, such as `run`, is core's too (`SHARED`): it parses NAME, and RUN_ID where the verb takes one, and
the part that runs NAME parses the rest. A verb of a part the repo does not declare is refused, never offered. The verbs a
developer runs on a clone, `check`, `sync` and `new`, point a clone that names no hooks folder at the repo's hooks
(`basis.core.hooks.ensure`); `run` and the rest, which a container or a scheduler runs, leave git alone.

Every command ends with one log record, `finished VERB`, with its `exit_code` and `ms`, whatever its outcome, and then
each failure's line on stderr, `ERROR: REASON: FIX`, held back until then (`basis.core.verbs.holding`): so a failure's
ERROR line is the last line, and monitoring finds every exit code. The exit code is 1, or 2 for a command line that
does not parse. A `BasisError`, which basis raises on purpose with the fix in its message, prints as that line alone,
as does a pydantic `ValidationError` of the caller's settings or params; any other exception is a defect, and its
traceback is logged before the line, as every exception's is with `BASIS_LOG_LEVEL=DEBUG`. `standalone` runs a
command of the same kind outside a repo, such as `basis-job`, which makes one. `basis doctor` and `basis --version`
run outside a repo too."""

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property, partial
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Literal, NoReturn, override

from pydantic import ValidationError

from basis.core import check, ddl, doctor, environments, full, gates, hooks, invalid, logs, qa, review, settings, sync, upgrade
from basis.core.destination import Dialect
from basis.core.errors import BasisError
from basis.core.logs import utc_now
from basis.core.verbs import GROUP, Part, ProjectError, Runs, Shared, Verb, basis_table, error_line, fail, holding, not_a_table

CORE = ("check", "review", "new", "sync", "upgrade", "doctor", "docs", "smoke")
"""Core's verbs, which no part may add: in the order `basis --help` lists them. `check`, `review`, `sync`, `upgrade`
and `doctor` are always listed, since every repo has basis's own files and gates, and each of the others when a
declared part gives it something to do."""
ALWAYS = frozenset({"check", "review", "sync", "upgrade", "doctor"})


@dataclass(frozen=True)
class Spec:
    """How core parses one of the verbs the parts that run things share, before the part that runs NAME reads the
    rest."""

    name: str
    """NAME's help, with `{noun}` for what the parts run, such as `job`, and `{path}` where a module path would do."""
    required: bool = False
    run_id: bool = False
    """Whether RUN_ID follows NAME."""
    env: bool = False
    """Whether the verb takes `--env`, since it reaches the firm's systems: the part reads its settings from the
    environment it names (`basis.core.environments`)."""
    alone: Literal["refuse", "each", "ledger", "names"] = "refuse"
    """What the verb does without NAME, where more than one declared part has it: refuse; hand the arguments, which
    must be options every part takes, to each part in turn, and print what each part printed as JSON, by part; print
    the ledger's CREATE TABLE, which is core's, whichever part publishes; or print the name of everything every part
    runs."""


SHARED = {
    "run": Spec("the {noun}'s name, as the repo lists it{path}", required=True, env=True),
    "describe": Spec("the {noun} to describe{path}; without it, every one the repo lists, by name", alone="names"),
    "resume": Spec("the {noun} whose run it is; without it, the run is found by its id", run_id=True, env=True),
    "replay": Spec("the {noun} whose run it is; without it, the run is found by its id", run_id=True, env=True),
    "regress": Spec("the {noun} whose runs to replay; without it, every one", alone="each", env=True),
    "ddl": Spec("the {noun} whose tables to print{path}", alone="ledger"),
}
"""The verbs of the parts that run things by name, such as jobs, which are core's, in the order `basis --help` lists
them; each is listed when a declared part's `Runs` has it."""

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
    return _finishing("basis", argv, partial(_run, argv, cwd=cwd, installed=installed))


def _run(argv: Sequence[str], *, cwd: Path, installed: Installed) -> int:
    try:
        logs.setup()
        if list(argv[:1]) == ["--version"]:  # which needs no repo
            sys.stdout.write(f"{versions()}\n")
            return 0
        if list(argv[:1]) == ["doctor"]:  # which runs outside a repo too, and says what is wrong with one
            found, unread = _for_doctor(cwd, installed)
            if found is None:
                return _doctor_outside(argv, unread)
            repo = found
        elif list(argv[:1]) == ["docs"] and _for_doctor(cwd, installed) == (None, None):
            return _docs_outside(argv, installed)  # outside a basis repo, such as a ctzd app, every installed part's docs, which write nothing
        else:
            settings.refuse_renamed(os.environ)
            repo = find(cwd, installed)
            for part in repo.parts.values():
                if part.renamed is not None:
                    part.renamed(os.environ)
        groups = _groups(repo)
        verbs = {verb.name: verb for _, group in groups for verb in group}
        _refuse_undeclared(argv, repo, verbs, installed)
        parser = _parser("basis", _describe(repo, groups), verbs.values())
        parser.set_defaults(repo_root=repo.root)  # for a handler that needs the repo, as `basis.core.verbs` says, so it never looks for it again
        return _execute(parser, verbs, argv)
    except Exception as error:  # noqa: BLE001 -- the boundary where every failure becomes its one line
        return report(error)


def standalone(prog: str, description: str, verbs: Sequence[Verb], argv: Sequence[str] | None = None) -> int:
    """A command with `verbs` that runs outside a repo, such as `basis-job`, which makes one; it parses, runs and
    fails as `basis` does."""
    given = sys.argv[1:] if argv is None else argv

    def execute() -> int:
        try:
            logs.setup()
            return _execute(_parser(prog, description, verbs), {verb.name: verb for verb in verbs}, given)
        except Exception as error:  # noqa: BLE001 -- the boundary where every failure becomes its one line
            return report(error)

    return _finishing(prog, given, execute)


def _finishing(prog: str, argv: Sequence[str], execute: Callable[[], int]) -> int:
    """`execute`'s exit code, `prog ARGV`'s, after the record that says it finished, with its verb, its exit code and
    how long it took, and then the ERROR lines it printed, so that they are last."""
    start = time.perf_counter()
    with holding() as held:
        code = execute()
    verb = next((argument for argument in argv if not argument.startswith("-")), None)
    log.info("finished %s", verb or prog, extra={"verb": verb, "exit_code": code, "ms": logs.ms_since(start)})
    sys.stderr.write("".join(held))
    return code


def versions() -> str:
    """basis-core's version and each installed part's package's, as `--version` prints them."""
    return ", ".join(f"{name} {number}" for name, number in review.versions().items())


def _nearest(cwd: Path) -> Path | None:
    """The `pyproject.toml` in `cwd` or the nearest folder above it, if any."""
    return next((folder / "pyproject.toml" for folder in (cwd, *cwd.parents) if (folder / "pyproject.toml").is_file()), None)


def find(cwd: Path, installed: Installed) -> Repo:
    """The repo at or above `cwd`, with each part it declares loaded."""
    pyproject = _nearest(cwd)
    if pyproject is None:
        msg = f"no pyproject.toml in {cwd} or a folder above it, so it is in no basis repo: run basis in one, or make one, as `uvx basis-job new repo NAME` does"
        raise CommandError(msg)
    parts: dict[str, Part] = {}
    for name in declared(pyproject):
        if name not in installed:
            msg = (
                f"{pyproject} declares the part {name!r}, which no installed package provides: add the package whose {GROUP} entry point is named {name!r}"
                " to its [project] dependencies, and run `uv sync --all-packages`"
            )
            raise CommandError(msg)
        parts[name] = installed[name]()
    return Repo(pyproject, parts)


def declared(pyproject: Path) -> list[str]:
    """The parts `pyproject` declares, as `[tool.basis] parts = ["job"]`. A part is never inferred from a folder. A
    declared part may have a table of its own, `[tool.basis.PART]`, which the part reads (`verbs.table`); one for a
    part the repo does not declare is refused."""
    try:
        table = basis_table(pyproject)
    except ProjectError as error:
        raise CommandError(str(error)) from error
    example = '[tool.basis] parts = ["job"], naming each part the repo has, such as job or api'
    if table is None:
        msg = f"{pyproject} declares no basis parts: add {example}"
        raise CommandError(msg)
    names = table.get("parts")
    listed = {str(name) for name in names} if isinstance(names, list) else set[str]()
    unknown = sorted(set(table) - {"parts", sync.APT, gates.KEY, qa.KEY} - listed)
    if unread := [key for key in unknown if not isinstance(table[key], dict)]:
        reads = f"parts, as {example}, {sync.APT}, {gates.KEY}, {qa.KEY}, and a table of each declared part's own, as [tool.basis.job]"
        msg = f"{pyproject}'s [tool.basis] has {', '.join(unread)}, which basis does not read: remove it; basis reads {reads}"
        raise CommandError(msg)
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        msg = f"{pyproject}'s [tool.basis] parts is not a list of part names: give {example}"
        raise CommandError(msg)
    found = [str(name) for name in names]
    if len(set(found)) != len(found):
        msg = f"{pyproject}'s [tool.basis] parts names a part twice: name each once"
        raise CommandError(msg)
    if unknown:  # tables, since a key that is no table was refused above: each a part's, for a part the repo does not declare
        part = unknown[0]
        msg = f"{pyproject} has [tool.basis.{part}], a part's own table, and its [tool.basis] parts does not declare the {part} part: add {part!r} to its parts, or remove the table"
        raise CommandError(msg)
    for part in found:
        if part in table and not isinstance(table[part], dict):
            raise CommandError(not_a_table(pyproject, part))
    return found


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
        args = CheckArgs(staged=bool(n.staged), full=bool(n.full), settings_from=n.settings_from, with_qa=not n.no_qa)
        if not args.full:
            if args.settings_from is not None or not args.with_qa:
                return fail("--settings-from and --no-qa change what `basis check --full` does, and were given without --full", "give --full too, or leave them out")
            return check.check(repo.root, agent_files(), run=check.run_command, staged=args.staged, checks=check.checks_of(repo.parts))
        if args.staged:
            return fail("--staged checks a commit's staged files, and --full a commit being pushed", "give one of them")
        folder = args.settings_from.resolve() if args.settings_from is not None else repo.root
        return full.full(repo.root, agent_files(), repo.parts, run=check.run_command, report=reported, folder=folder, clock=utc_now, with_qa=args.with_qa)

    def run_sync(n: argparse.Namespace) -> int:
        return _sync(repo.root, agent_files(), SyncArgs(adopt=bool(n.adopt)))

    offered = {
        "check": Verb("check", "lint, format check, type check and test the repo, and check its framework files and hooks; --full adds regress and the QA tier", _check_arguments, run_check),
        "review": Verb("review", "print the pull request's summary of what basis check --full found on a commit, as Markdown", _review_arguments, lambda n: review.review(repo.root, str(n.commit))),
        "upgrade": Verb("upgrade", "relock basis's packages, sync, check in full, and print what changed", _no_arguments, lambda _: _upgrade(repo)),
        "doctor": Verb("doctor", DOCTOR, _doctor_arguments, lambda n: _diagnose(repo, None, n.env)),
        "new": Verb("new", f"add a {' or a '.join(kinds)} to the repo", partial(_new_arguments, kinds), lambda n: _new(repo, kinds[str(n.kind)], str(n.name))),
        "sync": Verb("sync", "rewrite the repo's framework and agent files from the installed version", _sync_arguments, run_sync),
        "docs": Verb("docs", "list the docs of the installed parts, or print one", _docs_arguments, lambda n: _docs(_pages(repo.parts), n.page)),
        "smoke": Verb("smoke", SMOKE, _smoke_arguments, lambda n: _smoke(repo, str(n.env), n.commit)),
    }
    given = ALWAYS.union(*map(_gives, parts))
    return [offered[name] for name in CORE if name in given]


def _gives(part: Part) -> set[str]:
    """The core verbs `part` gives something to do, the shared verbs among them."""
    fields = {"docs": part.docs is not None, "new": bool(part.new), "smoke": part.smoke is not None}
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
    offering: list[str] = []
    for name, load in installed.items():
        if name in repo.parts:
            continue
        try:
            part = load()
        except Exception:  # a part that does not load offers no verb; argparse refuses the verb instead
            log.debug("the %s part does not load", name, exc_info=True)
            continue
        if verb in _offers(part):
            offering.append(name)
    if len(offering) == 1:
        msg = f"`basis {verb}` comes with the {offering[0]} part, which {repo.pyproject} does not declare: add {offering[0]!r} to its [tool.basis] parts"
        raise CommandError(msg)
    if offering:
        parts = " and ".join(offering)
        msg = f"`basis {verb}` comes with the {parts} parts, which {repo.pyproject} does not declare: add the one the repo has, {' or '.join(map(repr, offering))}, to its [tool.basis] parts"
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
    """A verb in `SHARED`, which reads its own command line, since which part's parser reads it depends on its NAME:
    `_execute` hands it the arguments after the verb."""

    dispatch: Callable[[Sequence[str]], int]


def _shared(repo: Repo) -> list[Verb]:
    """Each verb in `SHARED` a declared part has, dispatched to the parts that have it."""
    found: list[Verb] = []
    for name, spec in SHARED.items():
        sides = {part: (runs, side) for part, runs in _running(repo).items() if (side := _sides(runs).get(name)) is not None}
        if sides:
            sharing = _Sharing(name, spec, sides, repo.root)
            found.append(_Dispatched(name, sharing.help(), _no_arguments, partial(_dispatched, sharing.dispatch), sharing.dispatch))
    return found


def _no_arguments(_: argparse.ArgumentParser) -> None:
    """Adds no arguments: for a verb that takes none, and for a shared verb in the parser of every verb, since its
    parts' parsers read them."""


def _dispatched(dispatch: Callable[[Sequence[str]], int], _: argparse.Namespace) -> int:
    """A shared verb that argparse reached with nothing after it, as in `basis -- describe`."""
    return dispatch(())


def _running(repo: Repo) -> dict[str, Runs]:
    return {name: part.runs for name, part in repo.parts.items() if part.runs is not None}


@dataclass(frozen=True)
class _Sharing:
    """A verb in `SHARED` as the declared parts that have it share it."""

    verb: str
    spec: Spec
    sides: Mapping[str, tuple[Runs, Shared]]
    """Each part that has the verb, by name, with what it runs and its side of the verb."""
    root: Path
    """The repo's folder, which every handler finds as `repo_root`, as a part's own verbs' do."""

    @cached_property
    def names(self) -> dict[str, Mapping[str, str]]:
        """What each part runs, by name, with its module path: read once per command, when it is needed."""
        return {part: runs.names() for part, (runs, _) in self.sides.items()}

    def help(self) -> str:
        """The verb's line in `basis --help`: the only part's help, or each part's, after what it runs."""
        if len(self.sides) == 1:
            return next(iter(self.sides.values()))[1].help
        return " | ".join(f"{runs.noun}s: {side.help}" for runs, side in self.sides.values())

    def dispatch(self, argv: Sequence[str]) -> int:
        """`basis VERB ARGV`: handed to the only part that has the verb, or to the one that runs NAME, whose parser
        reads every argument; without NAME, where several parts have it, as `spec.alone` says."""
        if len(self.sides) == 1:
            return self._hand(next(iter(self.sides)), argv)
        leading = next((index for index, argument in enumerate(argv) if argument.startswith("-")), len(argv))
        helping = "-h" in argv or "--help" in argv
        known = leading >= 1 and any(argv[0] in found for found in self.names.values())
        if leading >= (2 if self.spec.run_id else 1) or (known and helping):  # NAME is given, and comes first
            return self._hand(self._owner(argv[0]), argv)
        if self.spec.run_id and known:
            msg = f"basis {self.verb} {argv[0]} takes the id of one of its runs after the name: give it, as `basis {self.verb} {argv[0]} RUN_ID`"
            raise CommandError(msg, code=2)
        if helping:
            return self._help()
        match self.spec.alone:
            case "names":
                return _print(self._listed())
            case "each":
                return self._every_part(argv)
            case "ledger":
                return _ledger(argv)
            case "refuse":
                where = "before the run's id" if self.spec.run_id else "first"
                msg = f"more than one part of this repo has `basis {self.verb}` ({', '.join(self.sides)}): give the {self._nouns("'s")} name {where}"
                raise CommandError(msg)

    def _parser(self, part: str) -> argparse.ArgumentParser:
        """`part`'s parser for the verb: core's NAME and RUN_ID, and the part's own arguments."""
        runs, side = self.sides[part]
        parser = _Parser(prog=f"basis {self.verb}", description=side.help)
        parser.set_defaults(repo_root=self.root)
        metavar, path = (runs.noun.upper(), ", or its module path") if len(self.sides) == 1 else ("NAME", "")
        parser.add_argument("name", metavar=metavar, nargs=None if self.spec.required else "?", help=self.spec.name.format(noun=runs.noun, path=path))
        if self.spec.run_id:
            parser.add_argument("run_id", metavar="RUN_ID")
        if self.spec.env:
            environment_argument(parser)
        side.arguments(parser)
        return parser

    def _hand(self, part: str, argv: Sequence[str]) -> int:
        """`argv`, parsed by `part`'s parser, handed to `part`; for `describe` without NAME, what every part runs."""
        try:
            namespace = self._parser(part).parse_args(argv)
        except SystemExit as exited:  # --help prints, then exits
            return exited.code if isinstance(exited.code, int) else 0
        if namespace.name is None and self.spec.alone == "names":
            return _print(self._listed())
        return self.sides[part][1].handle(namespace)

    def _every_part(self, argv: Sequence[str]) -> int:
        """`basis VERB ARGV` without NAME, handed to every part in turn: with only the options every part takes, since
        one part's would fail the others, and with each part's result, which it prints as JSON, printed as one JSON
        object, by part. A part's failure is its own line, and every other part still runs."""
        for part in self.sides:
            _, unknown = self._parser(part).parse_known_args(argv)
            if options := [argument.partition("=")[0] for argument in unknown if argument.startswith("-")]:
                listed = ", ".join(options)
                msg = f"basis {self.verb}: {listed} is not an option of the {part} part's `basis {self.verb}`: name the {self._nouns()} it is for, as `basis {self.verb} NAME {options[0]} ...`"
                raise CommandError(msg, code=2)
        results: dict[str, object] = {}
        code = 0
        for part in self.sides:
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = max(code, self._each(part, argv))
            results[part] = self._parsed(part, printed.getvalue())
        _print(results)
        return code

    def _each(self, part: str, argv: Sequence[str]) -> int:
        """`part`'s side of the verb, whose failure is its own line, so that every other part's runs too."""
        try:
            return self._hand(part, argv)
        except Exception as error:  # noqa: BLE001 -- each part's failure is its own line, and the other parts still run
            error.add_note(f"in the {part} part")
            return report(error)

    def _parsed(self, part: str, printed: str) -> object:
        """What `part`'s side of the verb printed, as JSON, or None where it printed nothing."""
        if not printed.strip():
            return None
        try:
            return json.loads(printed)
        except json.JSONDecodeError as error:
            msg = f"the {part} part's `basis {self.verb}` printed what is not JSON, which it must print where several parts run it: {error}"
            raise ValueError(msg) from error

    def _help(self) -> int:
        """What `basis VERB --help` prints where several parts have the verb, before NAME says whose arguments to give."""
        spec = self.spec
        usage = f"basis {self.verb} [-h] {'NAME' if spec.required or spec.run_id else '[NAME]'}{' RUN_ID' if spec.run_id else ''} ..."
        lines = [f"With a {runs.noun}'s NAME, it does as the {part} part says: {side.help}." for part, (runs, side) in self.sides.items()]
        lines.append(f"`basis {self.verb} NAME --help` gives the arguments of NAME's part.")
        sys.stdout.write(f"usage: {usage}\n\n" + "\n".join(lines) + "\n")
        return 0

    def _owner(self, name: str) -> str:
        """The part that runs `name`."""
        owners = [part for part, found in self.names.items() if name in found]
        if len(owners) == 1:
            return owners[0]
        if owners:
            msg = f"the {' and '.join(owners)} parts both run something named {name!r}: rename one of them"
            raise CommandError(msg)
        listed = sorted({found for each in self.names.values() for found in each})
        msg = f"nothing in this repo is named {name!r}: give one of {', '.join(listed) or f'its {self._nouns("s")}, once it has some'}"
        raise CommandError(msg)

    def _listed(self) -> dict[str, str]:
        """Everything the parts run, by name, with its module path; a name two parts run is refused."""
        found: dict[str, str] = {}
        for each in self.names.values():
            for name, path in each.items():
                if name in found:
                    self._owner(name)  # refuses it
                found[name] = path
        return found

    def _nouns(self, suffix: str = "") -> str:
        """What the parts run, such as `job`, joined by `or`, each with `suffix`."""
        return " or ".join(f"{runs.noun}{suffix}" for runs, _ in self.sides.values())


def _print(value: object) -> int:
    sys.stdout.write(json.dumps(value, indent=2) + "\n")
    return 0


def _upgrade(repo: Repo) -> int:
    packages = set(review.versions())  # basis-core and the package of each installed part, whatever its name

    def write(line: str) -> None:
        sys.stdout.write(f"{line}\n")

    return upgrade.upgrade(repo.root, packages, run=check.run_command, write=write)


SMOKE = "check a deployed environment, after each deploy and from a desktop: an API's document, readiness and examples, and a job's dry run"


def _smoke_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", metavar="ENV", choices=environments.NAMES, required=True, help=f"{', '.join(environments.NAMES)}: the environment to check")
    parser.add_argument("--commit", metavar="SHA", help="the commit the environment must serve, as a pipeline that deployed it checks")


def _smoke(repo: Repo, env: str, commit: str | None) -> int:
    """`basis smoke --env ENV [--commit SHA]`: each declared part's checks of the environment, each problem one `ERROR`
    line."""
    failures = 0
    for name, part in repo.parts.items():
        if part.smoke is None:
            continue
        problems = list(part.smoke(repo.root, env, commit))
        failures += sum(fail(*problem) for problem in problems)
        if not problems:
            log.info("the %s part's smoke checks of %s passed", name, env, extra={"part": name, "env": env})
    return 1 if failures else 0


DOCTOR = "check this desktop's uv, git, hooks, feed and settings, and with --env an environment's logins and run store, with the fix for each problem"


def _doctor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", metavar="ENV", choices=environments.NAMES, help=f"{', '.join(environments.NAMES)}: also check that environment's settings, each database's logins and its run store")


def _diagnose(repo: Repo | None, unread: str | None, env: str | None = None) -> int:
    """`basis doctor`: the desktop, and the repo when it runs in one; `unread` says why the folder's `pyproject.toml`
    is a basis repo's that basis cannot read."""
    return doctor.doctor(repo.root if repo is not None else None, repo.parts if repo is not None else {}, doctor.Desktop.here(), describe=one_line, unread=unread, env=env)


def _for_doctor(cwd: Path, installed: Installed) -> tuple[Repo | None, str | None]:
    """The basis repo at or above `cwd`, or None, with why basis cannot read it when it is one. A folder in no
    project, or in an ordinary Python project whose `pyproject.toml` has no `[tool.basis]`, is no basis repo, and
    doctor checks the desktop alone there."""
    if (pyproject := _nearest(cwd)) is None:
        return None, None
    try:
        return find(cwd, installed), None
    except CommandError as error:
        try:
            declares = basis_table(pyproject) is not None
        except ProjectError:
            return None, str(error)
        return None, str(error) if declares else None


def _doctor_outside(argv: Sequence[str], unread: str | None) -> int:
    """`basis doctor` where there is no basis repo basis can read, which parses as it does in one."""
    verb = Verb("doctor", DOCTOR, _doctor_arguments, lambda n: _diagnose(None, unread, n.env))
    return _execute(_parser("basis", "Check this desktop's setup for basis.", [verb]), {"doctor": verb}, argv)


def _docs_outside(argv: Sequence[str], installed: Installed) -> int:
    """`basis docs` where there is no basis repo: the pages of every installed part, which parses as it does in one."""
    pages = _pages({name: load() for name, load in installed.items()})
    verb = Verb("docs", "list the docs of the installed parts, or print one", _docs_arguments, lambda n: _docs(pages, n.page))
    return _execute(_parser("basis", "Read the docs of basis's installed parts.", [verb]), {"docs": verb}, argv)


def _new(repo: Repo, make: Callable[[Path, str], Path], name: str) -> int:
    log.info("made %s", make(repo.root, name))
    hooks.ensure(repo.root)
    return 0


@dataclass(frozen=True)
class CheckArgs:
    staged: bool
    """Whether to check the files staged for a commit as well, as the commit hook does."""
    full: bool
    """Whether to add regress, the QA tier and the contract diffs, as the push hook does."""
    settings_from: Path | None
    """The clone's own folder, where the files git does not track are, for a check in another worktree."""
    with_qa: bool
    """Whether `--full` runs the QA tier, which `basis upgrade` leaves to the push, since its relock is not yet a
    commit."""


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


def _pages(parts: Mapping[str, Part]) -> dict[str, Path]:
    """Every part's docs, by `PART/PAGE`."""
    return {f"{name}/{path.relative_to(part.docs).with_suffix('').as_posix()}": path for name, part in parts.items() if part.docs is not None for path in sorted(part.docs.rglob("*.md"))}


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
    parser.add_argument(
        "--full", action="store_true", help="also regress where the run store is reachable, and run the QA tier where the repo declares one and this machine has its settings: what the push hook runs"
    )
    parser.add_argument(
        "--settings-from",
        type=existing_folder,
        metavar="DIR",
        help="with --full, read a relative BASIS_SNAPSHOT_ROOT from DIR, the clone's own folder: for a commit the push hook checks in a worktree of its own",
    )
    parser.add_argument("--no-qa", action="store_true", help="with --full, leave the QA tier to the push, as basis upgrade does for a relock not yet committed")


def _review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commit", default="HEAD", metavar="REF", help="the commit to summarize, which basis check --full has checked (default: HEAD)")


def _sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adopt", action="store_true", help="replace each file of the repo's own at a path basis writes, after saving it as NAME.before-basis")


def environment_argument(parser: argparse.ArgumentParser) -> None:
    """`--env ENV`, which every verb that reaches the firm's systems takes, and gives its handler as `env`, or None for
    `BASIS_ENV`'s (`basis.core.environments.load`)."""
    names = ", ".join(environments.NAMES)
    parser.add_argument("--env", metavar="ENV", choices=environments.NAMES, help=f"{names}: the environment to act in, whose settings it reads; without it, BASIS_ENV's, and there is none by default")


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


@dataclass(frozen=True)
class LedgerArgs:
    table: str
    dialect: Dialect


def _ledger(argv: Sequence[str]) -> int:
    """`basis ddl --ledger TABLE --dialect DIALECT` without NAME, in a repo where several parts publish: the ledger's
    CREATE TABLE, which is the same for every part."""
    parser = _Parser(prog="basis ddl", description="print the ledger's CREATE TABLE, or name what a part runs, such as a job, to print its tables")
    parser.add_argument("--ledger", metavar="TABLE", required=True, help="the ledger, as BASIS_DATABASES__NAME__WRITE__LEDGER names it")
    parser.add_argument("--dialect", choices=["mssql", "oracle"], required=True)
    try:
        namespace = parser.parse_args(argv)
    except SystemExit as exited:  # --help prints, then exits
        return exited.code if isinstance(exited.code, int) else 0
    args = LedgerArgs(namespace.ledger, "mssql" if namespace.dialect == "mssql" else "oracle")
    sys.stdout.write(ddl.ledger(args.table, args.dialect))
    return 0


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


def reported(error: Exception) -> str:
    """Print `error` as `report` does, and return its line, without `ERROR: `."""
    report(error)
    return one_line(error) if isinstance(error, BasisError | ValidationError) else f"{type(error).__name__}: {one_line(error)}: a defect"


def report(error: Exception) -> int:
    """Print `error` as its one line, after its traceback if it is a defect, and return its exit code."""
    if isinstance(error, CommandError):
        error_line(str(error))
        return error.code
    deliberate = isinstance(error, BasisError | ValidationError)
    if not deliberate or log.isEnabledFor(logging.DEBUG):
        log.error("failed", exc_info=error)
    message = one_line(error)
    if not deliberate:
        message = f"{type(error).__name__}: {message}: a defect; the log record above holds its traceback"
    error_line(message)
    return 1


def one_line(error: BaseException) -> str:
    """`error`'s message, with the notes added on its way up, on one line."""
    if isinstance(error, ValidationError):
        text = invalid.explain(error)
    elif isinstance(error, FileNotFoundError) and error.filename is not None:
        text = f"no file or folder {error.filename}: give a path that exists"
    else:
        text = str(error) or type(error).__name__
    notes = [str(note) for note in getattr(error, "__notes__", [])]
    return " ".join(": ".join([*notes, text]).split())
