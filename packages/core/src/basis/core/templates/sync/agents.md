## basis

This repo's one command is `basis`, and `uv run basis --help` lists every verb it has here. Every failure prints one
line that starts with `ERROR`, gives the reason and ends with the fix.

- Commit and push with the hooks in `.githooks/`, which any `basis` command turns on. The commit hook fixes and
  formats the staged Python files with ruff, then runs `basis check`; the push hook runs `basis check`. Never skip
  either with `--no-verify` or `-n`: when one fails, fix what each `ERROR` line says, and commit or push again.
- basis writes, and owns: each file whose first lines say basis manages it, `CLAUDE.md`, the skills in
  `.agents/skills/` and their copies in `.claude/skills/` whose names start with a part's, the deny rules in
  `.claude/settings.json`, and the blocks of this file between `basis sync`'s markers. Never edit them: `basis check`
  fails when one differs from what the installed version writes, and `uv run basis sync` rewrites it, so a change to
  one is a change to basis. Everything else, in this file too, is the repo's own.
