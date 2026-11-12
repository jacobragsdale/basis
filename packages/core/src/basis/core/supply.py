"""The supply chain, which `basis check --full` checks: that the repo takes a dependency only once it has been public
`COOLDOWN_DAYS`, its `pyproject.toml`'s `[tool.uv] exclude-newer`, which `basis new` writes as `"7 days"`, with
basis's own packages (`OWN`), and what they bring, taken as released, since basis chose those versions and
`basis upgrade` brings them and their fixes; and `uv audit`
of the locked project, each known vulnerability one `ERROR` line with the version that fixes it.

A week keeps out a package that is compromised and pulled within days, as most such packages are, and costs a week's
wait for a new release. `uv audit` asks OSV (`https://api.osv.dev/`); a machine that cannot reach it skips the audit,
with one `SKIPPED` line, rather than fail every push. Both are uv's own, as uv 0.13.6's help documents them:
`exclude-newer` takes a duration, which the lock records as a span, so `uv lock --locked` holds from one day to the
next, and `uv audit`, a preview in that version, exits 0 with no vulnerability, 1 with one, and 2 when it cannot ask."""

import json
import re
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from basis.core.verbs import Problem

COOLDOWN_DAYS = 7
OWN = ("basis-core", "basis-job", "basis-api")
"""basis's own packages, which a repo takes as they are released."""
AUDIT = ("uv", "audit", "--locked", "--preview-features", "audit-command,json-output", "--output-format", "json")
SERVICE = "https://api.osv.dev/"
_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_DAYS = re.compile(r"^\s*(\d+)\s*(day|days|week|weeks|hour|hours)\s*$|^P(?:(\d+)W|(\d+)D|T(\d+)H)$", re.IGNORECASE)


def block(parts: Iterable[str] = ()) -> str:
    """The `[tool.uv]` lines `basis new` writes into a repo's `pyproject.toml`: the cooldown, from which basis's own
    packages are exempt, and every package they bring, as this environment has them installed (`closure`), since basis
    chose and tested those versions, which may be younger than the cooldown; what the repo adds itself waits."""
    exempt = sorted({*OWN, *closure([*OWN, *parts])})
    return (
        "[tool.uv]\n"
        f"# A dependency is taken only once it has been public {COOLDOWN_DAYS} days, which keeps out one compromised and pulled within days.\n"
        "# basis's own packages, and the ones they bring, come as basis released them. `basis check --full` checks the cooldown.\n"
        f'exclude-newer = "{COOLDOWN_DAYS} days"\n'
        "[tool.uv.exclude-newer-package]\n" + "".join(f"{name} = false\n" for name in exempt)
    )


def closure(names: Iterable[str]) -> set[str]:
    """`names` and every distribution they require, as this environment has them installed, by their normalized names:
    each extra of `names`, which a repo takes as `basis-job[dev]`, and no extra of anything else; one that is not
    installed, as a requirement for another platform, brings nothing."""
    given = {_normalized(name) for name in names}
    found: set[str] = set()
    todo = sorted(given)
    while todo:
        name = todo.pop()
        if name in found:
            continue
        try:
            required = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        found.add(name)
        for requirement in required:
            if "extra ==" in requirement.partition(";")[2] and name not in given:
                continue
            if (match := _REQUIREMENT.match(requirement)) is not None:
                todo.append(_normalized(match.group(0)))
    return found


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def cooldown(repo: Path) -> Problem | None:
    """Why the repo's `pyproject.toml` does not hold its dependencies back a week, or None where it does: a duration of
    `COOLDOWN_DAYS` or more, or a fixed date, which holds them back further every day."""
    with (repo / "pyproject.toml").open("rb") as file:
        found = tomllib.load(file).get("tool", {}).get("uv", {}).get("exclude-newer")
    fix = f'add exclude-newer = "{COOLDOWN_DAYS} days" under [tool.uv], as `basis new` writes it'
    if found in {None, False, "false"}:
        return "pyproject.toml takes a dependency as soon as it is published", fix
    days = _days(str(found))
    if days is not None and days < COOLDOWN_DAYS:
        return f"pyproject.toml holds a dependency back {found}, under the {COOLDOWN_DAYS} days basis asks", f'set exclude-newer = "{COOLDOWN_DAYS} days" under [tool.uv]'
    return None


def _days(text: str) -> float | None:
    """The days a duration names, or None for a date, or a form basis does not read, which uv checks itself."""
    if (match := _DAYS.match(text)) is None:
        return None
    count, unit, weeks, days, hours = match.groups()
    if count is not None:
        return int(count) * {"day": 1, "week": 7, "hour": 1 / 24}[unit.lower().rstrip("s")]
    return int(weeks) * 7 if weeks is not None else int(days) if days is not None else int(hours) / 24


@dataclass(frozen=True)
class Audited:
    """What `uv audit` found: each vulnerability, as one problem with its fix; why it did not run; how many it audited."""

    problems: list[Problem] = field(default_factory=list)
    skipped: str | None = None
    packages: int = 0


type Run = Callable[[Path], tuple[int, str]]
"""Runs `AUDIT` in a repo: its exit code and its stdout."""


def run_audit(repo: Path) -> tuple[int, str]:
    done = subprocess.run(AUDIT, cwd=repo, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def audit(repo: Path, *, run: Run = run_audit) -> Audited:
    """`uv audit` of the repo's locked dependencies, as the module's docstring says; a repo with no `uv.lock` has
    nothing locked to audit."""
    if not (repo / "uv.lock").is_file():
        return Audited(skipped="the repo has no uv.lock, so nothing is locked to audit: run `uv lock`")
    code, out = run(repo)
    if code not in {0, 1}:
        return Audited(skipped=f"uv audit could not ask its vulnerability service (exit {code}), as from a machine that cannot reach {SERVICE}")
    try:
        found = json.loads(out)
    except ValueError:
        return Audited(skipped=f"uv audit printed no report it reads (exit {code}): run `uv audit` to see why")
    problems: list[Problem] = []
    for entry in found.get("vulnerabilities", []):
        name, version = entry["dependency"]["name"], entry["dependency"]["version"]
        said = ", ".join([*entry.get("aliases", [])[:1], *([entry["link"]] if entry.get("link") else [])])
        reason = f"{name} {version} has the known vulnerability {entry['id']}" + (f" ({said})" if said else "")
        fixes = entry.get("fix_versions") or []
        fix = f"upgrade it to {fixes[0]} or later, as `uv lock --upgrade-package {name}`" if fixes else f"no release fixes it yet: remove {name}, or wait for a fix and upgrade then"
        problems.append((reason, fix))
    return Audited(problems=problems, packages=int(found.get("summary", {}).get("audited_packages", 0)))
