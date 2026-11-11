---
name: api-add-route
description: Add a route to a basis-api service end to end, from its domain rule to its tests and openapi.json. Use when the user wants a new endpoint, route or API call added.
---

# Add a route

1. Read `uv run basis docs api/reference` once: the layers, the route's declaration and the resources.
2. Domain, in `domain.py`: the response's model and the rule, plain pydantic and functions that import nothing of
   basis; an error the rule can meet is a plain exception there, as `class Matured(Exception)`. Every number is a
   `float`, or an `int` where it counts; every time is an `AwareDatetime`.
3. Repository, in `repository.py`: one function per query or call to another service, taking the resource
   (`api.Reader`, `api.Writer`, `api.Client`, `api.Clock`, `api.Ids`) and returning domain types. SQL binds every value.
   A failure that means something to the domain becomes its exception.
4. Errors, in `errors.py`: each new domain exception's meaning, as
   `MATURED = api.error(domain.Matured, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="...")`, and a subclass of
   `api.Error` for each refusal of the request itself.
5. Handler, in `handlers.py`: its parameters, each resource as `Annotated[api.Reader, api.given(WAREHOUSE)]`, passed on
   to the repository; never call a resource's methods here. `async def` where it awaits. A grid's row model, with its
   `api.format`, lives here too.
6. Service, in `service.py`: `api.get(PATH, handlers.NAME, deadline_s=..., response=..., errors=[...])`, every error
   the handler raises or meets listed. A route that writes asks for the user or declares `anonymous=True`; a GET that
   reads nothing a deployment lacks gives `smoke=`.
7. Fakes, in the module the service's `fakes=` names: an answer for each new SQL statement or path, so
   `uv run basis call NAME '{"path": {...}}' --fake` works.
8. Tests: the rule in `tests/test_domain.py` on plain values; the route in `tests/test_handlers.py` on fakes, each
   declared error among them, a resource's failure made with its fake's `fails`; the repository's SQL in
   `tests/test_repository.py`, marked `integration`.
9. Run `uv run basis sync`, which writes `openapi.json`, then `uv run basis check`, and fix each `ERROR` line as it
   says. Commit `openapi.json` with the code. A change that removes or retypes what a UI reads fails
   `basis check --full`: add a field or route beside it instead.
