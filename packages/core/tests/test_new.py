import tomllib
from pathlib import Path

import pytest

from basis.core.new import GIT, UNRELEASED, Commit, NewError, NoReleaseError, commit_of, sources

PART = {"basis-job": "packages/job"}
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in."""
FORK = "https://git.example.com/basis"
SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef" * 4


def test_a_checkout_gives_the_parts_package_from_its_folder_and_basis_core_through_it() -> None:
    found = tomllib.loads(sources(PART, version=UNRELEASED, index=None, checkout=CHECKOUT, commit=None))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"path": (CHECKOUT / "packages/job").as_posix(), "editable": True}}


def test_without_an_index_or_a_checkout_the_parts_package_comes_from_the_versions_tag() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=None, commit=None))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": GIT, "subdirectory": "packages/job", "tag": "v0.7.0"}}


def test_before_a_release_there_is_no_tag_and_the_error_says_to_run_from_git_or_use_a_checkout() -> None:
    run = r"run basis-job from git, as `uvx --from 'git\+\S+#subdirectory=packages/job' basis-job new repo NAME` does"
    with pytest.raises(NoReleaseError, match=rf"no tag holds its packages: {run}, or clone \S+ and pass --framework-path"):
        sources(PART, version=UNRELEASED, index=None, checkout=None, commit=None)


@pytest.mark.parametrize("version", [UNRELEASED, "0.7.0"])
def test_a_part_installed_from_git_gives_the_new_repo_its_commit(version: str) -> None:
    found = tomllib.loads(sources(PART, version=version, index=None, checkout=None, commit=Commit(FORK, SHA1)))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": FORK, "subdirectory": "packages/job", "rev": SHA1}}


@pytest.mark.parametrize(
    ("direct_url", "commit"),
    [
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA1}", "requested_revision": "main"}}, "subdirectory": "packages/job"}}', Commit(FORK, SHA1)),
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA256}"}}}}', Commit(FORK, SHA256)),
        ('{"url": "file:///src/basis/packages/job", "dir_info": {"editable": true}}', None),
        ('{"url": "https://files.example.com/basis_job-0.7.0.tar.gz", "archive_info": {}}', None),
        (f'{{"url": "https://hg.example.com/basis", "vcs_info": {{"vcs": "hg", "commit_id": "{SHA1}"}}}}', None),
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "main"}}}}', None),
    ],
    ids=["git", "git-sha256", "folder", "archive", "mercurial", "a-ref-not-a-commit"],
)
def test_only_a_git_install_names_a_commit(direct_url: str, commit: Commit | None) -> None:
    assert commit_of(direct_url) == commit


def test_an_index_replaces_pypi_so_a_name_it_lacks_never_comes_from_pypi() -> None:
    commit = Commit(FORK, SHA1)  # given an index, where the part came from does not matter
    found = tomllib.loads(sources(PART, version="0.7.0", index="https://feed.example.com/simple", checkout=None, commit=commit))["tool"]["uv"]
    assert found == {"index": [{"name": "internal", "url": "https://feed.example.com/simple", "default": True}]}


def test_a_folder_that_is_not_a_checkout_of_basis_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NewError, match="is not a checkout of basis: it has no packages/job, packages/core"):
        sources(PART, version="0.7.0", index=None, checkout=tmp_path, commit=None)
