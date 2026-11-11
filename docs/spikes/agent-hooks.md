# Spike 8.9: agent hooks

Can a repo's own files make Claude Code, Cursor and Codex refuse a commit or push that skips the repo's hooks, and
keep an agent from stopping while `basis check` fails? Yes for all three, on a trusted workspace, as best effort: a
hook sees the commands the agent's own tools run, not a script the agent writes and runs, and the push hook stays the
backstop. Read from each tool's current docs on 2026-11-10; no page showed a version or a date, and some wording came
through a summarising fetch, so the exact quotes are to be checked against the live pages before anyone relies on
them.

## Versions

The docs as published on 2026-11-10: Claude Code's
[hooks](https://code.claude.com/docs/en/hooks), [settings](https://code.claude.com/docs/en/settings),
[permissions](https://code.claude.com/docs/en/permissions) and
[managed settings](https://code.claude.com/docs/en/managed-settings); Cursor's
[hooks](https://cursor.com/docs/hooks); Codex's hooks, advanced config, security and config reference pages
(`developers.openai.com/codex/...`, which redirect to `learn.chatgpt.com/docs/...`).

## Answers

| Question | Claude Code | Cursor | Codex |
|---|---|---|---|
| Does a committed project file declare hooks? | Yes: `.claude/settings.json`, key `hooks` | Yes: `.cursor/hooks.json`, `{"version": 1, "hooks": {...}}` | Yes: `.codex/hooks.json`, or `[hooks]` in `.codex/config.toml` |
| Can a hook refuse a shell command before it runs? | Yes: `PreToolUse` with matcher `Bash`; `tool_input.command` on stdin; answer `hookSpecificOutput.permissionDecision: "deny"` with a reason, or exit 2 | Yes: `beforeShellExecution`; `command` on stdin; answer `permission: "deny"` with `user_message` and `agent_message`, or exit 2 | Yes: `PreToolUse` with matcher `Bash`; `tool_input.command`; answer as Claude Code does, or exit 2 |
| Can a hook refuse an edit tool's write? | Yes: `PreToolUse` with matcher `Edit\|Write\|MultiEdit\|NotebookEdit`, `tool_input.file_path` | No hook for the agent's own edits was found | Not for its patch tool: its docs give no matcher for it |
| Can a hook keep the agent from stopping? | Yes: `Stop`, answer `decision: "block"` with a reason; `stop_hook_active` says it was sent back already | Yes: `stop`, answer `followup_message`, sent as the next user message; `loop_count` says how often | Yes: `Stop`, answer `decision: "block"` with a reason; `stop_hook_active` as Claude Code's |
| Does the repo's hook run without the user's say? | On a trusted workspace, with no further approval | On a trusted workspace | Only once reviewed and trusted, bound to the hook's hash, and again after it changes (`/hooks`) |
| Can the user turn project hooks off? | Yes: `disableAllHooks` | None documented | Yes: `[features] hooks = false` |
| Can the firm's managed settings hold hooks a repo cannot remove? | Yes: `managed-settings.json` (`/etc/claude-code/` on Linux and WSL, `C:\Program Files\ClaudeCode\` on Windows), with `allowManagedHooksOnly` | Yes: an enterprise `hooks.json` (`/etc/cursor/`, `C:\ProgramData\Cursor\`) | Yes: `[hooks]` in `requirements.toml`, with `allow_managed_hooks_only` |
| Does the hook run on Windows? | Yes: Git Bash, or PowerShell where it is missing | Yes: PowerShell, with stdin piped | Not documented; a hook may give a `commandWindows` |

## What basis does with it

`basis sync` writes the hooks into each agent's file (`basis.core.sync.AGENT_HOOKS` and `CLAUDE_SETTINGS`), merged
with the repo's own, and every one runs `uv run --frozen --quiet basis hook EVENT --agent AGENT`, so it is the
repo's locked basis, in Python, on every platform (`basis.core.guard`):

- `shell` refuses a commit, merge or push with `--no-verify`, or any abbreviation of it git takes, or a commit's `-n`
  bundled or alone; `core.hooksPath` set by `-c`, `--config-env`, `git config` or a `GIT_CONFIG_*` variable;
  `git commit-tree` and `git update-ref`, which make a commit or move a branch without hooks; and a command that
  changes `.githooks/`, an agent's hook settings, or git's config. It looks inside `sh -c`, `bash -c`,
  `pwsh -Command` and an interpreter's `-c` too.
- `edit` refuses an edit tool's write of those same files, in Claude Code.
- `stop`, where the clone has changes no commit holds, runs `basis check`, and while it fails sends the agent back
  once with its ERROR lines. An agent sent back once may stop, so that none loops on a check it cannot pass.

## Each no, and its fallback

- **A script the agent writes and then runs is out of a hook's sight.** The push hook checks every pushed commit, and
  stays the backstop; the firm's CI, when it comes, is the boundary.
- **Cursor has no hook on its edit tool, and Codex none on its patch tool.** An edit to `.githooks/` or the settings
  shows in `basis check`, which compares them with what `basis sync` writes, and so fails the stop hook and the
  push.
- **The user can turn project hooks off, in Claude Code and Codex, and Codex runs a project hook only once trusted.**
  Only managed settings, which a repo cannot edit, close that: whether the firm can deploy them to its desktops is a
  **(work)** question for the maintainer, in the plan's step 10.3.
- **A Claude Code hook that times out does not block the call.** basis's `shell` and `edit` hooks read stdin and
  answer without starting git or anything slow, in about 0.6 s, under the 30 s they are given; the `stop` hook has
  900 s, for `basis check`.
