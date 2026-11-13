# basis

basis is a set of Python packages that give a firm's APIs, batch jobs and, once
[phase 14](docs/dash-plan.md) builds them, Dash apps one shape, built for a codebase that agents write and checks
review: `basis-api` and `basis-job`, on an internal `basis-core`, with one `basis` command in every repo. Web UIs are
built with its TypeScript sibling, [basis-ui](https://github.com/jacobragsdale/basis-ui). Both names are placeholders.

The repo holds the packages, in `packages/`, and beside them the design and the build order:

- [The basis design](docs/design.md): what each package is, how they fit, the contract a UI can rely on, and why.
- [Roadmap](docs/roadmap.md): the phases, the steps in each, and which run at the same time.

basis grew out of [etl-framework](https://github.com/jacobragsdale/etl-framework), which moved into it in the roadmap's
phase 1, and the repo that is now [trellis](https://github.com/jacobragsdale/trellis).

## License

MIT.
