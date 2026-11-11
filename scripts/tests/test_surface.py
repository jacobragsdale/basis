"""basis's compatibility promise for its own surface (`scripts/surface.py`): nothing a release had leaves it, but a name
that one release refused with its replacement, and the version being made has its surface recorded."""

import json
import tomllib
from typing import Any

import pytest
import surface

RELEASES = sorted(path.name for path in surface.FOLDER.iterdir() if (path / "surface.json").is_file())

RENAMED: dict[str, str] = {}
"""Each name that left the surface, as `flat` names it, with what replaced it: only after one release refused it with
that replacement, as `renamed` does for a variable and a module's `__getattr__` for a Python name."""


def flat(found: dict[str, Any]) -> set[str]:
    """Every name of a surface, one per line of what a repo relies on: `python basis.job.Table`, `verb job run --interval`,
    `setting BASIS_SNAPSHOT_ROOT`, `format manifest.run_id`."""
    python, verbs, settings, formats = found["python"], found["verbs"], found["settings"], found["formats"]
    names = {f"python {module}.{name}" for module, listed in python.items() for name in listed}
    names |= {f"verb {part} {verb}" for part, given in verbs.items() for verb in given}
    names |= {f"verb {part} {verb} {option}" for part, given in verbs.items() for verb, options in given.items() for option in options}
    names |= {f"setting {name}" for name in settings}
    return names | {f"format {kind}.{field}" for kind, fields in formats.items() for field in fields}


def test_the_version_being_made_and_every_release_before_it_has_its_surface_recorded() -> None:
    version = tomllib.loads((surface.ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version in RELEASES, f"run `uv run scripts/surface.py write {version}` as the release's commit does (docs/releasing.md)"


@pytest.mark.parametrize("release", RELEASES)
def test_nothing_a_release_had_has_left_the_surface_but_a_name_one_release_refused(release: str) -> None:
    released = json.loads((surface.FOLDER / release / "surface.json").read_text(encoding="utf-8"))
    gone = sorted(flat(released) - flat(surface.surface()) - set(RENAMED))
    assert gone == [], f"basis {release}'s surface had {gone}: keep each, or refuse it for one release with its replacement, then list it in RENAMED"


@pytest.mark.parametrize("release", RELEASES)
def test_the_ledger_has_the_columns_of_every_release_s_ledger_since_its_tables_outlive_them(release: str) -> None:
    released = json.loads((surface.FOLDER / release / "surface.json").read_text(encoding="utf-8"))
    current: dict[str, Any] = surface.surface()
    assert released["formats"]["ledger"] == current["formats"]["ledger"]
