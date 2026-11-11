"""The agent: Claude Code, headless, on this host's own login, in the trial's repo. It reads the repo's own `CLAUDE.md`,
skills, settings and hooks, as a developer's agent would, and none of the user's (`--setting-sources project,local`, no
MCP servers); its web tools are off, since this repo, which holds every task's solution, is public. Its transcript is
Claude Code's `stream-json`, which `summary` reads."""

import json
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from basis_eval import repo as repos

CHECKS = re.compile(r"\b(?:basis\s+check|pytest|ruff|ty\s+check|pre-commit|git\s+commit)\b")
"""A shell command that runs a check, as an agent closes its loop: `basis check`, a tool it runs, or a commit, whose hook
runs them."""
TIMEOUT_S = 1500.0


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output


class Run(BaseModel):
    """What one run of the agent did, as its transcript says."""

    model_config = ConfigDict(extra="forbid")

    seconds: float = 0.0
    """Its wall-clock time, from start to exit."""
    exit_code: int = 0
    timed_out: bool = False
    finished: bool = False
    """Whether the transcript ends with Claude Code's result, which a run that crashed or timed out has not."""
    error: str | None = None
    turns: int = 0
    tokens: Tokens = Field(default_factory=Tokens)
    cost_usd: float = 0.0
    """The API price of those tokens, which the subscription does not charge."""
    models: list[str] = Field(default_factory=list)
    version: str | None = None
    """Claude Code's version, as its transcript gives it."""
    commands: int = 0
    """Shell commands it ran."""
    check_runs: int = 0
    """Those that ran a check (`CHECKS`)."""


def argv(instruction: str, model: str) -> list[str]:
    return [
        "claude", "-p", instruction, "--model", model, "--output-format", "stream-json", "--verbose",
        "--setting-sources", "project,local", "--strict-mcp-config", "--permission-mode", "bypassPermissions",
        "--no-session-persistence", "--disallowedTools", "WebFetch", "WebSearch",
    ]  # fmt: skip


def summary(lines: Iterable[str]) -> Run:
    """What a `stream-json` transcript says the agent did."""
    found = Run()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            found.version = _text(event.get("claude_code_version")) or found.version
        elif event.get("type") == "assistant":
            for command in _commands(event):
                found.commands += 1
                found.check_runs += bool(CHECKS.search(command))
        elif event.get("type") == "result":
            found.finished = True
            found.turns = _int(event.get("num_turns"))
            found.cost_usd = float(event.get("total_cost_usd") or 0.0)
            found.error = None if event.get("subtype") == "success" and not event.get("is_error") else _text(event.get("subtype")) or "error"
            usage = event.get("modelUsage")
            if isinstance(usage, dict):
                found.models = sorted(str(name) for name in usage)
                found.tokens = Tokens(
                    input=sum(_int(each.get("inputTokens")) for each in usage.values() if isinstance(each, dict)),
                    cache_read=sum(_int(each.get("cacheReadInputTokens")) for each in usage.values() if isinstance(each, dict)),
                    cache_write=sum(_int(each.get("cacheCreationInputTokens")) for each in usage.values() if isinstance(each, dict)),
                    output=sum(_int(each.get("outputTokens")) for each in usage.values() if isinstance(each, dict)),
                )
    return found


def _commands(event: Mapping[str, object]) -> list[str]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    found = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
            given = block.get("input")
            command = given.get("command") if isinstance(given, dict) else None
            if isinstance(command, str):
                found.append(command)
    return found


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def run(instruction: str, model: str, repo: Path, env: Mapping[str, str], transcript: Path) -> Run:
    """Run the agent on `instruction` in `repo`, writing its transcript to `transcript`."""
    transcript.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    code, output, _ = repos.run(argv(instruction, model), repo, env, timeout=TIMEOUT_S)
    seconds = time.monotonic() - start
    transcript.write_text(output, encoding="utf-8")
    found = summary(output.splitlines())
    found.seconds, found.exit_code, found.timed_out = seconds, code, code == 124 and not found.finished  # noqa: PLR2004 -- timeout's exit code, which repos.run gives
    if not found.finished and found.error is None:
        found.error = "timed out" if found.timed_out else f"claude exited {code} with no result"
    return found
