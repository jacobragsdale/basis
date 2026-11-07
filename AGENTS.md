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

## Checks

```sh
uv sync
uv run pre-commit install           # once per clone: the commit hook runs the checks below
uv run pre-commit run --all-files   # ruff, ty with every rule an error, the lockfile, file hygiene
uv run pytest
```

CI runs the same on every pull request that changes more than docs.
