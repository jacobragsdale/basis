"""The workspace's own pytest setup: every member under packages/ must be installed before its tests run. A plain
`uv sync` installs only what the root depends on, which leaves out a member basis does not release, and that member's
tests would fail to import it; this fails first, in one line, with the fix."""

import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest

MEMBERS = Path(__file__).parent / "packages"


def pytest_addoption(parser: pytest.Parser) -> None:  # noqa: ARG001 -- pytest's hook signature
    """The check, in the first hook this conftest gets: pytest calls it as it registers the conftest, which is before it
    imports the members' own conftests, one of which would fail on a member that is not installed. A `UsageError`
    here prints as one `ERROR` line."""
    missing = [name for name in members() if not installed(name)]
    if missing:
        names = ", ".join(missing)
        msg = f"the workspace's {names} {'is' if len(missing) == 1 else 'are'} not installed, and the tests import every member: run `uv sync --all-packages`"
        raise pytest.UsageError(msg)


def members() -> list[str]:
    """Each workspace member's distribution, by the name its pyproject.toml gives."""
    return sorted(str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]) for pyproject in MEMBERS.glob("*/pyproject.toml"))


def installed(name: str) -> bool:
    try:
        distribution(name)
    except PackageNotFoundError:
        return False
    return True
