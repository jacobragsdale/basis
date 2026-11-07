"""The breaking-change check: an API's `openapi.json` against its last release's, found by the tags its repo declares,
which fails on a change that breaks a UI built against that release."""

import re
from collections.abc import Callable, Sequence
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from basis import api
from basis.api import cli, contract, openapi

type Git = Callable[..., str]
"""conftest's `git` fixture."""


class Bond(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security: str
    coupon: str
    maturity: date
    frequency: Literal[1, 2]


class BondWithIssuer(Bond):
    issuer: str


class BondWithoutCoupon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security: str
    maturity: date
    frequency: Literal[1, 2]


class BondCouponAsNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security: str
    coupon: float
    maturity: date
    frequency: Literal[1, 2]


class BondMayHaveNoCoupon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security: str
    coupon: str | None
    maturity: date
    frequency: Literal[1, 2]


class BondMonthly(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security: str
    coupon: str
    maturity: date
    frequency: Literal[1, 2, 12]


class Order(BaseModel):
    security: str
    side: Literal["buy", "sell"]
    note: str | None = None


class OrderWithDesk(Order):
    desk: str


class OrderWithTag(Order):
    tag: str | None = None


class OrderWithoutNote(BaseModel):
    security: str
    side: Literal["buy", "sell"]


class OrderNeedingNote(BaseModel):
    security: str
    side: Literal["buy", "sell"]
    note: str


class OrderBuyingOnly(BaseModel):
    security: str
    side: Literal["buy"]
    note: str | None = None


class OrderOfAnySide(BaseModel):
    security: str
    side: Literal["buy", "sell", "short"]
    note: str | None = None


class Row(BaseModel):
    book: str
    lots: int


def _bond(response: type[BaseModel]) -> Callable[[str], object]:
    def bond(security: str) -> object:
        del security
        raise NotImplementedError

    bond.__annotations__["return"] = response
    return bond


def _place(body: type[BaseModel] | None, name: str = "place") -> Callable[..., object]:
    def place(order: Order) -> Bond:
        del order
        raise NotImplementedError

    def place_nothing() -> Bond:
        raise NotImplementedError

    found = place if body is not None else place_nothing
    if body is not None:
        found.__annotations__["order"] = body
    found.__name__ = found.__qualname__ = name
    return found


def bond_by_isin(isin: str) -> Bond:
    del isin
    raise NotImplementedError


bond_by_isin.__name__ = bond_by_isin.__qualname__ = "bond"  # the same operation, its path's parameter named anew


def rows(security: str, desk: str | None = None) -> list[Row]:
    del security, desk
    return []


def document(*routes: api.Route) -> dict[str, JsonValue]:
    return openapi.document(api.Service(name="bonds", settings=api.Settings, routes=routes))


def get_bond(response: type[BaseModel] = Bond, path: str = "/bonds/{security}") -> api.Route:
    return api.get(path, _bond(response), deadline_s=1, response=response, errors=[])  # ty: ignore[invalid-argument-type] -- a handler made for the test


def post_order(body: type[BaseModel] | None = Order, *, name: str = "place", status: int = HTTPStatus.OK) -> api.Route:
    return api.post("/orders", _place(body, name), deadline_s=1, response=Bond, errors=[], status=status)  # ty: ignore[invalid-argument-type] -- a handler made for the test


def get_rows(*, dataset: bool) -> api.Route:
    return api.get("/rows/{security}", rows, deadline_s=1, response=list[Row], errors=[], dataset=dataset)


BEFORE = (get_bond(), post_order(), get_rows(dataset=True))


@pytest.mark.parametrize(
    ("after", "breaking", "compatible"),
    [
        (BEFORE, [], []),
        ((get_bond(BondWithIssuer), *BEFORE[1:]), [], ["GET /bonds/{security} (bond): the response.issuer is added"]),
        ((get_bond(BondWithoutCoupon), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon is removed, or renamed"], []),
        ((get_bond(BondCouponAsNumber), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon may be number"], []),
        ((get_bond(BondMayHaveNoCoupon), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon may be null"], []),
        ((get_bond(BondMonthly), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.frequency may be 12"], []),
        ((BEFORE[0], post_order(OrderWithoutNote), BEFORE[2]), ["POST /orders (place) no longer takes body.note"], []),
        ((BEFORE[0], post_order(OrderWithDesk), BEFORE[2]), ["POST /orders (place) requires body.desk, which it did not take"], []),
        ((BEFORE[0], post_order(OrderWithTag), BEFORE[2]), [], ["POST /orders (place) takes body.tag"]),
        ((BEFORE[0], post_order(OrderNeedingNote), BEFORE[2]), ["POST /orders (place) no longer takes body.note as null", "POST /orders (place) requires body.note"], []),
        ((BEFORE[0], post_order(OrderBuyingOnly), BEFORE[2]), ['POST /orders (place) no longer takes body.side as "sell"'], []),
        ((BEFORE[0], post_order(OrderOfAnySide), BEFORE[2]), [], ['POST /orders (place) takes body.side as "short" too']),
        ((BEFORE[0], post_order(None, name="place"), BEFORE[2]), ["POST /orders (place) takes no body"], []),
        ((BEFORE[0], post_order(name="submit"), BEFORE[2]), ["POST /orders (submit) has the operation id submit, renaming its client's function"], []),
        ((BEFORE[0], post_order(status=HTTPStatus.CREATED), BEFORE[2]), ["POST /orders (place) succeeds with 201, not 200"], []),
        ((*BEFORE[:2], get_rows(dataset=False)), ["GET /rows/{security} (rows) no longer answers Arrow"], []),
        (BEFORE[:2], ["GET /rows/{security} (rows) is removed, or has moved"], []),
        ((get_bond(path="/bond/{security}"), *BEFORE[1:]), ["GET /bonds/{security} (bond) is removed, or has moved"], ["GET /bond/{security} (bond) is added"]),
        ((api.get("/bonds/{isin}", bond_by_isin, deadline_s=1, response=Bond, errors=[]), *BEFORE[1:]), [], []),
    ],
    ids=[
        "unchanged", "a-response-field-added", "a-response-field-removed", "a-response-field-retyped", "a-response-field-nullable", "a-response-enum-grows",
        "a-request-field-removed", "a-required-request-field-added", "an-optional-request-field-added", "a-request-field-required", "a-request-enum-shrinks",
        "a-request-enum-grows", "a-body-dropped", "an-operation-renamed", "a-new-success-status", "no-longer-arrow", "an-operation-removed", "an-operation-moved",
        "a-path-parameter-renamed",
    ],
)  # fmt: skip
def test_what_breaks_a_ui_and_what_does_not(after: Sequence[api.Route], breaking: list[str], compatible: list[str]) -> None:
    found = contract.compare(document(*BEFORE), document(*after))
    assert (found.breaking, found.compatible) == (breaking, compatible)


def test_a_query_parameter_renamed_or_required_breaks_a_ui() -> None:
    def rows_by_book(security: str, book: str | None = None) -> list[Row]:
        del security, book
        return []

    def rows_for_a_desk(security: str, desk: str) -> list[Row]:
        del security, desk
        return []

    renamed = api.get("/rows/{security}", rows_by_book, deadline_s=1, response=list[Row], errors=[], dataset=True)
    required = api.get("/rows/{security}", rows_for_a_desk, deadline_s=1, response=list[Row], errors=[], dataset=True)
    assert contract.compare(document(*BEFORE), document(*BEFORE[:2], renamed)).breaking == [
        "GET /rows/{security} (rows_by_book) has the operation id rows_by_book, renaming its client's function",
        "GET /rows/{security} (rows_by_book) no longer takes the query parameter desk",
    ]
    assert "GET /rows/{security} (rows_for_a_desk) requires the query parameter desk" in contract.compare(document(*BEFORE), document(*BEFORE[:2], required)).breaking


@pytest.fixture
def released(tmp_path: Path, git: Git) -> Path:
    """A clone whose API was released as v0.1.0, with its document at the repo's root, in a folder of the clone."""
    repo = tmp_path / "clone" / "api"
    repo.mkdir(parents=True)
    git(tmp_path / "clone", "init", "--quiet", "--initial-branch=main")
    git(tmp_path / "clone", "config", "user.email", "ci@example.com")
    git(tmp_path / "clone", "config", "user.name", "ci")
    (repo / "pyproject.toml").write_text('[tool.basis]\nparts = ["api"]\n\n[tool.basis.api]\nreleases = "v*"\n', encoding="utf-8")
    write(repo, *BEFORE)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", "The first release")
    git(repo, "tag", "v0.1.0")
    return repo


def write(repo: Path, *routes: api.Route) -> None:
    (repo / "openapi.json").write_text(openapi.text(api.Service(name="bonds", settings=api.Settings, routes=routes)), encoding="utf-8")


def commit(repo: Path, git: Git, message: str) -> None:
    git(repo, "commit", "--quiet", "--all", "--message", message)


def test_an_unchanged_document_is_none(released: Path) -> None:
    assert contract.contract(released) is None


def test_a_change_no_ui_relies_on_is_summed_up_and_listed(released: Path, git: Git) -> None:
    write(released, get_bond(BondWithIssuer), *BEFORE[1:])
    commit(released, git, "An issuer")
    assert contract.contract(released) == ("Changed since release `v0.1.0`, with nothing a UI built against it relies on broken.\n\n- GET /bonds/{security} (bond): the response.issuer is added")


def test_a_change_that_breaks_a_ui_fails_and_names_each_break(released: Path) -> None:
    write(released, get_bond(BondWithoutCoupon), post_order(OrderWithDesk))
    with pytest.raises(contract.ContractError) as raised:
        contract.contract(released)
    assert str(raised.value) == (
        "openapi.json breaks its contract with release v0.1.0's: GET /rows/{security} (rows) is removed, or has moved;"
        " GET /bonds/{security} (bond): the response.coupon is removed, or renamed; POST /orders (place) requires body.desk, which it did not take:"
        " keep each field a UI reads or sends as it was, adding new ones beside it, or add the changed operation beside the old one, under a path of its own"
    )


def test_the_baseline_is_the_nearest_release_among_the_commits_ancestors(released: Path, git: Git) -> None:
    write(released, get_bond(BondWithoutCoupon), *BEFORE[1:])
    commit(released, git, "No coupon")
    git(released, "tag", "v0.2.0")
    git(released, "tag", "release-candidate")  # matches no glob the repo declares
    assert contract.contract(released) is None
    git(released, "checkout", "--quiet", "v0.1.0")
    assert contract.contract(released) is None


def test_a_clone_of_the_repo_with_its_tags_finds_its_release(released: Path, git: Git, tmp_path: Path) -> None:
    git(tmp_path, "clone", "--quiet", str(released.parent), "whole")
    write(tmp_path / "whole" / "api", get_bond(BondWithoutCoupon), *BEFORE[1:])
    with pytest.raises(contract.ContractError, match=re.escape("breaks its contract with release v0.1.0's")):
        contract.contract(tmp_path / "whole" / "api")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--depth", "1"), "is shallow, so the release it descends from may be missing from it, and its contract cannot be checked: fetch its whole history and its tags"),
        (("--no-tags",), "fetches no tags from origin, so the release it descends from may be missing from it, and its contract cannot be checked:"),
    ],
    ids=["shallow", "no-tags"],
)
def test_a_clone_that_may_lack_its_release_fails_rather_than_finding_none(released: Path, git: Git, tmp_path: Path, argv: tuple[str, ...], message: str) -> None:
    write(released, get_bond(BondWithIssuer), *BEFORE[1:])
    commit(released, git, "An issuer, after the release")  # so that one commit deep is short of it
    git(tmp_path, "clone", "--quiet", *argv, released.parent.as_uri(), "partial")
    with pytest.raises(contract.ContractError, match=re.escape(message)):
        contract.contract(tmp_path / "partial" / "api")


def test_with_no_earlier_release_there_is_nothing_to_break(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "pyproject.toml").write_text('[tool.basis.api]\nreleases = "api-v*"\n', encoding="utf-8")
    write(tmp_path, *BEFORE)
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "--quiet", "--message", "First")
    git(tmp_path, "tag", "v0.1.0")
    assert contract.contract(tmp_path) == "No earlier release to compare with: no tag matches `api-v*` among this commit's ancestors."


def test_a_release_before_the_document_has_none_to_break(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "pyproject.toml").write_text('[tool.basis.api]\nreleases = "v*"\n', encoding="utf-8")
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "--quiet", "--message", "Before the API")
    git(tmp_path, "tag", "v0.1.0")
    write(tmp_path, *BEFORE)
    assert contract.contract(tmp_path) == "No earlier document to compare with: release `v0.1.0` has no `openapi.json`."


def test_the_api_part_gives_basis_its_contract(released: Path) -> None:
    given = cli.part().contract
    assert given is not None
    assert given(released) is None


def test_a_missing_document_says_to_sync(released: Path) -> None:
    (released / "openapi.json").unlink()
    with pytest.raises(contract.ContractError, match=r"openapi.json is missing, so its contract cannot be checked: run `uv run basis sync`"):
        contract.contract(released)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("", 'declares no releases: add releases = "v*", the glob of the tags that mark its releases'),
        ('[tool.basis.api]\nreleases = "v*"\nrelases = "v*"\n', "has relases, which basis-api does not read: remove it"),
        ("[tool.basis.api]\nreleases = 1\n", "releases is not a glob: give one string"),
    ],
    ids=["none", "a-misspelled-key", "not-a-glob"],
)
def test_the_baseline_is_declared_never_guessed(tmp_path: Path, table: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[tool.basis]\nparts = ["api"]\n\n{table}', encoding="utf-8")
    with pytest.raises(contract.ContractError, match=re.escape(message)):
        contract.declared(tmp_path)


def test_a_document_that_is_not_json_says_to_sync(released: Path) -> None:
    (released / "openapi.json").write_text("{", encoding="utf-8")
    with pytest.raises(contract.ContractError, match=r"is not JSON .*: run `uv run basis sync`"):
        contract.contract(released)


class Sized(BaseModel):
    size: int


class SizedAsNumber(BaseModel):
    size: float


class Dated(BaseModel):
    on: date


class DatedAsText(BaseModel):
    on: str


class Noted(BaseModel):
    note: str


class MaybeNoted(BaseModel):
    note: str | None = None


def _taking(body: type[BaseModel]) -> api.Route:
    def take(order: Order) -> Bond:
        del order
        raise NotImplementedError

    take.__annotations__["order"] = body
    return api.post("/take", take, deadline_s=1, response=Bond, errors=[])


def _giving(response: type[BaseModel]) -> api.Route:
    def give() -> object:
        raise NotImplementedError

    give.__annotations__["return"] = response
    return api.get("/give", give, deadline_s=1, response=response, errors=[])


@pytest.mark.parametrize(
    ("before", "after", "breaking", "compatible"),
    [
        (_taking(Sized), _taking(SizedAsNumber), [], ["POST /take (take) takes body.size as number too"]),
        (_taking(SizedAsNumber), _taking(Sized), ["POST /take (take) no longer takes body.size as number"], []),
        (_taking(Dated), _taking(DatedAsText), [], ["POST /take (take) takes body.on as string too"]),
        (_taking(Noted), _taking(MaybeNoted), [], ["POST /take (take) takes body.note as null too", "POST /take (take) no longer requires body.note"]),
        (_giving(Sized), _giving(SizedAsNumber), ["GET /give (give): the response.size may be number"], []),
        (_giving(SizedAsNumber), _giving(Sized), [], []),
        (_giving(DatedAsText), _giving(Dated), [], []),
    ],
    ids=[
        "a-request-integer-taken-as-a-number",
        "a-request-number-narrowed-to-an-integer",
        "a-request-date-taken-as-text",
        "a-request-field-no-longer-required",
        "a-response-integer-given-as-a-number",
        "a-response-number-narrowed-to-an-integer",
        "a-response-text-narrowed-to-a-date",
    ],
)
def test_a_request_breaks_a_ui_only_where_it_refuses_what_it_took_and_a_response_where_it_gives_more(before: api.Route, after: api.Route, breaking: list[str], compatible: list[str]) -> None:
    found = contract.compare(document(before), document(after))
    assert (found.breaking, found.compatible) == (breaking, compatible)


def test_no_earlier_release_is_known_whatever_language_git_speaks(tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "pyproject.toml").write_text('[tool.basis.api]\nreleases = "v*"\n', encoding="utf-8")
    write(tmp_path, *BEFORE)
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "--quiet", "--message", "First")
    monkeypatch.setenv("LANGUAGE", "de")  # a git with its translations then says "Keine Namen gefunden"
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert contract.contract(tmp_path) == "No earlier release to compare with: no tag matches `v*` among this commit's ancestors."
