---
name: dash-why-failed
description: Find out why a basis-dash page shows an error, an empty or stale view, or "Not updated", from its notification, its logs and a replay with basis render. Use when the user asks why a page, grid, chart or callback failed or shows nothing.
---

# Why a page failed

1. Read the notification. Each failure shows one sentence at the page's bottom right, with the request's id:
   `warehouse did not answer within 30 s.` is the database's deadline, `could not be reached` its network or login,
   `refused this app` a permission, and `Something went wrong.` a defect in the app's code.
2. Find the request in the logs by that id: every record of it carries `request_id` and `trace_id`. A defect is logged
   once, `a callback failed`, with its traceback, which names the line of the repo's code that raised; a resource's
   failure names its `resource`, `kind` and `reason`, and has no traceback. Each load is `loaded DATASET`, with its
   `params`, `rows`, `ms` and `cache`; each answer `answered OUTPUT`, with its `bytes`.
3. A view marked "Stale" is a dataset whose reload failed inside its `stale_s`: it shows the last good value, and the
   logs hold one warning for it. "Not updated" marks an output whose last callback failed: it keeps what it showed.
4. Make it again on this machine, on the fakes: `uv run basis render PAGE --set ID.PROP=VALUE` runs the page's
   callbacks with the values the person had, which the URL's query string holds for its filters, and prints each
   output and each failure. With `--env dev` it reads dev's databases. `uv run basis load DATASET --param ...` reads
   one dataset alone.
5. A fake fails on demand, as the real database would: `fixtures.database(WAREHOUSE).fails(Timeout, query="NAME")`
   in a test, with `from basis.core.errors import Timeout`.
6. Fix it with a test first, in `tests/test_pages.py` or `tests/test_domain.py`, then `uv run basis check`.
