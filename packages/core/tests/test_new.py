import tomllib
from pathlib import Path

import pytest

from basis.core.new import GIT, UNRELEASED, NewError, NoReleaseError, sources

PART = {"basis-job": "packages/job"}
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in."""


def test_a_checkout_gives_the_parts_package_from_its_folder_and_basis_core_through_it() -> None:
    found = tomllib.loads(sources(PART, version=UNRELEASED, index=None, checkout=CHECKOUT))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"path": (CHECKOUT / "packages/job").as_posix(), "editable": True}}


def test_without_an_index_or_a_checkout_the_parts_package_comes_from_the_versions_tag() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=None))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": GIT, "subdirectory": "packages/job", "tag": "v0.7.0"}}


def test_before_a_release_there_is_no_tag_and_the_error_says_to_use_a_checkout() -> None:
    with pytest.raises(NoReleaseError, match=r"basis has no release yet, so no tag holds its packages: clone \S+ and pass --framework-path"):
        sources(PART, version=UNRELEASED, index=None, checkout=None)


def test_an_index_replaces_pypi_so_a_name_it_lacks_never_comes_from_pypi() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index="https://feed.example.com/simple", checkout=None))["tool"]["uv"]
    assert found == {"index": [{"name": "internal", "url": "https://feed.example.com/simple", "default": True}]}


def test_a_folder_that_is_not_a_checkout_of_basis_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NewError, match="is not a checkout of basis: it has no packages/job, packages/core"):
        sources(PART, version="0.7.0", index=None, checkout=tmp_path)
