`uv run pytest` fails in this repo: the `positions` job refuses its own fixture, which holds good data. Find out why and fix it. Don't change the fixture, the params or the tests.

When you're done, the repo's checks should pass.
