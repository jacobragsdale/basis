## basis

This repo's one command is `basis`, and `uv run basis --help` lists every verb it has here. Every failure prints one
line that starts with `ERROR`, gives the reason and ends with the fix.

- Commit and push with the hooks in `.githooks/`, which `basis check`, `sync` and `new` turn on. The commit hook
  fixes and formats the staged Python files with ruff, then runs `basis check --staged`, which also refuses a staged
  private key, a file over 500 KB, and YAML or TOML that does not parse; the push hook runs `basis check --full` on
  each commit being pushed. Never skip either with `--no-verify` or `-n`: when one fails, fix what each `ERROR` line
  says, and commit or push again. `.claude/settings.json` denies the usual ways to skip them, as best it can; the push
  hook is the backstop.
- `uv run basis check --full` adds `regress`, which replays recent published runs from `BASIS_SNAPSHOT_ROOT` with
  this code, and the QA tier, which runs each part against QA where `pyproject.toml` declares `[tool.basis.qa]` and
  this machine has its settings file; a piece that cannot run prints one `SKIPPED` line saying why. An output that
  `regress` sees change fails nothing: say in the pull request why it is meant. Open the pull request with
  `uv run basis review` as its description, which summarizes what the push's check found on the commit.
- `uv run basis doctor` checks this desktop's uv, git, hooks, feed and settings, and `uv run basis upgrade` takes a
  basis release: it relocks, syncs, checks in full and says what changed, for a commit of its own.
- basis writes, and owns: each file whose first lines say basis manages it, skills in `.agents/skills/` and their
  copies in `.claude/skills/` among them, `CLAUDE.md`, the deny rules in `.claude/settings.json`, and the blocks of
  this file between `basis sync`'s markers. Never edit them: `basis check`
  fails when one differs from what the installed version writes, and `uv run basis sync` rewrites it, so a change to
  one is a change to basis. Everything else, in this file too, is the repo's own. A Debian package the image needs
  goes in `pyproject.toml`'s `[tool.basis] apt-packages`, such as `["libpq5"]`, never in the Dockerfile.
- A file of the repo's own at a path basis writes, such as a Dockerfile written before basis, is left alone, and
  `basis check` says so. `uv run basis sync --adopt` saves it as `NAME.before-basis` and writes basis's: compare the
  two, move what the repo needs where basis reads it, and delete the old one.
