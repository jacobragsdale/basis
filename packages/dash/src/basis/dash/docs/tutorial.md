# Tutorial: your first page

In this tutorial, we will make a Dash app's repo, serve it on its synthetic data, and add a page of our own that shows
the book's holdings by rating, with its tests passing. It takes about twenty minutes. Every command runs from the
repo's root.

## 1. Make the repo

You need [uv](https://docs.astral.sh/uv/) 0.13 or later, git, and access to github.com. basis has no release yet, so a
Dash app's repo takes basis-dash from a checkout of basis:

```sh
git clone https://github.com/jacobragsdale/basis
uv run --project basis basis-dash new app desk --framework-path basis
cd desk
uv run basis check
```

`basis-dash new app NAME` makes a repo whose package, `desk/`, is a copy of basis-dash's example: the positions page
of a fixed income desk, on 320 synthetic bonds in `fixtures/holdings.csv`. `basis check` lints, type checks and tests
it, and checks the app's layers, its callbacks and every page on its fakes. It passes on a fresh repo; if it does not,
`uv run basis doctor` says what on this machine needs fixing.

## 2. Serve it

```sh
uv run basis dev
```

The log's first line names the address, `serving desk on 127.0.0.1:8000`. Open <http://127.0.0.1:8000> in a browser.
You should see the header with `LOCAL` beside the app's title, four tiles over two charts, and a grid of 320 holdings.
Pick a book in the filter bar: the tiles, the charts and the grid's totals change together, and the URL holds the
choice, so a link reproduces the view. Leave `basis dev` running; it serves each change as you save it.

## 3. See a page as an agent does

In a second terminal:

```sh
uv run basis render / --set book.value=Municipals
```

The output is JSON: each output of the page's callbacks, as a person sees it. Notice that the grid's first rows are
written as the grid writes them, `"$2,500,000"` and `"3.000%"`, and that the market value tile and the grid's total
are the same number.

## 4. Add a dataset

Open `desk/data.py`. Every dataset is declared once, by name: one query of the warehouse, `holdings`, and views of it,
each a pure function in `desk/domain.py`. Add a row and a view for the book by rating, at the end of `data.py`:

```python
class Rating(bd.Row):
    rating: str
    market_value: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(1)]


RATINGS = bd.view("ratings", domain.by_rating, Rating, positions=SHOWN)
```

and its function, at the end of `domain.py`:

```python
def by_rating(positions: pd.DataFrame) -> pd.DataFrame:
    """Each rating's market value and its weight."""
    ratings = positions.groupby("rating", as_index=False).agg(market_value=("market_value", "sum"))
    return pd.DataFrame({"rating": ratings["rating"], "market_value": ratings["market_value"], "weight": ratings["market_value"] / ratings["market_value"].sum()})
```

Check it loads:

```sh
uv run basis load ratings --param as_of=2026-11-12 --param book= --param search=
```

You should see five ratings, `A`, `AA`, `AAA`, `BB` and `BBB`, and two loads: the first a `miss`, the second a `hit`, since the
cache keeps the value.

## 5. Add the page

```sh
uv run basis new page ratings
```

This writes `desk/pages/ratings.py` and `desk/tests/test_ratings.py`, and adds the page to `app.py`. Open
`desk/pages/ratings.py`, and make it show the new dataset:

```python
import basis.dash as bd
from dash import Input, Output, callback

from desk.data import RATINGS, Rating

page = bd.page(
    "/ratings",
    "Ratings",
    filters=[bd.as_of("ratings-as-of", calendar="sifma-us")],
    body=[bd.card("By rating", bd.graph("ratings-chart"), span=6), bd.card("Ratings", bd.grid("ratings-grid", Rating, key="rating", height=None, search=False), span=6, flush=True)],
)


@callback(Output("ratings-chart", "figure"), Output("ratings-grid", "rowData"), Input("ratings-as-of", "value"))
async def show(as_of: str) -> tuple[object, bd.Rows]:
    ratings = await bd.load(RATINGS, as_of=bd.day(as_of), book="", search="")
    return bd.charts.bar(ratings, Rating, x="rating", y="market_value"), bd.rows(ratings)
```

The browser now shows a second link in the header, "Ratings". Open it: a chart and a grid of five ratings.

## 6. Test it

Make the test the generator wrote fire the new callback, in `desk/tests/test_ratings.py`:

```python
def test_the_ratings_page_shows_its_rows() -> None:
    with bd.testing.client(app) as client:
        answered = client.fire("..ratings-chart.figure...ratings-grid.rowData..", {"ratings-as-of.value": "2026-11-12"})
    assert [row["rating"] for row in answered["ratings-grid"]["rowData"]] == ["A", "AA", "AAA", "BB", "BBB"]
```

Then run the checks:

```sh
uv run basis check
```

It passes. Had the page's callback named an id the layout lacks, or loaded without `async`, `basis check` would have
ended with one `ERROR` line saying so, and how to fix it.

You have made a Dash app's repo, served it on its fixtures, and added a dataset and a page with their tests. The
[how-to guides](how-to.md) cover the next tasks, and the [reference](reference.md) every name.
