"""`basis hook EVENT --agent AGENT`: the gates in an agent's own harness, which the hooks `basis sync` writes for
Claude Code, Cursor and Codex run (docs/spikes/agent-hooks.md). A deny rule on a shell command is no boundary, as
Claude Code's own docs say, and `git -c core.hooksPath=/dev/null commit` matches none; a hook sees every command an
agent runs, and can refuse it, and can keep the agent from stopping.

- `shell`, before an agent runs a shell command: refused when it commits, pushes or merges past the repo's hooks or
  repoints them (`refusal`): `--no-verify`, or an abbreviation git takes for it, a commit's `-n`, alone or bundled,
  `core.hooksPath` set by `-c`, `--config-env`, `git config` or git's `GIT_CONFIG_*` variables, the plumbing that
  makes a commit or moves a branch without them (`commit-tree`, `update-ref`), and any change to the hooks' folder,
  the agents' settings or git's own config; inside `sh -c`, `bash -c`, `pwsh -Command` and an interpreter's `-c`
  too. Every other command gets no answer, so the agent's own permissions decide it.
- `edit`, before an agent's edit tool writes a file: refused for the same files (`edit_refusal`).
- `stop`, when an agent ends its turn: where the clone has changes no commit holds, `basis check` runs, and while it
  fails, the agent is sent back once with its ERROR lines. One sent back already may stop, and says why, so that no
  agent loops on a check it cannot pass; the push hook holds what reaches the remote either way.

What each agent sends on stdin and reads on stdout differs, and `hook` speaks each one's (`AGENTS`): Claude Code and
Codex read `hookSpecificOutput.permissionDecision` and `decision: block`, and Cursor `permission: deny`, with exit 2,
and `followup_message`. A hook is best effort too: a script the agent writes and runs, or a command an editor runs for
it, is out of its sight, which is why the push hook, which checks every pushed commit, stays the backstop."""

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from basis.core import clone
from basis.core.errors import BasisError

type Problem = tuple[str, str]
"""A reason and its fix, as `basis.core.sync.Problem` is."""
type Agent = Literal["claude", "cursor", "codex"]
type Event = Literal["shell", "edit", "stop"]
AGENTS: tuple[Agent, ...] = ("claude", "cursor", "codex")
EVENTS: tuple[Event, ...] = ("shell", "edit", "stop")
RUN = "uv run --frozen --quiet basis hook"
"""How every hook basis writes starts: through the repo's own environment, so it is the basis the repo locks."""

FIX = "fix what the hooks report, and commit or push again with them, which the push hook would hold anyway"
LEAVE = "leave the repo's hooks, the agents' settings and git's config as basis sync writes them; ask the developer to change them"
PROTECTED = (".githooks", ".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json", ".codex", ".git/config", ".gitconfig")
"""What holds the hooks, or would turn them off: the repo's hooks folder, each agent's hook settings, and git's config,
by path relative to the repo, a folder covering all it holds."""
SHOWN = 60
"""How many of the lines a failing `basis check` printed on stdout the stop hook hands back."""
_HOOKS_PATH = "core.hookspath"
_NO_VERIFY = "--no-verify"
_SHORTEST = len("--no-veri")
"""The shortest abbreviation of `--no-verify` git takes, since `--no-ve` is also `--no-verbose`'s."""
_COMMIT_WITH_ARGUMENT = frozenset("mFcCtSu")
"""The short options of `git commit` that take the rest of a bundle as their value, after which an `n` is no flag."""
_PLUMBING = frozenset({"commit-tree", "update-ref"})
_WRAPPERS = frozenset({"env", "command", "exec", "sudo", "nice", "nohup", "time", "xargs"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "pwsh", "powershell"})
_INTERPRETERS = re.compile(r"^(python[0-9.]*|py|node|ruby|perl)$")
_READERS = frozenset({"cat", "less", "more", "head", "tail", "grep", "rg", "ls", "dir", "diff", "wc", "file", "stat", "type", "find", "bat", "tree", "get-content"})
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "|&", ";;", "(", ")", "\n"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class HookError(BasisError):
    """What an agent sent that `basis hook` cannot read."""


def command(event: Event, agent: Agent) -> str:
    """The command a hook `basis sync` writes runs for `event` in `agent`'s harness."""
    return f"{RUN} {event} --agent {agent}"


def hooks(agent: Agent) -> dict[str, list[dict[str, object]]]:
    """The hooks `basis sync` writes for `agent`, by its event's name, in its own settings' shape."""
    if agent == "cursor":
        return {"beforeShellExecution": [{"command": command("shell", agent)}], "stop": [{"command": command("stop", agent)}]}
    timeouts: dict[Event, dict[str, object]] = {"shell": {"timeout": 30}, "edit": {"timeout": 30}, "stop": {"timeout": 900}} if agent == "claude" else {"shell": {}, "edit": {}, "stop": {}}
    entry = lambda event: {"type": "command", "command": command(event, agent), **timeouts[event]}  # noqa: E731 -- one entry's shape, three times
    before: list[dict[str, object]] = [{"matcher": "Bash", "hooks": [entry("shell")]}]
    if agent == "claude":  # Codex edits through its own patch tool, which its docs give no matcher for
        before.append({"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [entry("edit")]})
    stop: list[dict[str, object]] = [{"hooks": [entry("stop")]}]
    return {"PreToolUse": before, "Stop": stop}


def ours(entry: object) -> bool:
    """Whether a hook entry, or a group of them, is one `basis sync` writes."""
    return RUN in json.dumps(entry)


def refusal(text: str) -> Problem | None:
    """Why the shell command `text` gets past the repo's hooks or repoints them, with the fix, or None."""
    for segment in _segments(text):
        if (found := _segment_refusal(segment)) is not None:
            return found
    return None


def edit_refusal(repo: Path, path: str) -> Problem | None:
    """Why an agent's edit tool may not write `path`, given as the tool gives it, absolute or relative to `repo`, or
    None."""
    given = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    try:
        relative = PurePosixPath(*Path(given).relative_to(repo).parts) if given.is_absolute() else PurePosixPath(*given.parts)
    except ValueError:
        relative = PurePosixPath(given.name) if given.name == ".gitconfig" else None
    if relative is not None and (protected := _protected(relative.as_posix())) is not None:
        return f"the edit writes {relative.as_posix()}, which holds the repo's hooks or would turn them off ({protected})", LEAVE
    return None


def _protected(path: str) -> str | None:
    """The protected path `path` is, or is inside, or None."""
    path = path.removeprefix("./")
    return next((found for found in PROTECTED if path == found or path.startswith(f"{found}/") or path.endswith(f"/{found}")), None)


def _segments(text: str) -> Iterator[list[str]]:
    """Each simple command of `text`, as its words, split at the shell's operators."""
    lexer = shlex.shlex(text.replace("\r", ""), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    lexer.commenters = ""
    segment: list[str] = []
    try:
        for token in lexer:
            if token in _OPERATORS:
                if segment:
                    yield segment
                segment = []
            else:
                segment.append(token)
    except ValueError:  # an unbalanced quote: the rest as one word, which is as much as the shell would run
        segment.append(text)
    if segment:
        yield segment


def _segment_refusal(words: list[str]) -> Problem | None:
    redirected = any(word.startswith((">", ">>")) or word in {">", ">>", ">|"} for word in words)
    words, found = _unwrapped([word for word in words if word not in {">", ">>", ">|", "<"}])
    if found is not None or not words:
        return found
    program = _name(words[0])
    if (program in _SHELLS or _INTERPRETERS.match(program)) and (found := _inline_refusal(program, words[1:])) is not None:
        return found
    if program == "git":
        return _git(words[1:])
    named = [found for word in words[1:] if (found := _protected(word.strip("'\""))) is not None]
    if named and (redirected or program not in _READERS):
        return f"the command changes {named[0]}, which holds the repo's hooks or would turn them off", LEAVE
    return None


def _unwrapped(words: list[str]) -> tuple[list[str], Problem | None]:
    """A simple command's words without the variables it sets and the wrappers it runs through, such as `env` and
    `sudo`, or why a variable it sets hands git config that may repoint the hooks."""
    while words and _ASSIGNMENT.match(words[0]):
        name = words[0].partition("=")[0].upper()
        if name.startswith("GIT_CONFIG"):
            return words, (f"the command sets {name}, which hands git config the repo's hooks may be repointed by", LEAVE)
        words = words[1:]
    if words[:1] == ["export"] and any(word.upper().startswith("GIT_CONFIG") for word in words[1:]):
        return words, ("the command exports a GIT_CONFIG variable, which hands git config the repo's hooks may be repointed by", LEAVE)
    while words and _name(words[0]) in _WRAPPERS:
        words = words[1:]
        while words and words[0].startswith("-"):  # the wrapper's own options
            words = words[1:]
        while words and _ASSIGNMENT.match(words[0]):
            if words[0].upper().startswith("GIT_CONFIG"):
                return words, (f"the command sets {words[0].partition('=')[0]}, which hands git config the repo's hooks may be repointed by", LEAVE)
            words = words[1:]
    return words, None


def _inline_refusal(program: str, words: Sequence[str]) -> Problem | None:
    """Why the code a shell or an interpreter is given to run gets past the repo's hooks, or None."""
    for inner in _inline(words):
        if (found := refusal(inner)) is not None:
            return found
        if _INTERPRETERS.match(program) and _NO_VERIFY in inner:
            return f"the command runs {program} code that passes {_NO_VERIFY} to git, which skips the repo's hooks", FIX
        if _INTERPRETERS.match(program) and _HOOKS_PATH in inner.lower():
            return f"the command runs {program} code that names git's core.hooksPath, which repoints the repo's hooks", LEAVE
    return None


def _name(word: str) -> str:
    """A program's name, as a command gives it: without its folder or `.exe`, in lower case."""
    return str(re.split(r"[\\/]", word)[-1]).lower().removesuffix(".exe")


def _inline(words: Sequence[str]) -> Iterator[str]:
    """The code a shell's `-c` or an interpreter's `-c`/`-e` gives, which the command runs."""
    for index, word in enumerate(words[:-1]):
        if word.lower() in {"-c", "-e", "/c", "-command", "--command", "-encodedcommand"}:
            yield words[index + 1]


def _git(arguments: list[str]) -> Problem | None:
    """Why `git ARGUMENTS` gets past the repo's hooks or repoints them, or None."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        option = arguments[index]
        if option in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}:
            index += 2
        else:
            index += 1
    if index >= len(arguments):
        return None
    sub, rest = arguments[index], arguments[index + 1 :]
    reading = sub == "config" and not {"--get", "--get-all", "--get-regexp", "-l", "--list", "get", "list"}.isdisjoint(rest)
    if not reading and any(_HOOKS_PATH in word.lower() for word in arguments):
        return "the command sets git's core.hooksPath, which repoints the repo's hooks away from .githooks/", LEAVE
    return _subcommand(sub, rest)


def _subcommand(sub: str, rest: list[str]) -> Problem | None:
    """Why `git SUB REST` gets past the repo's hooks, or None."""
    found: Problem | None = None
    if sub in _PLUMBING:
        found = f"`git {sub}` makes a commit or moves a branch without the repo's hooks", FIX
    elif sub == "config" and any(word in {"-e", "--edit", "edit"} for word in rest):  # a key named in the command is `_git`'s
        found = "`git config --edit` changes git's config, where the repo's hooks may be repointed", LEAVE
    elif sub in {"rm", "mv", "checkout", "restore", "update-index"} and any(_protected(word) for word in rest):
        found = f"`git {sub}` changes what holds the repo's hooks", LEAVE
    for word in rest:
        if found is None and word.startswith("--no-veri") and _NO_VERIFY.startswith(word.partition("=")[0]) and len(word) >= _SHORTEST:
            found = f"`git {sub} {word}` skips the repo's hooks", FIX
        if found is None and sub == "commit" and _skips_by_n(word):
            found = f"`git commit {word}` is `--no-verify`, which skips the repo's hooks", FIX
    return found


def _skips_by_n(word: str) -> bool:
    """Whether a word of `git commit`'s is a bundle of short options holding `-n`, as `-n`, `-nm` or `-anm` are."""
    if not word.startswith("-") or word.startswith("--") or len(word) < 2:  # noqa: PLR2004 -- a dash and a letter
        return False
    for letter in word[1:]:
        if letter == "n":
            return True
        if letter in _COMMIT_WITH_ARGUMENT:
            return False
    return False


def hook(repo: Path, event: Event, agent: Agent, given: str, *, check: Callable[[Path], Sequence[str]] | None = None) -> int:
    """Answer the hook `agent` ran for `event`, as `given` on stdin describes it, on stdout in that agent's words; the
    exit code it reads. `check` gives the ERROR lines of `basis check` on the repo, by default from a run of it."""
    try:
        data: object = json.loads(given) if given.strip() else {}
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        msg = f"basis hook {event} read no JSON object on stdin, as {agent} sends a hook: run it only from the hooks basis sync writes"
        raise HookError(msg)
    data = {str(key): value for key, value in data.items()}
    if event == "stop":
        return _stop(repo, agent, data, check or _checked)
    if event == "shell":
        text = data.get("command") if agent == "cursor" else _tool_input(data).get("command")
        refused = refusal(text) if isinstance(text, str) else None
    else:
        path = _tool_input(data).get("file_path") or _tool_input(data).get("notebook_path")
        refused = edit_refusal(repo, path) if isinstance(path, str) else None
    if refused is None:
        return 0
    said = f"basis refuses this: {refused[0]}: {refused[1]}"
    if agent == "cursor":
        _answer({"permission": "deny", "user_message": said, "agent_message": said})
        return 2
    _answer({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": said}})
    return 0


def _tool_input(data: dict[str, object]) -> dict[str, object]:
    found = data.get("tool_input")
    return {str(key): value for key, value in found.items()} if isinstance(found, dict) else {}


def _stop(repo: Path, agent: Agent, data: dict[str, object], check: Callable[[Path], Sequence[str]]) -> int:
    again = data.get("loop_count", 0) not in {0, None} if agent == "cursor" else data.get("stop_hook_active") is True
    found = clone.of(repo)
    if again or found is None or found.clean():
        return 0
    lines = check(repo)
    if not lines:
        return 0
    said = "basis check fails on what you changed, which the commit and push hooks will refuse: fix each ERROR line, and run `uv run basis check` until it passes\n" + "\n".join(lines)
    _answer({"followup_message": said} if agent == "cursor" else {"decision": "block", "reason": said})
    return 0


def _checked(repo: Path) -> list[str]:
    """What a failing `basis check` on the repo printed, run in a process of its own: the tools' own lines on stdout,
    which its ERROR lines point at ("the problems listed above"), the first `SHOWN` of them, then the ERROR lines; or
    nothing when it passes."""
    done = subprocess.run([sys.executable, "-c", "import sys; from basis.core.command import main; sys.exit(main())", "check"], cwd=repo, capture_output=True, text=True, check=False)
    return reported(done.returncode, done.stdout, done.stderr)


def reported(code: int, stdout: str, stderr: str) -> list[str]:
    """What the stop hook hands back of a `basis check` that exited `code` and printed these."""
    if not code:
        return []
    printed = [line for line in stdout.splitlines() if line.strip()]
    shown = [*printed[:SHOWN], f"... and {len(printed) - SHOWN} more lines"] if len(printed) > SHOWN else printed
    return [*shown, *(line for line in stderr.splitlines() if line.startswith("ERROR: "))]


def _answer(value: object) -> None:
    sys.stdout.write(json.dumps(value) + "\n")
