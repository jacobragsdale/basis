import tomllib
from pathlib import Path

import pytest

from basis.core.new import GIT, sources

PART = {"basis-job": "packages/job"}
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in."""


def test_a_checkout_gives_every_basis_package_from_its_folder() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=CHECKOUT))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"path": (CHECKOUT / "packages/job").as_posix(), "editable": True}, "basis-core": {"path": (CHECKOUT / "packages/core").as_posix(), "editable": True}}


def test_without_an_index_or_a_checkout_every_basis_package_comes_from_the_versions_tag() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=None))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": GIT, "subdirectory": "packages/job", "tag": "v0.7.0"}, "basis-core": {"git": GIT, "subdirectory": "packages/core", "tag": "v0.7.0"}}


def test_an_index_is_not_explicit_so_basis_core_comes_from_it_through_the_part() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index="https://feed.example.com/simple", checkout=None))["tool"]["uv"]
    assert found == {"index": [{"name": "internal", "url": "https://feed.example.com/simple"}]}


def test_a_folder_that_is_not_a_checkout_of_basis_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="is not a checkout of basis: it has no packages/job, packages/core"):
        sources(PART, version="0.7.0", index=None, checkout=tmp_path)
