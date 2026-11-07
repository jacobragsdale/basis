# Agent guide

This repo is basis (a placeholder name): the Python packages that give a firm's APIs, batch jobs and portfolio
optimizers one shape. UIs live in its TypeScript sibling, [basis-ui](https://github.com/jacobragsdale/basis-ui), and
meet basis at the contract in [docs/design.md](docs/design.md#what-a-ui-can-rely-on). [docs/design.md](docs/design.md)
holds the decisions and their reasons, and [docs/roadmap.md](docs/roadmap.md) the phases, their steps, and the lanes
that run at the same time. Code arrives in the roadmap's phase 1, when etl-framework moves in; until then the repo is
its docs.

## Working here

- Read `docs/design.md` before changing anything. A decision there changes only when the maintainer says so, and the
  page changes in the same commit.
- Work one roadmap step per branch and git worktree, named after the step's id, such as `1.3-extract-core`, and follow
  the roadmap's rules for working in parallel.
- Changes to `basis-core` land one pull request at a time.
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
uv sync
uv run pre-commit install           # once per clone: the commit hook runs the checks below
uv run pre-commit run --all-files   # ruff, ty with every rule an error, the lockfile, the import contracts, file hygiene
uv run pytest                       # every package's unit tests, optim's among them, from the root
uv run pytest -m integration        # real SQL Server and Oracle, every package's: packages/job/tests/integration/README.md
```

To check what `basis-job new repo` generates, from the root:

```sh
uv run basis-job new repo pricing-etl --dir SCRATCH --framework-path .   # its first job is positions
cd SCRATCH/pricing-etl && uv run basis new job rates
uv run basis check                  # the framework files, the hooks, the lockfile, ruff, ty and pytest
git add --all && git commit -m "First commit"   # the commit hook fixes, formats and runs basis check
```

To check what `optim new repo` generates, from the root, then as the Checks section of
[packages/optim/AGENTS.md](packages/optim/AGENTS.md) goes on:

```sh
uv run optim new repo sma income --dir SCRATCH --framework-path .   # its first model is income
cd SCRATCH/sma && uv run basis new model ladder
uv run basis check
```

A repo whose image builds needs basis from git rather than a checkout: push the branch, and make one with
`uvx --from 'git+https://github.com/jacobragsdale/basis@BRANCH#subdirectory=packages/job' basis-job new repo loads`,
which takes basis from the commit uvx installed. CI runs all of this on every pull request that changes more than
docs, and builds that repo's image and publishes its job to SQL Server.

## The basis command

`basis` is basis-core's console script, and each part adds its verbs to it; `packages/core/src/basis/core/verbs.py`
holds the interface, and its docstring is the reference.

- A part lists one entry point in the `basis.verbs` group, named after the part (`job`, `api`, `optim`), that
  points at a callable returning its `Part`: its own `verbs`, each a `Verb` with a name, a one-line help, a function
  that adds its arguments to an argparse parser, and a handler that returns the exit code; and the fields core's verbs
  use, `files` (`sync`, `check`; read only when used), `docs` (`docs`), `new` (`new KIND NAME`), `runs` (`run`,
  `describe`, `resume`, `replay`, `regress` and `ddl`, which jobs and models both have), and `renamed`, which refuses
  the variables an earlier version of the part read, before any verb runs, as `basis` refuses `ETL_*`.
- A repo declares its parts in its `pyproject.toml`, as `[tool.basis] parts = ["job"]`, and basis never infers one
  from a folder. `basis` loads only those parts, and `basis --help` lists their verbs and each core verb one of them
  gives something to do. A declared part that is not installed, or a verb of one that is not declared, is refused.
- No two declared parts add the same verb, no part adds one twice, and none adds one of core's. A verb that several
  kinds of part share belongs in core, reaching each part through a field of `Part`, as the verbs of `runs` do: core
  parses NAME, and RUN_ID for `resume` and `replay`, and the `Shared` of the part that runs NAME adds and reads the
  rest, so a job's `run` takes `--interval` and a model's `--portfolios`. Where several declared parts have the verb,
  NAME comes first, and a name two of them run is refused.
- Every failure prints one line on stderr, `ERROR: REASON: FIX`: a handler returns `fail(reason, fix)`, or raises a
  `basis.core.errors.BasisError` whose message ends with the fix; a pydantic `ValidationError` of settings or params
  prints as one line too. Any other exception is a defect, whose traceback is logged first. A call that raises a
  builtin error on purpose, such as a lookup that finds no run, goes in `with deliberate(LookupError):`. Results a
  caller reads are JSON on stdout.
- `basis sync` owns the framework files (`basis.core.sync.REPO_FILES`: the Dockerfile, `azure-pipelines.yml`, the
  hooks in `.githooks/` and `.gitattributes`, from `packages/core/src/basis/core/templates/sync/`), basis's own block
  of `AGENTS.md` and each part's, `CLAUDE.md`, each `.agents/skills/PART-*` folder and its copy in `.claude/skills/`,
  the deny rules in `.claude/settings.json`, and each file whose first lines carry `basis.core.sync.MARKER`. It writes
  them with LF line endings, removes those nothing writes now, and `basis check` reports any that differ, byte for
  byte. A part adds its lines of the Dockerfile as its `AgentFiles.image`, and a repo its Debian packages as
  `[tool.basis] apt-packages`. `sync.plan` works this out once per call.
- A file of the repo's own at a path basis writes (one without the marker, a `CLAUDE.md` holding anything else, or
  settings with none of basis's rules) is left alone, and a file basis cannot read, such as settings that are not
  JSON, is one `ERROR` line; every other file is still checked and written. `basis sync --adopt` saves each file of
  the repo's own as `NAME.before-basis` and writes basis's.
- `basis check`, `sync` and `new` point a clone that names no hooks folder at the repo's `.githooks/`
  (`basis.core.hooks.ensure`); the verbs a container runs, such as `run`, leave git alone. `basis check` fails, with
  the fix, when the clone runs other hooks or is no clone. The hooks are POSIX sh, which Git for Windows runs too. The
  commit hook ends with `basis check --staged`, whose checks of the staged files are `basis.core.hygiene`'s; the push
  hook checks each pushed commit, in a temporary worktree inside the git folder when it is not a clean HEAD.
- A command that runs before a repo exists, such as `basis-job new repo NAME`, is a `standalone` command in
  `basis.core.command`, with the same failures.
