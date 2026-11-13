# Tutorial: your first page

In this tutorial, we will make a Dash app's repo, serve it on its synthetic data, and add a page of our own that shows
the desk's market value by book, with its tests passing. It takes about twenty minutes. Every command runs from the
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
written as the grid writes them, `"$500,000"` and `"1.750%"`, and that the market value tile and the grid's total
are the same number.

## 4. Add a dataset

Open `desk/data.py`. Every dataset is declared once, by name: two queries of the warehouse, `holdings` and `curve`,
and views of them, each a pure function in `desk/domain.py`. Add a row and a view for the desk by book, at the end of
`data.py`:

```python
class Book(bd.Row):
    book: str
    market_value: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(1)]


BOOK_TOTALS = bd.view("books", domain.by_book, Book, positions=POSITIONS)
```

and its function, at the end of `domain.py`:

```python
def by_book(positions: pd.DataFrame) -> pd.DataFrame:
    """Each book's market value and its weight of the desk's."""
    books = positions.groupby("book", as_index=False).agg(market_value=("market_value", "sum"))
    return pd.DataFrame({"book": books["book"], "market_value": books["market_value"], "weight": books["market_value"] / books["market_value"].sum()})
```

Check it loads:

```sh
uv run basis load books --param as_of=2026-11-12
```

You should see four books, `Core Rates`, `Credit`, `Liquidity` and `Municipals`, and two loads: the first a `miss`,
the second a `hit`, since the cache keeps the value.

## 5. Add the page

```sh
uv run basis new page books
```

This writes `desk/pages/books.py` and `desk/tests/test_books.py`, and adds the page to `app.py`. Open
`desk/pages/books.py`, and make it show the new dataset:

```python
import basis.dash as bd
from dash import Input, Output, callback

from desk.data import BOOK_TOTALS, Book

page = bd.page(
    "/books",
    "Books",
    filters=[bd.as_of("books-as-of", calendar="sifma-us")],
    body=[bd.card("By book", bd.graph("books-chart"), span=6), bd.card("Books", bd.grid("books-grid", Book, key="book", height=None, search=False), span=6, flush=True)],
)


@callback(Output("books-chart", "figure"), Output("books-grid", "rowData"), Input("books-as-of", "value"))
async def show(as_of: str) -> tuple[object, bd.Rows]:
    books = await bd.load(BOOK_TOTALS, as_of=bd.day(as_of))
    return bd.charts.bar(books, Book, x="book", y="market_value"), bd.rows(books)
```

The browser now shows a second link in the header, "Books". Open it: a chart and a grid of four books.

## 6. Test it

Make the test the generator wrote fire the new callback, in `desk/tests/test_books.py`:

```python
def test_the_books_page_shows_its_rows() -> None:
    with bd.testing.client(app) as client:
        answered = client.fire("..books-chart.figure...books-grid.rowData..", {"books-as-of.value": "2026-11-12"})
    assert [row["book"] for row in answered["books-grid"]["rowData"]] == ["Core Rates", "Credit", "Liquidity", "Municipals"]
```

Then run the checks:

```sh
uv run basis check
```

It passes. Had the page's callback named an id the layout lacks, or loaded without `async`, `basis check` would have
ended with one `ERROR` line saying so, and how to fix it.

You have made a Dash app's repo, served it on its fixtures, and added a dataset and a page with their tests. The
[how-to guides](how-to.md) cover the next tasks, and the [reference](reference.md) every name.
