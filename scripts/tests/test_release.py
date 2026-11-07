import hashlib
import http.client
import threading
import tomllib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
import shared
from release import CORE, FeedServer, Member, ReleaseError, Run, Workspace, gate, main, next_version, problems, read, with_version

VERSION = "0.4.0"
BASE = Member(CORE, "packages/core", VERSION, ("pydantic>=2.14.3",), publish=True)
JOB = Member("basis-job", "packages/job", VERSION, (f"{CORE}=={VERSION}", "pandas>=3.1.1"), publish=True)
KEPT = Member("other-part", "packages/other", VERSION, (f"{CORE}=={VERSION}",), publish=False)
"""A member that every release builds and checks at the one version, and none publishes."""


def workspace(*members: Member, version: str = VERSION, locked: dict[str, str] | None = None, releases: tuple[str, ...] | None = None) -> Workspace:
    """The workspace of `members`, whose root's dependencies are `releases`, by default the published ones, as a plain `uv sync` installs them."""
    dependencies = tuple(member.name for member in members if member.publish is True) if releases is None else releases
    return Workspace(version, dependencies, members, {member.name: member.version for member in members} if locked is None else locked)


def with_(member: Member, *, version: str | None = None, dependencies: tuple[str, ...] | None = None, publish: bool | None = True) -> Member:
    return Member(member.name, member.folder, version or member.version, member.dependencies if dependencies is None else dependencies, publish)


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (workspace(BASE, JOB, KEPT), []),
        (workspace(BASE, with_(JOB, version="0.3.0"), locked={CORE: VERSION, "basis-job": VERSION}), ["packages/job/pyproject.toml gives basis-job version 0.3.0"]),
        (workspace(BASE, with_(JOB, dependencies=("basis-core==0.3.0",))), ["packages/job/pyproject.toml pins basis-core as `basis-core==0.3.0`"]),
        (workspace(BASE, with_(JOB, dependencies=(f"basis-core>={VERSION}",))), [f"pins basis-core as `basis-core>={VERSION}`"]),
        (workspace(BASE, with_(JOB, dependencies=("pandas>=3.1.1",))), ["pins basis-core as nothing"]),
        (workspace(BASE, JOB, with_(KEPT, publish=None)), ["packages/other/pyproject.toml does not say whether a release publishes other-part"]),
        (workspace(BASE, JOB, locked={CORE: VERSION, "basis-job": "0.3.0"}), ["uv.lock holds basis-job at 0.3.0"]),
        (workspace(BASE, JOB, locked={CORE: VERSION}), ["uv.lock holds basis-job at no version"]),
        (workspace(with_(BASE, publish=False), with_(JOB, publish=False)), ["no member says that a release publishes it"]),
        (
            workspace(with_(BASE, publish=False), JOB, releases=(CORE, "basis-job")),
            ["a release publishes basis-job, which depends on basis-core, which it does not publish", "list basis-core, which says a release does not publish it"],
        ),
        (workspace(BASE, JOB, KEPT, releases=(CORE, "basis-job", "other-part", "pandas>=3.1.1")), ["dependencies, which are what basis releases, list other-part, which says"]),
        (workspace(BASE, JOB, KEPT, releases=(CORE,)), ["basis-job says a release publishes it, and the root pyproject.toml's dependencies, which are what basis releases, do not list it"]),
        (workspace(JOB), ["no member of the workspace is basis-core"]),
        (workspace(BASE, JOB, version="0.4"), ["the root pyproject.toml's version, '0.4', is not MAJOR.MINOR.PATCH"]),
        (workspace(with_(BASE, version="1.0.0"), with_(JOB, version="1.0.0", dependencies=("basis-core==1.0.0",)), version="1.0.0"), ["the version is 1.0.0, and basis stays at 0.x"]),
    ],
    ids=[
        "one-version",
        "a-member-at-another",
        "a-pin-at-another",
        "a-range-not-a-pin",
        "no-pin",
        "undeclared",
        "a-stale-lock",
        "unlocked",
        "nothing-published",
        "a-published-part-needs-an-unpublished-one",
        "the-root-releases-an-unpublished-one",
        "the-root-leaves-out-a-published-one",
        "no-core",
        "not-semver",
        "past-0x",
    ],
)
def test_the_workspace_is_one_version_and_each_member_says_whether_it_is_published(found: Workspace, expected: list[str]) -> None:
    reasons = [problem.reason for problem in problems(found)]
    assert len(reasons) == len(expected), reasons
    assert all(fragment in reason for fragment, reason in zip(expected, reasons, strict=True)), reasons


def test_a_mismatch_says_the_command_that_sets_every_version_and_pin() -> None:
    [problem] = problems(workspace(BASE, with_(JOB, dependencies=("basis-core==0.3.0",))))
    assert problem.fix == f"run `uv run scripts/release.py version {VERSION}`, which sets every member's version and pin, and relocks"


@pytest.mark.parametrize(("tag", "count"), [(f"v{VERSION}", 0), ("v0.5.0", 1), (VERSION, 1)])
def test_a_tag_must_name_the_workspaces_version(tag: str, count: int) -> None:
    found = problems(workspace(BASE, JOB), tag=tag)
    assert len(found) == count
    assert all(problem.reason == f"the tag {tag} does not name the workspace's version, {VERSION}" for problem in found)


@pytest.mark.parametrize(("current", "asked", "expected"), [("0.3.2", "patch", "0.3.3"), ("0.3.2", "minor", "0.4.0"), ("0.3.2", "0.9.1", "0.9.1"), ("0.3.2", "0.3.2", "0.3.2")])
def test_the_next_version(current: str, asked: str, expected: str) -> None:
    assert next_version(current, asked) == expected


@pytest.mark.parametrize(("current", "asked"), [("0.3.2", "0.4"), ("0.3.2", "v0.4.0"), ("0.3.2", "major"), ("dev", "patch")])
def test_a_version_that_is_not_major_minor_patch_is_refused(current: str, asked: str) -> None:
    with pytest.raises(ReleaseError, match="give "):
        next_version(current, asked)


PYPROJECT = """# A member.
[project]
name = "basis-job"
version = "0.3.0"  # the one version
dependencies = [
    "basis-core==0.3.0",  # exactly its own version
    "basis-core-extras>=1.0",
    "pandas>=3.1.1",
]

[tool.other]
version = "9.9.9"
pin = "basis-core==0.3.0"
"""


def test_setting_the_version_changes_the_projects_version_and_pin_and_nothing_else() -> None:
    expected = PYPROJECT.replace('version = "0.3.0"  #', 'version = "0.4.0"  #').replace('"basis-core==0.3.0",  #', '"basis-core==0.4.0",  #')
    assert with_version(PYPROJECT, "0.4.0", pin=True) == expected
    assert with_version(PYPROJECT, "0.4.0", pin=False) == PYPROJECT.replace('version = "0.3.0"  #', 'version = "0.4.0"  #')


def test_basis_cores_own_name_is_no_requirement_of_it() -> None:
    text = '[project]\nname = "basis-core"\nversion = "0.3.0"\ndependencies = ["pydantic>=2.14.3"]\n'
    assert with_version(text, "0.4.0", pin=True) == text.replace('"0.3.0"', '"0.4.0"')


@pytest.mark.parametrize("pin", ['"basis-core>=0.3.0"', "\"basis-core==0.3.0; sys_platform == 'linux'\"", '"basis-core[extra]==0.3.0"', '"basis-core == 0.3.0"'])
def test_any_requirement_of_basis_core_becomes_the_exact_pin(pin: str) -> None:
    text = f'[project]\nname = "basis-job"\nversion = "0.3.0"\ndependencies = [{pin}]\n'
    assert with_version(text, "0.4.0", pin=True) == '[project]\nname = "basis-job"\nversion = "0.4.0"\ndependencies = ["basis-core==0.4.0"]\n'


@pytest.mark.parametrize(("text", "missing"), [('[tool.other]\nversion = "0.3.0"\n', "no \\[project\\] table"), ('[project]\nname = "basis-job"\n', "gives no version")])
def test_a_pyproject_without_a_project_version_is_refused(text: str, missing: str) -> None:
    with pytest.raises(ReleaseError, match=missing):
        with_version(text, "0.4.0", pin=True)


TOML = '''[project]  # the package
name = "basis-job"
description = """A job's package.
[not a table] in a multi-line string
"""
dependencies = [
    # [not a table], in a comment
    "pandas>=3.1.1",
    "basis-core==0.3.0",  # exactly its own version
]
version = "0.3.0"

[ project.urls ]
version = "9.9.9"
'''


def test_the_project_table_is_found_as_toml_finds_it() -> None:
    expected = TOML.replace('"basis-core==0.3.0",', '"basis-core==0.4.0",').replace('version = "0.3.0"', 'version = "0.4.0"')
    assert with_version(TOML, "0.4.0", pin=True) == expected
    spaced = TOML.replace("[project]  # the package", "[ project ]")
    assert with_version(spaced, "0.4.0", pin=True) == expected.replace("[project]  # the package", "[ project ]")


@pytest.mark.parametrize("version", ["version = '''0.3.0'''", 'version = {value = "0.3.0"}'], ids=["a-multi-line-literal", "an-inline-table"])
def test_a_version_this_script_cannot_edit_in_place_is_refused_not_mangled(version: str) -> None:
    with pytest.raises(ReleaseError, match=r"is written in a way this script does not edit: set it to 0\.4\.0 by hand$"):
        with_version(f'[project]\nname = "basis-job"\n{version}\n', "0.4.0", pin=True, where="packages/job/pyproject.toml")


def test_the_unreleased_version_is_no_release_whatever_the_workspace_says() -> None:
    unreleased = workspace(with_(BASE, version="0.0.0"), with_(JOB, version="0.0.0", dependencies=("basis-core==0.0.0",)), version="0.0.0")
    [problem] = problems(unreleased, tag="v0.0.0")
    assert problem.reason == "the tag v0.0.0 names no release: 0.0.0 is the version basis carries before its first"


def repo(root: Path, *, version: str = "0.3.0") -> Path:
    """A workspace of basis-core and basis-job at `version`, locked, each published."""
    root_project = f'[project]\nname = "workspace"\nversion = "{version}"\ndependencies = ["{CORE}", "basis-job"]\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    (root / "pyproject.toml").write_text(root_project, encoding="utf-8")
    for name, dependencies in [(CORE, ""), ("basis-job", f'"{CORE}=={version}"')]:
        folder = root / "packages" / name.removeprefix("basis-")
        folder.mkdir(parents=True)
        text = f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{dependencies}]\n\n[tool.basis-release]\npublish = true\n'
        (folder / "pyproject.toml").write_text(text, encoding="utf-8")
    relock(root)
    return root


def relock(root: Path) -> int:
    """What `uv lock` does to the members' versions."""
    found = read(root)
    (root / "uv.lock").write_text("".join(f'[[package]]\nname = "{member.name}"\nversion = "{member.version}"\n\n' for member in found.members), encoding="utf-8")
    return 0


def test_check_passes_on_one_version_and_says_what_a_release_publishes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--tag", "v0.3.0"], root=repo(tmp_path)) == 0
    assert capsys.readouterr().out == "basis 0.3.0: 2 members at one version; a release publishes basis-core, basis-job\n"
    assert main(["packages"], root=tmp_path) == 0
    assert capsys.readouterr().out == "basis-core\nbasis-job\n"


def test_version_sets_every_member_and_pin_from_the_root_and_relocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    relocked: list[Path] = []
    assert main(["version", "minor"], root=root, lock=lambda folder: relocked.append(folder) or relock(folder)) == 0
    assert relocked == [root]
    assert capsys.readouterr().out == "basis 0.4.0: every member, each basis-core pin and uv.lock; commit them\n"
    job: object = tomllib.loads((root / "packages/job/pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert job == {"name": "basis-job", "version": "0.4.0", "dependencies": ["basis-core==0.4.0"]}
    assert main(["check", "--tag", "v0.4.0"], root=root) == 0


def test_a_version_past_0x_is_refused_before_any_file_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert main(["version", "1.0.0"], root=root, lock=lambda _: pytest.fail("relocked")) == 1
    assert capsys.readouterr().err.startswith("ERROR: the version is 1.0.0, and basis stays at 0.x")
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_a_relock_that_fails_puts_every_file_back(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def failing(folder: Path) -> int:
        (folder / "uv.lock").write_text("half written", encoding="utf-8")
        return 1

    assert main(["version", "0.3.1"], root=root, lock=failing) == 1
    assert capsys.readouterr().err == (
        "ERROR: uv could not relock the workspace, as it says above, so every file is as it was: fix what it says, and set the version again, as `uv run scripts/release.py version 0.3.1` does\n"
    )
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_a_member_whose_version_cannot_be_set_changes_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    job = root / "packages/job/pyproject.toml"  # after basis-core, so writing each text as it was worked out would have changed that one
    job.write_text(job.read_text(encoding="utf-8").replace('version = "0.3.0"', "version = '''0.3.0'''"), encoding="utf-8")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert main(["version", "0.3.1"], root=root, lock=lambda _: pytest.fail("relocked")) == 1
    assert capsys.readouterr().err.startswith("ERROR: packages/job/pyproject.toml's [project] version or basis-core requirement is written in a way")
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_names_are_compared_as_pep_503_normalizes_them(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    job = root / "packages/job/pyproject.toml"
    job.write_text(job.read_text(encoding="utf-8").replace('name = "basis-job"', 'name = "Basis_Job"'), encoding="utf-8")
    lock = root / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8").replace('name = "basis-core"', 'name = "basis.core"'), encoding="utf-8")
    assert main(["check"], root=root) == 0, capsys.readouterr().err
    assert capsys.readouterr().out == "basis 0.3.0: 2 members at one version; a release publishes basis-core, basis-job\n"


def test_a_mismatch_fails_with_one_error_line_each(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo(tmp_path)
    job = root / "packages/job/pyproject.toml"
    job.write_text(job.read_text(encoding="utf-8").replace("basis-core==0.3.0", "basis-core==0.2.0"), encoding="utf-8")
    assert main(["check"], root=root) == 1
    assert main(["packages"], root=root) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2
    assert all(line.startswith("ERROR: packages/job/pyproject.toml pins basis-core as `basis-core==0.2.0`") for line in err)


def test_a_publish_that_is_not_true_or_false_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job = repo(tmp_path) / "packages/job/pyproject.toml"
    job.write_text(job.read_text(encoding="utf-8").replace("publish = true", 'publish = "yes"'), encoding="utf-8")
    assert main(["check"], root=tmp_path) == 1
    assert capsys.readouterr().err.endswith("[tool.basis-release] publish is 'yes': make it true or false\n")


COMMIT = "0123456789abcdef0123456789abcdef01234567"
PASSED = Run("completed", "success", "https://github.com/OWNER/REPO/actions/runs/2")
FAILED = Run("completed", "failure", "https://github.com/OWNER/REPO/actions/runs/1")
RUNNING = Run("in_progress", None, "https://github.com/OWNER/REPO/actions/runs/3")


def looks(*answers: Sequence[Run]) -> Callable[[], Sequence[Run]]:
    """The runs GitHub lists on each look, newest first, and the last answer again once they run out."""
    remaining = list(answers)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


@pytest.mark.parametrize(
    ("answers", "waits", "reason"),
    [
        ([(PASSED,)], 0, None),
        ([(PASSED, FAILED)], 0, None),  # a run that passed after one that failed
        ([(), (RUNNING,), (PASSED,)], 2, None),  # not made yet, then running, then passed
        ([(FAILED, PASSED)], 0, f"ci.yml's newest run on {COMMIT} concluded failure: {FAILED.url}"),
        ([()], 2, f"ci.yml has no run on {COMMIT}, as a commit that changes only docs gets none"),
        ([(RUNNING,)], 2, f"ci.yml's newest run on {COMMIT} has not finished: {RUNNING.url}"),
    ],
    ids=["passed", "passed-after-a-failure", "waits-until-it-passes", "failed", "no-run", "still-running"],
)
def test_a_tag_releases_only_a_commit_ci_passed_on(answers: list[Sequence[Run]], waits: int, reason: str | None) -> None:
    waited: list[None] = []
    found = gate(COMMIT, on_main=True, runs=looks(*answers), wait=lambda: waited.append(None), attempts=3)
    assert [problem.reason for problem in found] == ([] if reason is None else [reason])
    assert len(waited) == waits


def test_a_tag_of_a_commit_off_main_is_refused_before_anything_is_asked_of_github() -> None:
    [problem] = gate(COMMIT, on_main=False, runs=lambda: pytest.fail("asked GitHub"), wait=lambda: pytest.fail("waited"), attempts=3)
    assert problem.reason == f"the tag names commit {COMMIT}, which is not on main"
    assert problem.fix.startswith("delete the tag, as `git push origin :refs/tags/TAG` does")


def test_the_scripts_share_one_environment_for_the_tools_they_run() -> None:
    variables = {"PATH": "/bin", "VIRTUAL_ENV": "/venv", "UV_EXCLUDE_NEWER": "2026-11-07", "PYTHONPATH": "scripts", "GIT_DIR": ".git", "GH_TOKEN": "t"}
    assert shared.environment(variables) == {"PATH": "/bin", "GH_TOKEN": "t"}


WHEEL = "basis_core-0.4.0-py3-none-any.whl"
SDIST = "basis_core-0.4.0.tar.gz"


@pytest.fixture
def served(tmp_path: Path) -> Iterator[http.client.HTTPConnection]:
    for name in (WHEEL, SDIST, "SHA256SUMS"):
        (tmp_path / name).write_bytes(name.encode())
    server = FeedServer(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        yield connection
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def get(connection: http.client.HTTPConnection, path: str, method: str = "GET") -> http.client.HTTPResponse:
    connection.request(method, path)
    response = connection.getresponse()
    response.read()
    return response


def test_the_feed_serves_its_files_by_their_hash_and_sends_every_other_name_to_pypi(served: http.client.HTTPConnection) -> None:
    served.request("GET", "/simple/basis-core/")
    page = served.getresponse().read().decode()
    assert [line for line in page.splitlines() if line.startswith("<a ")] == [f'<a href="/files/{name}#sha256={hashlib.sha256(name.encode()).hexdigest()}">{name}</a><br>' for name in (WHEEL, SDIST)]
    served.request("GET", "/simple/Basis_Core/")  # a name as a requirement may spell it, normalized
    assert served.getresponse().read().decode() == page
    served.request("GET", f"/files/{WHEEL}")
    assert served.getresponse().read() == WHEEL.encode()
    assert get(served, f"/files/{WHEEL}", "HEAD").status == 200  # uv asks for a wheel's headers first
    redirected = get(served, "/simple/pandas/")
    assert (redirected.status, redirected.getheader("Location")) == (302, "https://pypi.org/simple/pandas/")
    assert get(served, "/files/SHA256SUMS").status == 404  # only a distribution is served, and only under /files/
    assert get(served, f"/{WHEEL}").status == 404


def test_a_feed_of_no_distributions_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="holds no wheel and no sdist: build them first"):
        FeedServer(tmp_path, port=0)
