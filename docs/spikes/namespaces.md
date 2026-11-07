# Spike 0.2: namespaces

Phase 1 uses the namespace names `basis.core`, `basis.job` and `basis.api`, as the design says: two uv workspace members
that each add a portion to one `basis` namespace work with every tool basis uses. Run on 2026-11-06.

## Versions

uv 0.13.6 with `uv_build` 0.13.6, Python 3.14.7, ruff 0.17.4, ty 0.0.99, grimp 3.19, import-linter 2.16 and pytest
9.2.0.

## Setup

A uv workspace whose root is not a package (`[tool.uv] package = false`), with members `packages/core` and
`packages/job`. Each member's `pyproject.toml` names its portion with a dotted module name, and neither has a
`basis/__init__.py`:

```toml
[tool.uv.build-backend]
module-name = "basis.core"
```

`basis-job` depends on `basis-core==0.0.1`, an exact pin, and the workspace sources resolve it to the member.

## Answers

| Question | Answer |
|---|---|
| Do editable installs from the workspace import across portions? | Yes. `basis.__path__` holds both members' `src/basis`, and `basis.job` imports `basis.core`. |
| Does ruff check both portions? | Yes, with no configuration. |
| Does ty resolve imports across portions, and report errors through them? | Yes. A wrong return type that uses a `basis.core` function from `basis.job` is reported as `invalid-return-type`. |
| Does grimp build a graph of portions? | Yes, with each portion named: `grimp.build_graph("basis.core", "basis.job")`. |
| Does an import-linter contract across portions hold and fail? | Yes. `root_packages = ["basis.core", "basis.job"]` with a forbidden contract keeps, and reports `basis.core -> basis.job (l.3)` when the import is added. |
| Do the wheels hold only their portion? | Yes. `basis_core-0.0.1-py3-none-any.whl` holds `basis/core/` and `basis_job-0.0.1` holds `basis/job/`, neither with `basis/__init__.py`. Both wheels installed into a fresh venv import and run. |

## Two things phase 1 must do

**pytest needs `--import-mode=importlib`.** Two packages with a test file of the same name
(`packages/core/tests/test_basic.py` and `packages/job/tests/test_basic.py`) fail collection in pytest's default mode,
and pass with `--import-mode=importlib`. The workspace's pytest configuration sets it.

**A repo's contract test must name basis's portions.** With `include_external_packages=True`, grimp collapses an
installed namespace to its top-level name: a user package's graph shows one external module, `basis`, so it cannot tell
an allowed import of `basis.job` from a forbidden one of `basis.core.databases`. Naming the portions as packages of the
graph fixes it: `grimp.build_graph("jobs", "basis.job", "basis.core", include_external_packages=True)` found the chain
`jobs.loads.job -> basis.job.publish -> basis.core.databases`. The contract-test helpers in `basis.core` build their
graph that way, and each part's contract module (such as `basis/job/__init__.py`) keeps importing nothing that does I/O,
as etl-framework's does today.

## Fallback

None needed. Had the namespace failed, the fallback was top-level names, `basis_core` and `basis_job`.
