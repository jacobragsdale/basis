"""The supply chain (step 12.9): a new repo takes a dependency only once it has been public a week, `[tool.uv]
exclude-newer = "7 days"`, basis's own packages as released, and `basis check --full` checks the cooldown is there and
runs `uv audit`, each vulnerability one `ERROR` line with its fix, and a service it cannot reach one `SKIPPED` line."""

import json
import tomllib
from pathlib import Path

import pytest

from basis.core import new, supply

VULNERABLE = {
    "summary": {"audited_packages": 2, "vulnerabilities": 1, "adverse_statuses": 0},
    "vulnerabilities": [
        {"dependency": {"name": "jinja2", "version": "2.11.2"}, "id": "PYSEC-2021-66", "aliases": ["CVE-2020-28493"], "summary": None, "link": "https://example.test/a", "fix_versions": ["2.11.3"]}
    ],
    "adverse_statuses": [],
}
"""What `uv audit --output-format json` printed, with uv 0.13.6, for a project that pins jinja2 2.11.2."""


def pyproject(tmp_path: Path, uv: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "r"\nversion = "0.1.0"\n\n{uv}', encoding="utf-8")
    return tmp_path


def test_a_new_repos_sources_hold_the_cooldown_and_take_basiss_own_packages_as_released(tmp_path: Path) -> None:
    text = new.sources({"basis-job": "packages/job"}, version="0.1.0", index="https://feed.example/simple", checkout=None, installed=lambda: pytest.fail("not read"))
    uv = tomllib.loads(text)["tool"]["uv"]
    assert uv["exclude-newer"] == "7 days"
    exempt = uv["exclude-newer-package"]
    assert set(exempt.values()) == {False}
    assert {*supply.OWN, "basis-job", "pandas", "pydantic", "pydantic-core", "ruff", "ty"} <= exempt.keys()  # what basis brings, at the versions it chose
    assert "pre-commit" not in exempt  # the workspace's own, which no basis package brings
    assert supply.cooldown(pyproject(tmp_path, text)) is None


@pytest.mark.parametrize(
    ("uv", "says"),
    [
        ("", 'pyproject.toml takes a dependency as soon as it is published: add exclude-newer = "7 days" under [tool.uv]'),
        ("[tool.uv]\nexclude-newer = false\n", "pyproject.toml takes a dependency as soon as it is published"),
        ('[tool.uv]\nexclude-newer = "2 days"\n', 'pyproject.toml holds a dependency back 2 days, under the 7 days basis asks: set exclude-newer = "7 days" under [tool.uv]'),
        ('[tool.uv]\nexclude-newer = "P3D"\n', "holds a dependency back P3D, under the 7 days"),
    ],
)
def test_a_cooldown_missing_or_too_short_is_a_problem(tmp_path: Path, uv: str, says: str) -> None:
    found = supply.cooldown(pyproject(tmp_path, uv))
    assert found is not None
    assert says in ": ".join(found)


@pytest.mark.parametrize("uv", ['[tool.uv]\nexclude-newer = "1 week"\n', '[tool.uv]\nexclude-newer = "P14D"\n', '[tool.uv]\nexclude-newer = "2026-10-01T00:00:00Z"\n'])
def test_a_week_or_more_or_a_fixed_date_is_cooldown_enough(tmp_path: Path, uv: str) -> None:
    assert supply.cooldown(pyproject(tmp_path, uv)) is None


def locked(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return tmp_path


def test_each_vulnerability_the_audit_finds_is_one_problem_with_its_fix(tmp_path: Path) -> None:
    found = supply.audit(locked(tmp_path), run=lambda _: (1, json.dumps(VULNERABLE)))
    assert found == supply.Audited(
        problems=[("jinja2 2.11.2 has the known vulnerability PYSEC-2021-66 (CVE-2020-28493, https://example.test/a)", "upgrade it to 2.11.3 or later, as `uv lock --upgrade-package jinja2`")],
        skipped=None,
        packages=2,
    )


def test_an_audit_that_finds_nothing_has_no_problem_and_one_that_cannot_reach_its_service_is_skipped(tmp_path: Path) -> None:
    clean = {"summary": {"audited_packages": 9, "vulnerabilities": 0, "adverse_statuses": 0}, "vulnerabilities": [], "adverse_statuses": []}
    assert supply.audit(locked(tmp_path), run=lambda _: (0, json.dumps(clean))) == supply.Audited(problems=[], skipped=None, packages=9)
    unreached = supply.audit(tmp_path, run=lambda _: (2, "error: Failed to query https://api.osv.dev/"))
    assert unreached.problems == []
    assert unreached.skipped == "uv audit could not ask its vulnerability service (exit 2), as from a machine that cannot reach https://api.osv.dev/"


def test_a_vulnerability_with_no_fix_says_so(tmp_path: Path) -> None:
    unfixed = json.loads(json.dumps(VULNERABLE))
    unfixed["vulnerabilities"][0]["fix_versions"] = []
    [(reason, fix)] = supply.audit(locked(tmp_path), run=lambda _: (1, json.dumps(unfixed))).problems
    assert "PYSEC-2021-66" in reason
    assert fix == "no release fixes it yet: remove jinja2, or wait for a fix and upgrade then"


def test_a_repo_with_no_lock_has_nothing_to_audit(tmp_path: Path) -> None:
    assert supply.audit(tmp_path, run=lambda _: pytest.fail("ran uv audit")).skipped == "the repo has no uv.lock, so nothing is locked to audit: run `uv lock`"


def test_the_audit_asks_uv_for_json_of_the_locked_project() -> None:
    assert supply.AUDIT[:3] == ("uv", "audit", "--locked")
    assert "--output-format" in supply.AUDIT
