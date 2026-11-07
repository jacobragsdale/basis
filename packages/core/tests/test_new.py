import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from basis.core.new import GIT, UNRELEASED, Commit, Installed, NewError, NoReleaseError, NotInstalledError, check_tags, commit_of, installed_from, repo, sources

PART = {"basis-job": "packages/job"}
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in."""
FORK = "https://git.example.com/basis"
SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef" * 4


def from_a_folder() -> Installed:
    return Installed.FILES


def from_an_index() -> Installed:
    return Installed.INDEX


def unread() -> Commit | Installed:
    """Where the part was installed from, which `sources` must not read when an index or a checkout says."""
    msg = "sources read where the part was installed from, when an index or a checkout already said where its packages come from"
    raise AssertionError(msg)


def test_a_checkout_gives_the_parts_package_from_its_folder_and_basis_core_through_it() -> None:
    found = tomllib.loads(sources(PART, version=UNRELEASED, index=None, checkout=CHECKOUT, installed=unread))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"path": (CHECKOUT / "packages/job").as_posix(), "editable": True}}


def test_without_an_index_or_a_checkout_the_parts_package_comes_from_the_versions_tag() -> None:
    found = tomllib.loads(sources(PART, version="0.7.0", index=None, checkout=None, installed=from_a_folder))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": GIT, "subdirectory": "packages/job", "tag": "v0.7.0"}}


def test_before_a_release_there_is_no_tag_and_the_error_says_to_run_from_git_or_use_a_checkout() -> None:
    run = r"run basis-job from git, as `uvx --from 'git\+\S+#subdirectory=packages/job' basis-job new repo NAME` does"
    with pytest.raises(NoReleaseError, match=rf"no tag holds its packages: {run}, or clone \S+ and pass --framework-path"):
        sources(PART, version=UNRELEASED, index=None, checkout=None, installed=from_a_folder)


@pytest.mark.parametrize("version", [UNRELEASED, "0.7.0"])
@pytest.mark.parametrize("revision", [None, "main", "v0.6.0", "0.7.0"], ids=["no-ref", "a-branch", "another-tag", "no-v"])
def test_a_part_installed_from_git_gives_the_new_repo_its_commit(version: str, revision: str | None) -> None:
    installed = Commit(FORK, SHA1, revision)
    found = tomllib.loads(sources(PART, version=version, index=None, checkout=None, installed=lambda: installed))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": FORK, "subdirectory": "packages/job", "rev": SHA1}}


@pytest.mark.parametrize("version", [UNRELEASED, "0.7.0"])
def test_a_part_installed_from_git_at_its_versions_tag_gives_the_new_repo_that_release(version: str) -> None:
    installed = Commit(FORK, SHA1, f"v{version}")
    found = tomllib.loads(sources(PART, version=version, index=None, checkout=None, installed=lambda: installed))["tool"]["uv"]["sources"]
    assert found == {"basis-job": {"git": FORK, "subdirectory": "packages/job", "tag": f"v{version}"}}


@pytest.mark.parametrize(("version", "ref"), [(UNRELEASED, ""), ("0.7.0", "@v0.7.0")])
def test_a_part_installed_from_an_index_is_refused_without_one_since_uv_does_not_say_which(version: str, ref: str) -> None:
    reason = "basis-job was installed from an index or a --find-links folder, which uv does not record, so the new repo cannot take its basis packages from the same place"
    fix = f"rerun the same command with `--index URL`, naming the feed, or install basis-job from git, as `uvx --from 'git+{GIT}{ref}#subdirectory=packages/job'` does"
    with pytest.raises(NewError, match=f"^{re.escape(f'{reason}: {fix}')}$"):
        sources(PART, version=version, index=None, checkout=None, installed=from_an_index)


def test_what_an_install_records_says_whether_it_came_from_an_index() -> None:
    assert installed_from("pydantic") is Installed.INDEX  # from PyPI, as every third-party package here is
    assert installed_from("basis-core") is Installed.FILES  # this workspace's member, installed editable


def test_a_distribution_that_is_not_installed_is_an_error_of_its_own_not_a_guess() -> None:
    with pytest.raises(NotInstalledError, match=r"^no-such-distribution is not installed here, so .*: reinstall no-such-distribution, or give --index or --framework-path$"):
        installed_from("no-such-distribution")


def locked(folder: Path, *packages: tuple[str, str]) -> Path:
    """A new repo's folder whose uv.lock locks each (name, git source) given, beside one from an index."""
    entries = [f'[[package]]\nname = "{name}"\nversion = "0.7.0"\nsource = {{ git = "{git}" }}\n' for name, git in packages]
    entries.append('[[package]]\nname = "pandas"\nversion = "3.1.1"\nsource = { registry = "https://pypi.org/simple" }\n')
    (folder / "uv.lock").write_text("version = 1\n\n" + "\n".join(entries), encoding="utf-8")
    return folder


def made_from(commit: str) -> Callable[[str], Commit | Installed]:
    """Where each package was installed from here: basis-job from `commit` in the fork, and nothing else."""

    def installed(name: str) -> Commit | Installed:
        if name == "basis-job":
            return Commit(FORK, commit, "v0.7.0")
        msg = f"{name} is not installed here"
        raise NotInstalledError(msg)

    return installed


@pytest.mark.parametrize(
    "source",
    [
        f"{FORK}?subdirectory=packages%2Fjob&tag=v0.7.0#{SHA1}",  # the tag still names the commit that made the repo
        f"{FORK}?subdirectory=packages%2Fjob&rev={SHA256}#{SHA256}",  # a commit, which cannot move
        f"{FORK}?subdirectory=packages%2Fjob&branch=main#{SHA256}",  # a branch, which moves on by design
    ],
    ids=["same-commit", "a-rev", "a-branch"],
)
def test_a_lock_that_takes_the_commit_that_made_the_repo_or_no_tag_passes(tmp_path: Path, source: str) -> None:
    check_tags(locked(tmp_path, ("basis-job", source)), made_from(SHA1))


def test_a_tag_that_now_names_another_commit_is_refused_naming_both(tmp_path: Path) -> None:
    repo_folder = locked(tmp_path, ("basis-job", f"{FORK}?subdirectory=packages%2Fjob&tag=v0.7.0#{SHA256}"))
    reason = f"the new repo's uv.lock takes basis-job from tag v0.7.0 at commit {SHA256}, and the basis-job installed here, which made it, came from commit {SHA1}"
    with pytest.raises(NewError, match=f"^{re.escape(reason)}, so the tag has moved since, .*: delete .*, and make it again with `@{SHA1}` in place of `@v0.7.0`"):
        check_tags(repo_folder, made_from(SHA1))


@pytest.mark.parametrize("installed", [lambda _: Installed.FILES, lambda _: Installed.INDEX, made_from(SHA1)], ids=["from-a-wheel", "from-an-index", "not-installed"])
def test_a_tagged_package_not_installed_from_git_here_has_no_commit_to_compare(tmp_path: Path, installed: Callable[[str], Commit | Installed]) -> None:
    check_tags(locked(tmp_path, ("basis-core", f"{GIT}?subdirectory=packages%2Fcore&tag=v0.7.0#{SHA256}")), installed)


@pytest.mark.parametrize(
    ("direct_url", "commit"),
    [
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA1}", "requested_revision": "main"}}, "subdirectory": "packages/job"}}', Commit(FORK, SHA1, "main")),
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
        (f'{{"url": "{FORK}", "vcs_info": {{"vcs": "git", "commit_id": "{SHA1}", "requested_revision": 7}}}}', "gives a requested_revision that is not a ref"),
    ],
    ids=["not-json", "no-url", "no-vcs", "a-ref-not-a-commit", "a-revision-not-a-ref"],
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
