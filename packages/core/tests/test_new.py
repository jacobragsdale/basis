import re
import tomllib
from pathlib import Path

import pytest

from basis.core.new import GIT, UNRELEASED, Commit, NewError, NoReleaseError, commit_of, repo, sources

PART = {"basis-job": "packages/job"}
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in."""
FORK = "https://git.example.com/basis"
SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef" * 4


def from_an_index_or_a_folder() -> None:
    return None


def unread() -> Commit | None:
    """Where the part was installed from, which `sources` must not read when an index or a checkout says."""
    msg = "sources read where the part was installed from, when an index or a checkout already said where its packages come from"
    raise AssertionError(msg)


def test_a_checkout_gives_the_parts_package_from_its_folder_and_basis_core_through_it() -> None:
    found = tomllib.loads(sources(PART, version=UNRELEASED, index=None, checkout=CHECKOUT, installed=unread))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"path": (CHECKOUT / "packages/job").as_posix(), "editable": True}}


def test_without_an_index_or_a_checkout_the_parts_package_comes_from_the_versions_tag() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=None, installed=from_an_index_or_a_folder))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": GIT, "subdirectory": "packages/job", "tag": "v0.7.0"}}


def test_before_a_release_there_is_no_tag_and_the_error_says_to_run_from_git_or_use_a_checkout() -> None:
    run = r"run basis-job from git, as `uvx --from 'git\+\S+#subdirectory=packages/job' basis-job new repo NAME` does"
    with pytest.raises(NoReleaseError, match=rf"no tag holds its packages: {run}, or clone \S+ and pass --framework-path"):
        sources(PART, version=UNRELEASED, index=None, checkout=None, installed=from_an_index_or_a_folder)


@pytest.mark.parametrize("version", [UNRELEASED, "0.7.0"])
def test_a_part_installed_from_git_gives_the_new_repo_its_commit(version: str) -> None:
    found = tomllib.loads(sources(PART, version=version, index=None, checkout=None, installed=lambda: Commit(FORK, SHA1)))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": FORK, "subdirectory": "packages/job", "rev": SHA1}}


@pytest.mark.parametrize(
    ("direct_url", "commit"),
    [
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA1}", "requested_revision": "main"}}, "subdirectory": "packages/job"}}', Commit(FORK, SHA1)),
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA256}"}}}}', Commit(FORK, SHA256)),
        ('{"url": "file:///src/basis/packages/job", "dir_info": {"editable": true}}', None),
        ('{"url": "https://files.example.com/basis_job-0.7.0.tar.gz", "archive_info": {}}', None),
        (f'{{"url": "https://hg.example.com/basis", "vcs_info": {{"vcs": "hg", "commit_id": "{SHA1}"}}}}', None),
    ],
    ids=["git", "git-sha256", "folder", "archive", "mercurial"],
)
def test_only_a_git_install_names_a_commit(direct_url: str, commit: Commit | None) -> None:
    assert commit_of(direct_url, "basis-job") == commit


@pytest.mark.parametrize(
    ("direct_url", "problem"),
    [
        ("{", "is not JSON"),
        ('{"vcs_info": {"vcs": "git"}}', "gives no url"),
        (f'{{"url": "{FORK}", "vcs_info": {{"commit_id": "{SHA1}"}}}}', "has a vcs_info that names no version control system"),
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "main"}}}}', "says it came from git, and gives no full commit id"),
    ],
    ids=["not-json", "no-url", "no-vcs", "a-ref-not-a-commit"],
)
def test_a_direct_url_that_is_not_what_pep_610_describes_is_refused_with_the_fix(direct_url: str, problem: str) -> None:
    with pytest.raises(NewError, match=rf"basis-job's direct_url.json.* {problem}.*: reinstall basis-job, as `uvx --refresh --from .*` does, or give --index or --framework-path$"):
        commit_of(direct_url, "basis-job")


def test_an_index_replaces_pypi_so_a_name_it_lacks_never_comes_from_pypi() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index="https://feed.example.com/simple", checkout=None, installed=unread))["tool"]["uv"]
    assert found == {"index": [{"name": "internal", "url": "https://feed.example.com/simple", "default": True}]}


def test_a_folder_that_is_not_a_checkout_of_basis_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NewError, match="is not a checkout of basis: it has no packages/job, packages/core"):
        sources(PART, version="0.7.0", index=None, checkout=tmp_path, installed=unread)


@pytest.mark.parametrize("name", ["Pricing", "pricing etl", "pricing!", "-pricing"])
def test_a_folder_that_is_no_repos_name_is_refused_before_anything_is_made(tmp_path: Path, name: str) -> None:
    with pytest.raises(NewError, match=re.escape(f"{name!r} is no repo's name")):
        repo(tmp_path / name, templates=tmp_path, values={}, distributions=PART, version=UNRELEASED, index=None, checkout=CHECKOUT)
    assert list(tmp_path.iterdir()) == []
