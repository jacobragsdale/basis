# Datasets

One module per dataset this model reads, besides `optim`'s shared holdings and securities: its row model, a pydantic
model whose fields are the columns, and its `Dataset`, which says where it comes from. A database's query is a file in
`sql/`. Every query takes only the business date, `%(as_of_date)s`, and a dataset read per portfolio, whose rows have a
`portfolio_id`, also takes the batch's ids, `%(portfolios)s`.

To add one:

1. Copy the module nearest to it, such as `controls.py`, and its SQL, and change the row model, the query and the key.
2. Add it to `Data` in `model.py`: `limits = LIMITS`.
3. Add a CSV of a few rows to `tests/fixture/`, named as the dataset, with a header row of its fields.
4. Read it in `prep.py` or a rule: `portfolio.data.limits`.

`uv run optim docs how-to/add-a-dataset` says more.
