"""The settings of a repo's ruff, ty and pytest, which basis owns, so a repo's checks are as strict as basis made them
and move with `basis upgrade`: `basis sync` writes `ruff.toml`, `ty.toml` and `pytest.ini` from basis's own settings,
each declared part's share (`Tools`, such as the folders its tests are in) and the repo's exceptions, each file
carrying `basis.core.sync.MARKER`; `basis check` refuses a `[tool.ruff]`, `[tool.ty]` or `[tool.pytest]` table in the
repo's `pyproject.toml`, which would sit beside them. The tools themselves arrive pinned through each part's `dev`
extra, as `basis-job[dev]`, at `RUFF` and `TY`, which ty's own README says may change its diagnostics between any two
versions, so a repo's checks change only when basis does.

A repo's exception is one rule at one path, with its reason, in `[tool.basis]`:

    exceptions = [
        { rule = "PLR0913", path = "jobs/positions/job.py", reason = "the hooks' signatures, which basis-job fixes" },
        { rule = "possibly-missing-attribute", path = "jobs/legacy/**", reason = "the port keeps the system's names" },
    ]

A ruff rule's code goes into `ruff.toml`'s per-file ignores, and a ty rule's name into an override of `ty.toml`, for
the path alone; a coverage floor under the generators' 90 is one too, `{ rule = "coverage", path = ".", reason = "..." }`,
without which `basis check` refuses it; `basis review` lists every exception. Basis owns a
repo's settings only where every part it declares gives its share; a part that gives none, one whose design is
still provisional, say, leaves the repo's own settings as they are."""

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field, ValidationError

from basis.core.errors import BasisError
from basis.core.model import Model
from basis.core.project import ProjectError, project

RUFF = "0.17.4"
"""The ruff every repo checks with, which each part's `dev` extra pins."""
TY = "0.0.99"
"""The ty every repo checks with, which each part's `dev` extra pins."""
FLOOR = 90
"""The coverage floor every generator writes, `[tool.basis] coverage`; `basis review` lists one under it."""
OWNED = ("ruff", "ty", "pytest")
"""The `[tool.*]` tables of `pyproject.toml` whose settings basis writes in their own files."""
FILES = ("ruff.toml", "ty.toml", "pytest.ini")
COVERAGE = "coverage"
"""The exception's rule that lowers the coverage floor under `FLOOR`, its path the repo's, as "."."""
_RUFF_RULE = re.compile(r"^[A-Z]{1,4}[0-9]{0,4}$")
"""A ruff rule's code, or a prefix of codes, as `PLR0913` or `ANN`."""
_TY_RULE = re.compile(r"^[a-z]+(?:-[a-z]+)*$")
"""A ty rule's name, as `possibly-missing-attribute`."""


class ToolsError(BasisError, ValueError):
    """A repo's exceptions that are not as `basis.core.tools` takes them; the message gives the fix."""


@dataclass(frozen=True)
class Tools:
    """A part's share of a repo's tool settings."""

    testpaths: tuple[str, ...]
    """The folders a repo of the part holds its tests in, as pytest's `testpaths`."""
    per_file_ignores: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    """Ruff's rules the part's own files may break by design, by glob, as a job's hooks share one signature."""
    markers: tuple[str, ...] = ()
    """pytest markers the part's tests use, each `NAME: what it means`."""


class Exception_(Model):  # noqa: N801 -- `Exception`, which the builtin's name takes
    """One rule a repo's code may break at one path, and why."""

    rule: str = Field(min_length=1)
    path: str = Field(min_length=1)
    reason: str = Field(min_length=10)

    @property
    def ruff(self) -> bool:
        return bool(_RUFF_RULE.match(self.rule))

    @property
    def ty(self) -> bool:
        return self.rule != COVERAGE and bool(_TY_RULE.match(self.rule))


def exceptions(pyproject: Path) -> list[Exception_]:
    """The repo's exceptions, `[tool.basis] exceptions`, each checked: a ruff rule's code or a ty rule's name, a path,
    and a reason."""
    try:
        read = project(pyproject)
    except OSError as error:
        msg = f"{pyproject} cannot be read as TOML ({error}), so the repo's exceptions are unknown: fix it"
        raise ToolsError(msg) from error
    except ProjectError as error:
        msg = f"{pyproject} cannot be read as TOML ({error.toml}), so the repo's exceptions are unknown: fix it"
        raise ToolsError(msg) from error
    given = read.exceptions if read is not None and read.exceptions is not None else []
    fix = 'give each as { rule = "PLR0913", path = "jobs/x/job.py", reason = "why the rule does not fit there" }'
    if not isinstance(given, list):
        msg = f"{pyproject}'s [tool.basis] exceptions is not a list: {fix}"
        raise ToolsError(msg)
    found: list[Exception_] = []
    for index, entry in enumerate(given):
        try:
            exception = Exception_.model_validate(entry)
        except ValidationError as error:
            problems = "; ".join(f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}" for problem in error.errors())
            msg = f"{pyproject}'s [tool.basis] exceptions[{index}] is not one rule at one path with its reason ({problems}): {fix}"
            raise ToolsError(msg) from None
        if not (exception.ruff or exception.ty or exception.rule == COVERAGE):
            kinds = "a ruff rule's code, as PLR0913, nor a ty rule's name, as possibly-missing-attribute"
            msg = f"{pyproject}'s [tool.basis] exceptions[{index}] names {exception.rule!r}, which is neither {kinds}: name one"
            raise ToolsError(msg)
        found.append(exception)
    return found


def owned(parts: Sequence[Tools | None]) -> bool:
    """Whether basis writes the repo's tool settings: where every declared part gives its share."""
    return bool(parts) and all(part is not None for part in parts)


def ruff_toml(marker: str, parts: Sequence[Tools], repo: Sequence[Exception_]) -> str:
    """`ruff.toml`: every stable rule, but those that cannot be fixed, only turned off; each part's ignores for its own
    files; and each of the repo's exceptions for its path."""
    ignores: dict[str, list[str]] = {"**/tests/**": ["S101", "PLR2004", "DTZ001"], "**/conftest.py": ["S101", "PLR2004"]}
    for part in parts:
        for glob, rules in part.per_file_ignores.items():
            ignores.setdefault(glob, []).extend(rule for rule in rules if rule not in ignores.get(glob, []))
    reasons: dict[str, list[str]] = {}
    for exception in (found for found in repo if found.ruff):
        ignores.setdefault(exception.path, []).append(exception.rule)
        reasons.setdefault(exception.path, []).append(f"{exception.rule}: {exception.reason}")
    per_file = "\n".join(f"{_quoted(glob)} = [{', '.join(_quoted(rule) for rule in rules)}]{_comment(reasons.get(glob, []))}" for glob, rules in ignores.items())
    return f"""# {marker}. The repo's own exceptions go in [tool.basis] exceptions, each with its reason.
line-length = 200
required-version = "=={RUFF}"
force-exclude = true
show-fixes = true
unsafe-fixes = false

[lint]
select = ["ALL"]  # every stable rule; preview rules stay off
ignore = [
    "COM812",  # conflicts with the formatter
    "ISC001",  # conflicts with the formatter
    "S603",  # flags every subprocess call; there is no fix, only review
    "TC001",  # moving imports under TYPE_CHECKING breaks code that reads annotations at runtime
    "TC002",
    "TC003",
    "CPY001",  # a copyright header is a legal decision per repository, not a lint fix
    "D",  # write a docstring only where it says what code and types cannot
]

[lint.per-file-ignores]
{per_file}

[lint.isort]
split-on-trailing-comma = false

[format]
docstring-code-format = true
skip-magic-trailing-comma = true
"""


def ty_toml(marker: str, repo: Sequence[Exception_]) -> str:
    """`ty.toml`: every rule an error, and each of the repo's exceptions an override for its path."""
    overrides = "".join(
        f'\n[[overrides]]\ninclude = [{_quoted(exception.path)}]\nrules = {{ {_quoted(exception.rule)} = "ignore" }}  # {_oneline(exception.reason)}\n' for exception in repo if exception.ty
    )
    return f"""# {marker}. The repo's own exceptions go in [tool.basis] exceptions, each with its reason.
[rules]
all = "error"  # every rule, those ty leaves off by default among them

[analysis]
respect-type-ignore-comments = false  # a specific `# ty: ignore[rule]`, with its reason
strict-generic-narrowing = true
strict-equality-semantics = true

[terminal]
error-on-warning = true
{overrides}"""


def pytest_ini(marker: str, parts: Sequence[Tools]) -> str:
    """`pytest.ini`: the tests' folders and markers of every part, unit tests alone by default, and every warning an
    error."""
    testpaths = list(dict.fromkeys(path for part in parts for path in part.testpaths))
    markers = list(dict.fromkeys(["integration: needs a real database or service; excluded from the default run", *(marker for part in parts for marker in part.markers)]))
    lines = "".join(f"\n    {found}" for found in markers)
    return f"""# {marker}.
[pytest]
addopts = -ra --strict-markers --strict-config --import-mode=importlib -m "not integration"
testpaths = {" ".join(testpaths)}
xfail_strict = true
filterwarnings = error
markers ={lines}
"""


def tables(pyproject: Path) -> list[str]:
    """The tables of `pyproject.toml` whose settings basis writes in their own files: `[tool.ruff]` and the rest."""
    try:
        tool: object = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
    except OSError, tomllib.TOMLDecodeError:
        return []  # its own problem, which the checks that read it report
    return [name for name in OWNED if isinstance(tool, dict) and name in tool]


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _comment(reasons: Sequence[str]) -> str:
    return f"  # {_oneline('; '.join(reasons))}" if reasons else ""
