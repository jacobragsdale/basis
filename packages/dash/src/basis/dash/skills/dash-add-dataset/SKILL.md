---
name: dash-add-dataset
description: Add a dataset to a basis-dash app, a query, a file, another service's rows or a view, with its row, its fixture and its tests. Use when the user wants the app to read a new table, query, file or feed, or to compute a new summary.
---

# Add a dataset

1. Decide what it reads. A database's rows are `bd.query`; a shared folder's or a lake's file `bd.file`; another
   service's JSON rows `bd.fetch`; and anything computed from datasets the app already has is `bd.view`, which needs
   no new source. Prefer a view: filters, summaries and joins belong in one, over a query's cached frame.
2. Its row, in `data.py`: a `bd.Row` with each column the source answers, typed, each number a float with its format
   (`Annotated[float, bd.format.money("USD", 0)]`), a column that may be missing `| None`.
3. Its declaration, in `data.py`, once, by name:
   - `bd.query(NAME, WAREHOUSE, "sql/NAME.sql", Row, binds={"as_of": date}, ttl_s=900, stale_s=86_400)`, with
     `sql/NAME.sql` binding each param in its database's dialect, `%(as_of)s` for SQL Server or `:as_of` for Oracle,
     and nothing else; bind the few params that change what is read, never a list;
   - `bd.view(NAME, domain.FUNCTION, Row, INPUT=DATASET)`, with `FUNCTION(INPUT: pd.DataFrame, PARAM: str, ...)` in
     `domain.py`, pure, returning a frame of its row;
   - `smoke={"as_of": ...}`, or a function of no arguments that returns the params, where `basis smoke` should load it
     after a deploy.
4. Its fixture, for a query: `fixtures/NAME.csv`, a synthetic row per line, each column the row's, read through the row
   as a real answer would be; a bind that names a column filters it, as `as_of` does.
5. Load it to see it: `uv run basis load NAME --param as_of=2026-11-12` prints its first rows and how the cache
   answered; with `--env dev`, from dev's database.
6. Tests: a view's function in `tests/test_domain.py` on plain frames, an empty one among them.
7. Run `uv run basis check`, and fix each `ERROR` line as it says: a SQL file that binds a name the query does not
   declare, a view's function that imports Dash, or a fixture that does not match its row each fail there.
