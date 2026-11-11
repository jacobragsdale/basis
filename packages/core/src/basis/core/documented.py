"""What each part's tests hold its packaged words to (step 9.1): its docs, skills and agent block teach agents, so
every name they give of the part's module exists, as `etl.Row` or `api.Table`, and every `basis` command line they
give is one the real parser takes.

A command line in a code block, or an inline one that starts `uv run basis`, is one an agent copies, and must parse
whole, unless it is a synopsis, holding `[`, `|`, `(` or `...`; any other names a verb the repo's parts give, and only
options that verb takes. A path a whole line names, such as `--params jobs/JOB/params/default.json`, is made in a
scratch folder first, since the parser checks a file exists, not what it holds.

A skill's `SKILL.md` names itself as its folder does, and gives a description of at most `DESCRIBED` characters,
which is as much as the Agent Skills format has an agent read: what it reads to choose the skill."""

import contextlib
import re
import shlex
from collections.abc import Collection, Generator, Iterable, Mapping
from itertools import takewhile
from pathlib import Path
from types import ModuleType

from basis.core import command

_SPAN = re.compile(r"`((?:uv run )?basis [^`]+)`")
_BLOCK = re.compile(r"^ *```[a-z]*\n(.*?)^ *```", re.DOTALL | re.MULTILINE)
_LINE = re.compile(r"^(?:uv run )?basis ")
_SYNOPSIS = ("[", "|", "(", "...")
_SHELL = frozenset({"|", "||", "&&", ";", "&"})
_REDIRECT = re.compile(r"^[0-9]*[<>]")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
DESCRIBED = 1024
"""The most characters of a skill's description an agent reads, as the Agent Skills format caps it."""


def names(text: str, alias: str) -> list[str]:
    """Each attribute of `alias` that `text` names, as `etl.Query`, once each, in order; a file name such as `api.py`
    is none."""
    return list(dict.fromkeys(re.findall(rf"(?<![\w/.-]){re.escape(alias)}\.([A-Za-z_]\w*)", text)))


def commands(text: str) -> list[tuple[str, bool]]:
    """Each `basis` command line `text` gives, with whether it must parse whole, once each, in order."""
    found: dict[str, bool] = {}
    for block in _BLOCK.findall(text):
        for line in block.splitlines():
            line = line.split(" #")[0].strip()  # noqa: PLW2901 -- the line without its comment
            if _LINE.match(line):
                found[line] = found.get(line, False) or not _synopsis(line)
    for span in _SPAN.findall(text):
        line = " ".join(span.split())
        found[line] = found.get(line, False) or (line.startswith("uv run ") and not _synopsis(line))
    return list(found.items())


def _synopsis(line: str) -> bool:
    return any(mark in line for mark in _SYNOPSIS)


def problems(files: Iterable[Path], *, modules: Mapping[str, ModuleType], repo: command.Repo, scratch: Path, tables: Collection[str] = ()) -> list[str]:
    """Each name of `modules` (by the alias the words use, such as `etl`) that `files` give and the module lacks, and
    each command line they give that `repo`'s `basis` refuses, as one line naming its file; none when every one holds.
    `tables` are names the words give as a database's tables, in a schema named as an alias is, such as the ledger
    `etl.publishes`."""
    found: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for alias, module in modules.items():
            found += [f"{path}: {alias}.{name} is not in {module.__name__}" for name in names(text, alias) if name not in tables and not hasattr(module, name)]
        for line, whole in commands(text):
            if (refused := _refused(line, repo, scratch=scratch, whole=whole)) is not None:
                found.append(f"{path}: `{line}`: {refused}")
        if path.name == "SKILL.md":
            found += [f"{path}: {said}" for said in _skill(text, path.parent.name)]
    return found


def _skill(text: str, folder: str) -> list[str]:
    """What an agent would not read whole of a skill's front matter."""
    front = _FRONT.match(text)
    fields = dict(line.partition(": ")[::2] for line in front.group(1).splitlines()) if front is not None else {}
    found: list[str] = []
    if (name := fields.get("name", "").strip()) != folder:
        found.append(f"its name is {name or 'missing'}, not its folder's, {folder}")
    if not (description := fields.get("description", "").strip()):
        found.append("it has no description")
    elif len(description) > DESCRIBED:
        found.append(f"its description is {len(description)} characters, over the {DESCRIBED} an agent reads")
    return found


def _refused(line: str, repo: command.Repo, *, scratch: Path, whole: bool) -> str | None:
    """Why `basis` refuses `line`; a synopsis's `\\|`, a `|` escaped in a Markdown table, is no pipe, and its brackets
    hold options too."""
    try:
        words = shlex.split(line.replace("\\|", "\\\\|"))
    except ValueError as error:
        return f"not a command line: {error}"
    words = list(takewhile(lambda word: word not in _SHELL and not _REDIRECT.match(word), words))  # the shell's, not basis's
    argv = words[3:] if words[:2] == ["uv", "run"] else words[1:]
    if not whole:
        argv = [word.lstrip("[(").rstrip("]).") for word in argv]
    with _paths(scratch, argv if whole else []):
        return command.unparsed(argv, repo, whole=whole)


@contextlib.contextmanager
def _paths(scratch: Path, argv: Iterable[str]) -> Generator[None]:
    """In `scratch`, with each path `argv` names made: a file where it has a suffix, a folder where not."""
    for word in argv:
        if "/" in word and not word.startswith(("-", "@")):
            path = scratch / word
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            else:
                path.mkdir(parents=True, exist_ok=True)
    with contextlib.chdir(scratch):
        yield
