"""The gates in an agent's own harness (step 8.9): `basis hook`, which each agent's hooks run, refuses a shell
command that commits or pushes past the repo's hooks or repoints them, and an edit of the files that hold them, and
keeps an agent from stopping while `basis check` fails, handing it the ERROR lines. Every way the spike lists of
getting past the hooks is one case here (docs/spikes/agent-hooks.md)."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from basis.core import guard, hooks, sync

type Git = Callable[..., str]
"""conftest's `git` fixture."""

SKIPPING = [
    "git commit --no-verify -m 'x'",
    "git commit -m 'x' --no-verify",
    "git commit --no-veri -m x",
    "git commit -n -m x",
    "git commit -nm x",
    "git commit -anm x",
    "git commit -am x -n",
    "git -C sub commit --no-verify -m x",
    "git push --no-verify",
    "git push origin main --no-verify",
    "git merge --no-verify topic",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -ccore.hooksPath=/dev/null commit -m x",
    "git -c CORE.HOOKSPATH=/dev/null commit -m x",
    "git --config-env=core.hooksPath=EMPTY commit -m x",
    "git config core.hooksPath /dev/null",
    "git config --local core.hooksPath .git/hooks",
    "git config --unset core.hooksPath",
    "git config set core.hooksPath x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x",
    "env GIT_CONFIG_GLOBAL=/tmp/g git commit -m x",
    "export GIT_CONFIG_COUNT=1",
    "cd sub && git commit --no-verify -m x",
    "true; git push --no-verify",
    "echo hi | git commit -F - --no-verify",
    "/usr/bin/git commit --no-verify -m x",
    "git.exe commit --no-verify -m x",
    "command git commit --no-verify -m x",
    "sudo git commit --no-verify -m x",
    "bash -c 'git commit --no-verify -m x'",
    'sh -c "git -c core.hooksPath=/dev/null commit -m x"',
    "pwsh -Command 'git commit --no-verify -m x'",
    "python -c \"import subprocess; subprocess.run(['git', 'commit', '--no-verify'])\"",
    "git commit-tree HEAD^{tree} -p HEAD -m x",
    "git update-ref refs/heads/main abc123",
    "rm .githooks/pre-commit",
    "chmod -x .githooks/pre-commit",
    "echo exit 0 > .githooks/pre-commit",
    "sed -i 's/basis check/true/' .githooks/pre-commit",
    "mv .githooks hidden",
    "rm -rf .githooks",
    "git rm .githooks/pre-push",
    "cp /dev/null .claude/settings.json",
    "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json",
    "printf '[features]\\nhooks = false\\n' >> .codex/config.toml",
    "rm .cursor/hooks.json",
    "git config --global --edit",
]
"""Each way past the hooks the spike lists."""

LEFT_ALONE = [
    "git commit -m 'Add the rates job'",
    "git commit -m 'Explain why -n is never used'",
    "git commit -am 'x'",
    "git push origin main",
    "git push --force-with-lease",
    "git log --oneline -5",
    "git config --get core.hooksPath",
    "git config user.email",
    "git status",
    "cat .githooks/pre-commit",
    "ls .githooks",
    "git diff -- .githooks",
    "grep -n basis .claude/settings.json",
    "uv run basis check",
    "uv run pytest -k no_verify",
    "echo 'core hooks path'",
    "",
]


@pytest.mark.parametrize("command", SKIPPING)
def test_each_way_past_the_hooks_is_refused_with_the_fix(command: str) -> None:
    refused = guard.refusal(command)
    assert refused is not None, command
    reason, fix = refused
    assert reason
    assert fix.startswith(("fix what the hooks report", "leave")), fix


@pytest.mark.parametrize("command", LEFT_ALONE)
def test_a_command_that_keeps_the_hooks_is_left_alone(command: str) -> None:
    assert guard.refusal(command) is None


@pytest.mark.parametrize("path", [".githooks/pre-commit", ".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json", ".codex/hooks.json", ".codex/config.toml", ".git/config"])
def test_an_edit_of_the_files_that_hold_the_hooks_is_refused(tmp_path: Path, path: str) -> None:
    assert guard.edit_refusal(tmp_path, str(tmp_path / path)) is not None
    assert guard.edit_refusal(tmp_path, path) is not None


def test_an_edit_of_the_repos_own_code_is_left_alone(tmp_path: Path) -> None:
    assert guard.edit_refusal(tmp_path, str(tmp_path / "jobs" / "rates" / "job.py")) is None
    assert guard.edit_refusal(tmp_path, str(tmp_path / "docs" / "githooks.md")) is None


def answered(capsys: pytest.CaptureFixture[str]) -> Any:  # noqa: ANN401 -- an agent's JSON answer, whose shape each test asserts
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


BASH = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}


@pytest.mark.parametrize(
    ("agent", "given", "code"),
    [("claude", BASH, 0), ("codex", BASH, 0), ("cursor", {"hook_event_name": "beforeShellExecution", "command": "git commit --no-verify -m x"}, 2)],
    ids=["claude", "codex", "cursor"],
)
def test_each_agent_is_refused_a_command_in_its_own_words(tmp_path: Path, capsys: pytest.CaptureFixture[str], agent: guard.Agent, given: dict[str, object], code: int) -> None:
    assert guard.hook(tmp_path, "shell", agent, json.dumps(given)) == code
    found = answered(capsys)
    assert isinstance(found, dict)
    if agent == "cursor":
        assert (found["permission"], found["agent_message"] == found["user_message"]) == ("deny", True)
        assert "--no-verify" in str(found["agent_message"])
    else:
        output = found["hookSpecificOutput"]
        assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
        assert "--no-verify" in output["permissionDecisionReason"]


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor"])
def test_a_command_that_keeps_the_hooks_gets_no_answer_so_the_agents_own_permissions_decide(tmp_path: Path, capsys: pytest.CaptureFixture[str], agent: guard.Agent) -> None:
    given = {"command": "git status"} if agent == "cursor" else {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert guard.hook(tmp_path, "shell", agent, json.dumps(given)) == 0
    assert answered(capsys) is None


def test_an_edit_tool_on_a_hook_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    given = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / ".githooks" / "pre-commit"), "content": "exit 0\n"}}
    assert guard.hook(tmp_path, "edit", "claude", json.dumps(given)) == 0
    found = answered(capsys)
    assert isinstance(found, dict)
    assert found["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.fixture
def clone(tmp_path: Path, git: Git) -> Path:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "job.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "--no-verify", "-m", "first")
    return tmp_path


def checking(*lines: str) -> tuple[list[Path], Callable[[Path], Sequence[str]]]:
    asked: list[Path] = []

    def check(repo: Path) -> Sequence[str]:
        asked.append(repo)
        return lines

    return asked, check


@pytest.mark.parametrize(
    ("agent", "given", "answer"),
    [
        ("claude", {"hook_event_name": "Stop", "stop_hook_active": False}, {"decision": "block"}),
        ("codex", {"hook_event_name": "Stop", "stop_hook_active": False}, {"decision": "block"}),
        ("cursor", {"hook_event_name": "stop", "status": "completed", "loop_count": 0}, {}),
    ],
    ids=["claude", "codex", "cursor"],
)
def test_an_agent_that_stops_while_the_check_fails_is_handed_its_error_lines(
    clone: Path, capsys: pytest.CaptureFixture[str], agent: guard.Agent, given: dict[str, object], answer: dict[str, object]
) -> None:
    (clone / "job.py").write_text("x = eval('1')\n", encoding="utf-8")
    asked, check = checking("ERROR: job.py:1 S307 eval: use ast.literal_eval")
    assert guard.hook(clone, "stop", agent, json.dumps(given), check=check) == 0
    assert asked == [clone]
    found = answered(capsys)
    assert isinstance(found, dict)
    said = found["followup_message"] if agent == "cursor" else found["reason"]
    assert "ERROR: job.py:1 S307 eval: use ast.literal_eval" in said
    assert said.startswith("basis check fails")
    assert {key: found[key] for key in answer} == answer


@pytest.mark.parametrize(("agent", "given"), [("claude", {"stop_hook_active": True}), ("cursor", {"loop_count": 1})], ids=["claude", "cursor"])
def test_an_agent_already_sent_back_once_may_stop(clone: Path, capsys: pytest.CaptureFixture[str], agent: guard.Agent, given: dict[str, object]) -> None:
    """So no agent loops on a check it cannot pass: it stops, and says why, which the push hook holds anyway."""
    (clone / "job.py").write_text("x = eval('1')\n", encoding="utf-8")
    asked, check = checking("ERROR: job.py:1 S307 eval: use ast.literal_eval")
    assert guard.hook(clone, "stop", agent, json.dumps(given), check=check) == 0
    assert (asked, answered(capsys)) == ([], None)


def test_an_agent_that_changed_nothing_or_whose_check_passes_may_stop(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    asked, check = checking("ERROR: never printed: since the clean tree is not checked")
    assert guard.hook(clone, "stop", "claude", "{}", check=check) == 0
    assert (asked, answered(capsys)) == ([], None)
    (clone / "job.py").write_text("x = 2\n", encoding="utf-8")
    asked, check = checking()
    assert guard.hook(clone, "stop", "claude", "{}", check=check) == 0
    assert (asked, answered(capsys)) == ([clone], None)


def test_input_an_agent_did_not_send_is_one_error_line(tmp_path: Path) -> None:
    with pytest.raises(guard.HookError, match=r"basis hook shell read no JSON object on stdin"):
        guard.hook(tmp_path, "shell", "claude", "not json")


def test_sync_writes_each_agents_hooks_and_keeps_the_repos_own(tmp_path: Path, git: Git) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    (tmp_path / ".cursor").mkdir()
    own = {"command": ".cursor/hooks/format.sh"}
    (tmp_path / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"afterFileEdit": [own]}}), encoding="utf-8")
    git(tmp_path, "init", "--quiet")
    hooks.ensure(tmp_path)
    sync.write(tmp_path, [])
    claude = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = {event: [hook["command"] for group in groups for hook in group["hooks"]] for event, groups in claude["hooks"].items()}
    assert commands == {"PreToolUse": [guard.command("shell", "claude"), guard.command("edit", "claude")], "Stop": [guard.command("stop", "claude")]}
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert [hook["command"] for group in codex["hooks"]["PreToolUse"] for hook in group["hooks"]] == [guard.command("shell", "codex")]
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert cursor["hooks"]["afterFileEdit"] == [own]
    assert cursor["hooks"]["beforeShellExecution"] == [{"command": guard.command("shell", "cursor")}]
    assert sync.stale(tmp_path, []) == []


def test_the_stop_hook_hands_back_the_tools_lines_the_error_lines_point_at_then_the_error_lines() -> None:
    stdout = "jobs/x/transform.py:1:5: S307 Use of possibly insecure function\n\n" + "".join(f"line {n}\n" for n in range(guard.SHOWN))
    stderr = '{"level": "info", "message": "finished check"}\nERROR: ruff check found the problems listed above: fix each\n'
    lines = guard.reported(1, stdout, stderr)
    assert lines[0] == "jobs/x/transform.py:1:5: S307 Use of possibly insecure function"
    assert lines[guard.SHOWN :] == ["... and 1 more lines", "ERROR: ruff check found the problems listed above: fix each"]
    assert guard.reported(0, stdout, stderr) == []
