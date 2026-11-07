"""basis-core: the plumbing every basis part shares, which no user code imports. Each part (`basis.job`, `basis.api`,
`optim`) depends on it at exactly its own version, and adds its verbs to the `basis` command it provides.

`basis` is a namespace package: each distribution adds one portion, so there is no `basis/__init__.py`."""
