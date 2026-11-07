# How to add a dataset

Give a model another table to read, such as issuer credit scores from Oracle or a per-portfolio restricted list from
SQL Server. A dataset is one module in the model's `datasets/` folder, with its row model, source, query, key and cache
lifetime, and the model reads it as a typed attribute of its `Data`.

Prerequisites:

- A model repo, with the model's tests passing.
- The query, and, for a run on live data, read access to the source and its `OPTIM_*` settings in `.env` (see
  [how to run on live data](run-on-live-data.md)). The fixture lets you develop without them.

To add a dataset:

1. Decide whose it is. A dataset one model reads goes in that model's `datasets/`. One that two models of the repo read
   goes in `models/shared/datasets/` (see [how to share code between models](share-code-between-models.md)), and one
   that two repos read belongs in `optim.datasets`, which the package's owner reviews. If you need another column of
   the shared holdings or securities, do not copy the shared dataset: declare a dataset of your own keyed on a column the
   shared one has, such as `security_id` or `issuer`, and join it in `prep.py`.
2. Copy the module nearest to it in `datasets/`, such as `signals.py` for a table every portfolio shares or
   `controls.py` for one read per portfolio, to a module named for the dataset, such as `datasets/issuer_scores.py`.
   Write its row model: field names are the column names the query returns. Document each field with a docstring under
   it, and put every rule in the annotation: rows are validated a column at a time, so a row model with a
   `@field_validator` is refused.

   ```python
   """Each issuer's internal credit score, from Oracle: one table that every portfolio shares."""

   from datetime import timedelta
   from pathlib import Path
   from typing import Annotated

   from optim import Dataset, Row
   from pydantic import Field

   SQL = Path(__file__).parent / "sql"


   class IssuerScore(Row):
       issuer: str
       """The issuer, named as the securities name it."""
       score: Annotated[float, Field(ge=0, le=100)]
       """The issuer's internal credit score; higher is safer."""


   ISSUER_SCORES = Dataset("issuer_scores", IssuerScore, source="oracle", query=SQL / "issuer_scores.sql", key=("issuer",), cache=timedelta(hours=12))
   ```

   A field is a `float`, `int`, `str`, `bool`, `date` or `Literal[...]`, or any of them `| None` where a value may be
   missing. Every number is a float or an int: a driver's `Decimal` becomes a float as rows are validated.
   `optim.schema` has field types such as `Id`, `Cusip`, `Rating` and `Weight`.
3. Declare where it comes from in the `Dataset`. `source` is `"oracle"`, `"mssql"`, `"rest"`, whose `query` is the
   API's path, such as `"/v1/expected-returns"`, or `"file"`, whose `query` is a file's path or URL, in which
   `{as_of_date}` is replaced by the business date, as in `"//share/signals/{as_of_date}.parquet"`. The `key` columns
   must be unique. Leave out `cache` to read the dataset live on every run: one that changes during the day, or is read
   per portfolio, should not be cached. `check=` takes a function of the frame that raises `ValueError` for rules
   across rows, such as weights that must sum to 1.
4. Write a database's SQL in `datasets/sql/`, as `issuer_scores.sql`, aliasing each column to its field name. Every
   query takes only the business date and, for a dataset read per portfolio, the batch's portfolio ids, a JSON array:

   | Source | Date | Portfolio ids |
   |---|---|---|
   | Oracle | `:as_of_date` | `IN (SELECT id FROM JSON_TABLE(:portfolios, '$[*]' COLUMNS (id VARCHAR2(64) PATH '$')))` |
   | SQL Server | `%(as_of_date)s` | `IN (SELECT id FROM OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$'))` |

   ```sql
   -- Oracle. Each issuer's latest internal credit score on the business date.
   SELECT s.issuer_name AS issuer,
          s.score
   FROM   credit.issuer_scores s
   WHERE  s.score_date = (SELECT MAX(score_date) FROM credit.issuer_scores WHERE score_date <= :as_of_date)
   ```

   A row model with a `portfolio_id` field is read per portfolio: in batches of portfolio ids, and sliced to each
   portfolio before its hooks see it. Its key includes `portfolio_id`. Oracle refuses a bind its SQL does not use, so
   an Oracle query must use `:as_of_date`, and a per-portfolio one `:portfolios` too. Never let a param change the
   query: select rows after loading instead, so a sweep can run every variant from one snapshot.
5. Add it to `Data` in `model.py`, importing it beside the others:

   ```python
   from models.income.datasets.issuer_scores import ISSUER_SCORES


   class Data(optim.Datasets):
       portfolios = PORTFOLIOS
       signals = SIGNALS
       controls = CONTROLS
       mandates = MANDATES
       issuer_scores = ISSUER_SCORES
   ```

6. Add a CSV file of a few rows to the model's fixture, `tests/fixture/issuer_scores.csv`, named as the dataset, with a
   header row of the field names; an empty cell is null:

   ```text
   issuer,score
   Issuer A,82
   Issuer B,64
   ```

   Until it exists, the contract test fails with `has no file for ['issuer_scores']`.
7. Read it. A column every rule should see goes in `prep.py`, joined onto the market or a portfolio's universe:

   ```python
   joined = data.securities.merge(data.signals, on="security_id", how="left", validate="one_to_one")
   joined = joined.merge(data.issuer_scores, on="issuer", how="left", validate="many_to_one")
   ```

   A rule reads a dataset from the portfolio's slice of the data instead, as `portfolio.data.issuer_scores`: whole for
   a dataset read whole, and the portfolio's own rows for one read per portfolio, as `rules/controls.py` reads
   `portfolio.data.controls`. Pass `validate=` to every merge, so a duplicated key fails instead of multiplying rows.
8. Test what reads it on a few rows. `tests/builders.py`'s `portfolio(...)` takes the model's other datasets as
   keywords, built with `optim.testing.frame`:

   ```python
   def test_every_bond_has_its_issuers_score_and_one_without_a_score_has_none() -> None:
       scores = frame(ISSUER_SCORES, {"issuer": "Issuer A", "score": 82.0})

       found = portfolio({"security_id": "A"}, {"security_id": "B", "issuer": "Issuer B"}, issuer_scores=scores)

       assert found.universe.set_index("security_id")["score"].to_dict() == {"A": 82.0, "B": pytest.approx(float("nan"), nan_ok=True)}
   ```

To confirm it worked, run `uv run pytest`: the contract test loads the new file through the row model and decides every
portfolio of the fixture with it. `uv run optim describe MODEL` then lists `issuer_scores` under `datasets`, with its
source and columns. A snapshot saved before the dataset existed has no file for it, so it cannot be replayed with the
new code.

A dataset declared as `portfolios` is special: its query takes only the business date, it lists the portfolios the
model manages, one row each, and a run given no portfolios decides every one it lists. The example's
`datasets/portfolios.py` is one. Each portfolio's hooks see only its own row, so a rule reads a column of it, such as a
client's own rating floor, from `portfolio.data.portfolios`.

To try a dataset that no system holds yet, or a what-if, give `run` a folder with `data=`: each dataset that has a file
there, such as `issuer_scores.csv`, is read from it, and the others from their sources. For every field of `Dataset`,
see the [reference](../reference.md).
