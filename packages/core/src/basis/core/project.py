"""A repo's `[tool.basis]`, read in one place: each key basis reads, which the module that reads it checks, and each
declared part's own table. It imports nothing of basis's but its errors, so every module that reads a key may import
it."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from basis.core.errors import BasisError

QA_KEY = "qa"
"""The `[tool.basis]` table that declares the QA tier (`basis.core.qa`), which every command reads past."""


class ProjectError(BasisError, ValueError):
    """A repo's `pyproject.toml` that basis cannot read its `[tool.basis]` from."""

    def __init__(self, message: str, *, toml: tomllib.TOMLDecodeError | None = None) -> None:
        super().__init__(message)
        self.toml = toml
        """TOML's own error, where the file is not TOML, which a caller may say in its own words."""


KEYS = ("parts", "coverage", "exceptions", "apt-packages", QA_KEY)
"""Every key of `[tool.basis]` basis reads, besides each declared part's own table: the parts
(`basis.core.command`), the coverage floor (`basis.core.gates`), the reasoned exceptions to its tools' rules
(`basis.core.tools`), the image's Debian packages (`basis.core.sync`) and the QA tier (`basis.core.qa`), each checked
by the module that reads it."""


@dataclass(frozen=True)
class Project:
    """A repo's `[tool.basis]`, read once, each key as TOML gives it, for the module that reads it to check: see
    `KEYS`. `own` holds every other key, each a declared part's table or a key basis refuses."""

    pyproject: Path
    parts: object = None
    coverage: object = None
    exceptions: object = None
    apt_packages: object = None
    qa: object = None
    own: Mapping[str, object] = field(default_factory=dict)

    @property
    def table(self) -> dict[str, object]:
        """The table as TOML reads it."""
        named = {"parts": self.parts, "coverage": self.coverage, "exceptions": self.exceptions, "apt-packages": self.apt_packages, QA_KEY: self.qa}
        return {key: value for key, value in named.items() if value is not None} | dict(self.own)


def project(pyproject: Path) -> Project | None:
    """`pyproject`'s `[tool.basis]`, or None when it has none: the one place basis reads it, for `basis` itself, each
    module that reads one of its keys, and each part's `table`."""
    try:
        data: object = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"{pyproject} is not TOML: {error}: fix it"
        raise ProjectError(msg, toml=error) from error
    found = _mapping(_mapping(data, "tool"), "basis")
    if found is None:
        return None
    return Project(
        pyproject, found.get("parts"), found.get("coverage"), found.get("exceptions"), found.get("apt-packages"), found.get(QA_KEY),
        {key: value for key, value in found.items() if key not in KEYS},
    )  # fmt: skip


def basis_table(pyproject: Path) -> dict[str, object] | None:
    """`pyproject`'s `[tool.basis]`, as TOML reads it, or None when it has none (`project`)."""
    found = project(pyproject)
    return found.table if found is not None else None


def _mapping(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    found = {str(name): entry for name, entry in value.items()}.get(key)
    return {str(name): entry for name, entry in found.items()} if isinstance(found, dict) else None
