# Agent guide

This repo is basis (a placeholder name): the packages that give a firm's APIs, batch jobs, portfolio optimizers and
React UIs one shape. [docs/design.md](docs/design.md) holds the decisions and their reasons, and
[docs/roadmap.md](docs/roadmap.md) the phases, their steps, and the lanes that run at the same time. Code arrives in the
roadmap's phase 1, when etl-framework moves in; until then the repo is its docs.

## Working here

- Read `docs/design.md` before changing anything. A decision there changes only when the maintainer says so, and the
  page changes in the same commit.
- Work one roadmap step per branch and git worktree, named after the step's id, such as `1.3-extract-core`, and follow
  the roadmap's rules for working in parallel.
- Changes to `basis-core` land one pull request at a time.
- A spike writes its result to `docs/spikes/NAME.md`: the versions it used, a yes or no for each question, and the
  fallback for each no.
- A step marked **(work)** needs the firm's environment. Hand it to the maintainer as the commands to run and what to
  send back.
- Write "the firm", never a firm's real name, and use placeholder names in examples. This repo is public.

## Checks

None yet. Roadmap step 1.1 adds the workspace, its checks and CI.
