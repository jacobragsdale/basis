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
uv run pytest                       # every package's unit tests, from the root
uv run pytest -m integration        # real SQL Server and Oracle: packages/job/tests/integration/README.md
```

To check what `basis-job new repo` generates, from the root:

```sh
uv run basis-job new repo pricing-etl --dir SCRATCH --framework-path .   # its first job is positions
cd SCRATCH/pricing-etl && uv run basis new job rates
uv run basis check                  # the agent files, the lockfile, ruff, ty and pytest, as a commit needs them
```

CI runs the same on every pull request that changes more than docs.

## The basis command

`basis` is basis-core's console script, and each part adds its verbs to it; `packages/core/src/basis/core/verbs.py`
holds the interface, and its docstring is the reference.

- A part lists one entry point in the `basis.verbs` group, named after the part (`job`, later `api` and `optim`), that
  points at a callable returning its `Part`: its own `verbs`, each a `Verb` with a name, a one-line help, a function
  that adds its arguments to an argparse parser, and a handler that returns the exit code; and the fields core's verbs
  use, `files` (`sync`, `check`; read only when used), `docs` (`docs`), `new` (`new KIND NAME`) and `runs` (`replay`,
  `regress`).
- A repo declares its parts in its `pyproject.toml`, as `[tool.basis] parts = ["job"]`, and basis never infers one
  from a folder. `basis` loads only those parts, and `basis --help` lists their verbs and each core verb one of them
  gives something to do. A declared part that is not installed, or a verb of one that is not declared, is refused.
- No two declared parts add the same verb, no part adds one twice, and none adds one of core's. A verb that several
  kinds of part share belongs in core, reaching each part through a field of `Part`, as `replay` does.
- Every failure prints one line on stderr, `ERROR: REASON: FIX`: a handler returns `fail(reason, fix)`, or raises a
  `basis.core.errors.BasisError` whose message ends with the fix; a pydantic `ValidationError` of settings or params
  prints as one line too. Any other exception is a defect, whose traceback is logged first. A call that raises a
  builtin error on purpose, such as a lookup that finds no run, goes in `with deliberate(LookupError):`. Results a
  caller reads are JSON on stdout.
- `basis sync` owns each part's block of `AGENTS.md`, each `.agents/skills/PART-*` folder, and each file whose first
  lines carry `basis.core.sync.MARKER`; it removes those no installed part writes now, and `basis check` reports them.
- A command that runs before a repo exists, such as `basis-job new repo NAME`, is a `standalone` command in
  `basis.core.command`, with the same failures.
