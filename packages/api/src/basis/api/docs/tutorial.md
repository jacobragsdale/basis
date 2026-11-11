# Tutorial: your first route

This walks through an API repo from nothing to a new route that a UI can call, on fakes, with its tests passing. It
takes about twenty minutes. Every command runs from the repo's root.

## 1. Make the repo

You need [uv](https://docs.astral.sh/uv/) 0.13 or later, git, and access to github.com. basis has no release yet, so
an API repo takes basis-api from a checkout of basis:

```sh
git clone https://github.com/jacobragsdale/basis
uv run --project basis basis-api new app ledger --framework-path basis
cd ledger
uv run basis check
```

`basis-api new app NAME` makes a repo whose package, `ledger/`, is a copy of basis-api's example: an accruals service
for fixed-rate bonds, with a job beside it in `jobs/`. `basis check` lints, type checks and tests it, and checks the
layers and the agent files. It passes on a fresh repo; if it does not, `uv run basis doctor` says what on this machine
needs fixing.

## 2. Call a route on its fakes

```sh
uv run basis call conventions --fake
uv run basis call bond '{"path": {"security": "US912810TM64"}}' --fake
```

`basis call ROUTE` runs one route in this process and prints its response as JSON. A route is named by its handler,
so `conventions` is `handlers.conventions`. `--fake` runs it on the fakes the service declares, in `ledger/dev.py`:
synthetic bonds, and a pricing service that quotes two of them. Ask for a bond the fakes do not hold, and the answer
is a problem, with its status, its type and the request's id, and one `ERROR` line on stderr.

`uv run basis dev` serves the same app on `127.0.0.1` until Ctrl+C, for a UI to call.

## 3. Read the layers

Open `ledger/service.py`. Each route names its path, its handler, its deadline, its response and its errors. The
handler is in `handlers.py`, and reaches the warehouse only through what `api.given` gives it, which it passes on to a
function in `repository.py`, where all the SQL is. The rules are in `domain.py`, which imports nothing of basis, so a
job could run them. `resources.py` declares what the API reaches, and `errors.py` what each domain error means in HTTP.
`basis check` fails, with the fix, when an import goes the wrong way.

## 4. Add a route

Add a route that answers a bond's maturity. First the rule, in `domain.py`:

```python
class Maturity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    maturity: date
    days_left: int


def maturity(bond: Bond, today: date) -> Maturity:
    return Maturity(security_id=bond.security_id, maturity=bond.maturity, days_left=(bond.maturity - today).days)
```

A handler passes each resource on and never calls its methods, so the clock is read in `repository.py`, as its
`stamp` reads it:

```python
def today(clock: api.Clock) -> date:
    """The clock's date, in UTC."""
    return clock.now().date()
```

Then the handler, in `handlers.py`, which reads the bond and the date through the repository:

```python
async def bond_maturity(security: Security, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)], clock: Annotated[api.Clock, api.given(CLOCK)]) -> domain.Maturity:
    found = await repository.bond(warehouse, security)
    return domain.maturity(found, repository.today(clock))
```

Call `clock.now()` in the handler instead, and `basis check` fails, naming the line and the fix.

Then the route, added to the list of `routes` in `service.py`:

```python
api.get("/bonds/{security}/maturity", handlers.bond_maturity, deadline_s=5, response=Maturity, errors=[UNKNOWN_BOND])
```

importing `Maturity` from the domain beside the others. The route declares `UNKNOWN_BOND` because
`repository.bond` raises the domain's `UnknownBond` for a bond the warehouse does not have.

## 5. Test it, and write its document

Add a test to `ledger/tests/test_domain.py` for the rule, on plain values, and two to `ledger/tests/test_handlers.py`
for the route, with the fixtures already there, whose fake clock reads 2026-11-06:

```python
def test_a_bonds_maturity(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}/maturity")
    assert response.json() == {"security_id": ISIN, "maturity": "2034-11-15", "days_left": 2931}


def test_the_maturity_of_an_unknown_bond_is_a_404(client: TestClient) -> None:
    assert kind(client.get("/bonds/XS0000000000/maturity")) == (404, "unknown-bond")
```

Then format, write the document, and check:

```sh
uv run ruff check --fix . && uv run ruff format .
uv run basis sync
uv run basis check
uv run basis call bond_maturity '{"path": {"security": "US912810TM64"}}' --fake
```

`basis sync` writes the new route into `openapi.json`, the document UIs generate their clients from; `basis check`
fails until it matches the service. Commit what it wrote with the code: the commit hook runs `basis check` again.

## Where next

- `uv run basis docs api/how-to` for the tasks that come up next: a route that writes, a table for a grid, another
  service, a signed-in user.
- `uv run basis docs api/reference` for every declaration and verb.
- The skills in `.agents/skills/api-*` walk an agent through adding a route and finding out why a request failed.
