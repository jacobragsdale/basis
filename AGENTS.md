# Agent guide

This repo is basis (a placeholder name): the Python packages that give a firm's APIs, batch jobs and Dash apps one
shape. UIs live in its TypeScript sibling,
[basis-ui](https://github.com/jacobragsdale/basis-ui), and meet basis at the contract in
[docs/design.md](docs/design.md#what-a-ui-can-rely-on). [docs/design.md](docs/design.md) holds the decisions and their
reasons, and [docs/roadmap.md](docs/roadmap.md) the phases, their steps, and the lanes that run at the same time,
[PLAN.md](PLAN.md) the review after the MVP and its phases 6 to 12, and [docs/dash-plan.md](docs/dash-plan.md) phases 13
to 18: one part's removal, and Dash apps. The code is in `packages/`: basis-core, basis-job, basis-api and basis-dash.

## Working here

- Read `docs/design.md` before changing anything. A decision there changes only when the maintainer says so, and the
  page changes in the same commit.
- Work one roadmap step per branch and git worktree, named after the step's id, such as `1.3-extract-core`, and follow
  the roadmap's rules for working in parallel.
- Changes to `basis-core` land one pull request at a time.
- Core names no part, and parts import core and never each other, so a part can change, or go, and core, the other
  parts, their release and the contract a UI relies on stay as they are. A mechanism in core that only one part uses
  must be one a job could use as it stands. The import contracts in the root `pyproject.toml` check what core and each
  part import.
- A change to the contract a UI relies on is a change to basis-ui's too: say so in the pull request, and update
  basis-ui's pinned sample.
- A spike writes its result to `docs/spikes/NAME.md`: the versions it used, a yes or no for each question, and the
  fallback for each no.
- A step marked **(work)** needs the firm's environment. Hand it to the maintainer as the commands to run and what to
  send back.
- Write "the firm", never a firm's real name, and use placeholder names in examples. This repo is public.
- grimp's and import-linter's caches stay off (`cache_dir=None`, `lint-imports --no-cache`), here and in generated
  repos: their folders clutter every checkout, and the graphs build fresh in well under a second.

## Checks

```sh
uv sync --all-packages              # every member, as CI installs them
uv run pre-commit install           # once per clone: the commit hook runs the checks below
uv run pre-commit run --all-files   # ruff, ty with every rule an error, dead code, the lockfile, the import contracts, file hygiene
uv run pytest -n auto               # every package's unit tests, and scripts/tests and evals/tests, from the root
uv run scripts/release.py check     # every member at the root's one version, pinning basis-core at it (docs/releasing.md)
uv run pytest -m integration        # real SQL Server and Oracle, every package's: packages/job/tests/integration/README.md
```

To check what `basis-job new repo` generates, from the root:

```sh
uv run basis-job new repo pricing-etl --dir SCRATCH --framework-path .   # its first job is positions
cd SCRATCH/pricing-etl && uv run basis new job rates
uv run basis check                  # the framework files, the hooks, the lockfile, ruff, ty and pytest
git add --all && git commit -m "First commit"   # the commit hook fixes, formats and runs basis check
```

To check what `basis-api new app` generates, from the root:

```sh
uv run basis-api new app pricing --dir SCRATCH --framework-path .   # its first job beside it is positions
cd SCRATCH/pricing && uv run basis check
```

A repo whose image builds needs basis from git rather than a checkout: push the branch, and make one with
`uvx --from 'git+https://github.com/jacobragsdale/basis@BRANCH#subdirectory=packages/job' basis-job new repo loads`,
which takes basis from the commit uvx installed. CI runs all of this on every pull request that changes more than
docs, and builds that repo's image and publishes its job to SQL Server.

A release is a `v*` tag. The root `pyproject.toml`'s `[project] version` is every package's, which
`uv run scripts/release.py version X.Y.Z` sets, and each package says in its own `pyproject.toml` whether a release
publishes it (`[tool.basis-release] publish`). [docs/releasing.md](docs/releasing.md) says how to cut a release, and
what the release workflow's dry run proves on each pull request that touches a release file.

## The basis command

`basis` is basis-core's console script, and each part adds its verbs to it; `packages/core/src/basis/core/verbs.py`
holds the interface, and its docstring is the reference.

- A part lists one entry point in the `basis.verbs` group, named after the part, such as `job` or `api`, that
  points at a callable returning its `Part`: its own `verbs`, each a `Verb` with a name, a one-line help, a function
  that adds its arguments to an argparse parser, and a handler that returns the exit code; and the fields core's verbs
  use, `files` (`sync`, `check`; read only when used), `docs` (`docs`), `new` (`new KIND NAME`), `runs` (`run`,
  `describe`, `resume`, `replay`, `regress`, `runs` and `ddl`, which every part that runs things by name has, and
  `regressions`, for `check --full`), `qa` (the QA tier), `smoke` (`smoke --env ENV`: the part's checks of a deployed environment), `contract` (`check --full` and `review`, such as basis-api's breaking-change check of
  `openapi.json`), `checks` (`check`: the part's own checks of the repo's code, each problem one `ERROR` line, such as
  basis-api's layers and its app on its fakes), `full_checks` (`check --full`: those too slow for every commit, such as
  basis-api's Schemathesis requests), and `renamed`, which refuses the
  variables an earlier version of the part read, before any verb runs, as `basis` refuses `ETL_*`.
- A repo declares its parts in its `pyproject.toml`, as `[tool.basis] parts = ["job"]`, and basis never infers one
  from a folder. `basis` loads only those parts, and `basis --help` lists their verbs and each core verb one of them
  gives something to do. A declared part that is not installed, or a verb of one that is not declared, is refused.
  What the repo declares to a part beyond that is the part's own table, `[tool.basis.PART]`, which the part reads with
  `basis.core.verbs.table` and checks strictly; the table of a part the repo does not declare is refused.
- No two declared parts add the same verb, no part adds one twice, and none adds one of core's. A verb that several
  kinds of part share belongs in core, reaching each part through a field of `Part`, as the verbs of `runs` do: core
  parses NAME, and RUN_ID for `resume` and `replay`, and the `Shared` of the part that runs NAME adds and reads the
  rest, so a job's `run` takes `--interval`, and another part's `run` takes options of its own. Where several declared
  parts have the verb, NAME comes first, and a name two of them run is refused. Nothing in core is for one part alone:
  a mechanism only one part uses must be one a job could use as it stands, and core names no part.
- Every command's stderr ends with one log record, `finished VERB`, with its `verb`, `exit_code` and `ms`, whatever
  the outcome, and then, for each failure, one line, `ERROR: REASON: FIX`: so a failure's ERROR line is the last line,
  and the only one starting `ERROR`. The other stderr lines are JSON log records, for machines. A handler returns
  `fail(reason, fix)`, or raises a `basis.core.errors.BasisError` whose message ends with the fix; a pydantic
  `ValidationError` of settings or params prints as one line too; `basis` holds each ERROR line back until the record.
  Any other exception is a defect, whose traceback is logged first. A call that raises a
  builtin error on purpose, such as a lookup that finds no run, goes in `with deliberate(LookupError):`. Results a
  caller reads are JSON on stdout.
- `basis sync` owns the framework files (`basis.core.sync.REPO_FILES`: the Dockerfile, `azure-pipelines.yml`, the
  hooks in `.githooks/` and `.gitattributes`, from `packages/core/src/basis/core/templates/sync/`), the repo's
  `ruff.toml`, `ty.toml` and `pytest.ini` where every declared part gives its share of them (`basis.core.tools`), with
  the repo's `[tool.basis] exceptions`, each a rule at a path with its reason, basis's own block
  of `AGENTS.md` and each part's, `CLAUDE.md`, the deny rules and agent hooks in `.claude/settings.json`, the agent
  hooks in `.cursor/hooks.json` and `.codex/hooks.json` (`basis.core.guard`, which `basis hook` runs), and each file whose first
  lines carry `basis.core.sync.MARKER`: each skill in `.agents/skills/` and its copy in `.claude/skills/` among them,
  whose front matter's `metadata` starts with the marker (`sync.SKILL_KEY`), so a skill is basis's whatever its name
  and whether or not the part that wrote it is still installed, and each file a part makes from the repo's own code
  (`AgentFiles.made`), such as an API's `openapi.json`, whose first key is the marker. It writes them with LF line
  endings, removes those nothing writes now, and `basis check` reports any that differ, byte for byte. A part makes a
  file only at a path relative to the repo, outside the folders basis never reads, that neither basis nor another part
  writes, and a file it cannot make is one `ERROR` line. A part adds its lines of the Dockerfile as its
  `AgentFiles.image`, and a repo its Debian packages as `[tool.basis] apt-packages`. `sync.plan` works this out once
  per call.
- A file of the repo's own at a path basis writes (one without the marker, a skill's `SKILL.md` among them, a
  `CLAUDE.md` holding anything else, or settings with none of basis's rules) is left alone, and a file basis cannot
  read, such as settings that are not JSON, is one `ERROR` line; every other file is still checked and written.
  `basis sync --adopt` saves each file of the repo's own as `NAME.before-basis` and writes basis's.
- `basis check`, `sync` and `new` point a clone that names no hooks folder at the repo's `.githooks/`
  (`basis.core.hooks.ensure`); the verbs a container runs, such as `run`, leave git alone. `basis check` fails, with
  the fix, when the clone runs other hooks or is no clone. The hooks are POSIX sh, which Git for Windows runs too. The
  commit hook ends with `basis check --staged`, whose checks of the staged files are `basis.core.hygiene`'s; the push
  hook checks each pushed commit, in a temporary worktree beside the clone when it is not a clean HEAD.
- `basis check` (`basis.core.check`) holds every repo's tests to the gates (`basis.core.gates`): the coverage floor
  `[tool.basis] coverage` gives, which pytest-cov measures as basis configures it, repository modules left out; no
  `unittest.mock` or pytest-mock but in a file with a line `# basis: allow-mocks -- WHY`; no `# noqa`, `# ty: ignore`,
  `# pragma: no cover` or skip of a test without its reason, as `# noqa: S307 -- WHY`, each of which `basis review`
  lists where a change adds it, beside the tests it removes; and no socket in a unit test,
  which basis-core's pytest plugin (`basis.core.pytest_plugin`, `pytest11`'s `basis`) fails, and tells the check of.
  This workspace's own session leaves the plugin out (`-p no:basis`), since its tests serve and reach real servers.
- `basis check --full` (`basis.core.full`), which the push hook and the pipeline run, adds each part's `full_checks`, `regress` from the process's
  `BASIS_SNAPSHOT_ROOT` where it is reachable, the QA tier where the repo declares `[tool.basis.qa]` and the machine has
  its settings file, each part's `contract`, and mutation testing of the domain functions the change touches, in WSL
  and on Linux (`basis.core.mutation`); a piece that does not run prints one `SKIPPED:` line. An output
  `regress` sees change fails nothing, and nor does a mutant that survives. It records what it found for `basis review` in the git folder the clone's
  worktrees share (`basis/checks/PREFIX/COMMIT.json`), when the repo's folder has nothing uncommitted. The push hook
  checks a commit that is not a clean HEAD in a worktree of its own, with `--settings-from` naming the clone's folder,
  where the untracked QA settings are.
- The QA tier (`basis.core.qa`) keeps the end-to-end safeguards, all declared in `[tool.basis.qa]`: `settings`, the
  one file it reads, never the environment; `marker`, which every database those settings configure must carry, in a
  SQL Server connection string's one server or database, or an Oracle DSN; `stores`, one of which
  `BASIS_SNAPSHOT_ROOT` must be; and `writes`, the tables it may write, by database, ledgers included. While any
  setting or any part's plan is refused, nothing runs, and the databases a part gets refuse other tables as they
  write. A part's own runs are its `[tool.basis.qa.PART]` table, such as basis-job's `[tool.basis.qa.job]`.
- `basis doctor` (`basis.core.doctor`) runs in a repo or outside one, and checks uv, git, the hooks, the feed (the
  user's `uv.toml` default index) and the settings; `basis upgrade` (`basis.core.upgrade`) relocks basis-core and the
  package of each installed part, then runs `basis sync` and `basis check --full` through `uv run`, so the new version does them.
- A command that runs before a repo exists, such as `basis-job new repo NAME`, is a `standalone` command in
  `basis.core.command`, with the same failures.
