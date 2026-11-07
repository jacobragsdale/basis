# basis

basis is a set of Python packages that give a firm's APIs, batch jobs and portfolio optimizers one shape, built for a
codebase that agents write and checks review: `basis-api`, `basis-job` and `optim`, on an internal `basis-core`, with
one `basis` command in every repo. optim's design is provisional until the maintainer meets the firm's quant team, and
basis releases it only then; nothing else in basis depends on it. Web UIs are built with its TypeScript sibling,
[basis-ui](https://github.com/jacobragsdale/basis-ui). Both names are placeholders.

Nothing is built yet. The repo holds the design and the build order:

- [The basis design](docs/design.md): what each package is, how they fit, the contract a UI can rely on, and why.
- [Roadmap](docs/roadmap.md): the phases, the steps in each, and which run at the same time.

basis grows out of [etl-framework](https://github.com/jacobragsdale/etl-framework) and
[portfolio-optim](https://github.com/jacobragsdale/portfolio-optim), which move into it in the roadmap's phases 1 and 3.

## License

MIT.
