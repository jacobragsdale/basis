"""The breaking-change check: an API's `openapi.json` against its last release's, found by the tags its repo declares,
which fails on a change that breaks a UI built against that release."""

import json
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
from basis.core import command
from basis.core.sync import MARKER

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
    lots: float


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
    return api.post("/orders", _place(body, name), deadline_s=1, response=Bond, errors=[], status=status, anonymous=True)  # ty: ignore[invalid-argument-type] -- a handler made for the test


def get_rows(*, dataset: bool) -> api.Route:
    return api.get("/rows/{security}", rows, deadline_s=1, response=list[Row], errors=[], dataset=dataset)


BEFORE = (get_bond(), post_order(), get_rows(dataset=True))


@pytest.mark.parametrize(
    ("after", "breaking", "compatible"),
    [
        (BEFORE, [], []),
        ((get_bond(BondWithIssuer), *BEFORE[1:]), [], ["GET /bonds/{security} (bond): the response.issuer is added"]),
        ((get_bond(BondWithoutCoupon), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon is removed, or renamed"], []),
        ((get_bond(BondCouponAsNumber), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon may be null or number"], []),
        ((get_bond(BondMayHaveNoCoupon), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.coupon may be null"], []),
        ((get_bond(BondMonthly), *BEFORE[1:]), ["GET /bonds/{security} (bond): the response.frequency may be 12"], []),
        ((BEFORE[0], post_order(OrderWithoutNote), BEFORE[2]), ["POST /orders (place) no longer takes body.note"], []),
        ((BEFORE[0], post_order(OrderWithDesk), BEFORE[2]), ["POST /orders (place) requires body.desk, which it did not take"], []),
        ((BEFORE[0], post_order(OrderWithTag), BEFORE[2]), [], ["POST /orders (place) takes body.tag"]),
        ((BEFORE[0], post_order(OrderNeedingNote), BEFORE[2]), ["POST /orders (place) no longer takes body.note as null", "POST /orders (place) requires body.note"], []),
        ((BEFORE[0], post_order(OrderBuyingOnly), BEFORE[2]), ['POST /orders (place) no longer takes body.side as "sell"'], []),
        ((BEFORE[0], post_order(OrderOfAnySide), BEFORE[2]), [], ['POST /orders (place) takes body.side as "short" too']),
        ((BEFORE[0], post_order(None, name="place"), BEFORE[2]), ["POST /orders (place) takes no body"], ["POST /orders (place) no longer answers body-too-large"]),
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
def released(tmp_path: Path) -> Path:
    """An API repo released as v0.1.0: its document, and the released one committed beside it."""
    repo = tmp_path / "api"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[tool.basis]\nparts = ["api"]\n', encoding="utf-8")
    write(repo, *BEFORE)
    contract.release(repo, "v0.1.0")
    return repo


def write(repo: Path, *routes: api.Route) -> None:
    (repo / "openapi.json").write_text(openapi.text(api.Service(name="bonds", settings=api.Settings, routes=routes)), encoding="utf-8")


def test_an_unchanged_document_is_none(released: Path) -> None:
    assert contract.contract(released) is None


def test_a_change_no_ui_relies_on_is_summed_up_and_listed(released: Path) -> None:
    write(released, get_bond(BondWithIssuer), *BEFORE[1:])
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


def test_the_released_document_is_the_repos_own_which_sync_leaves_alone(released: Path) -> None:
    text = (released / "openapi.released.json").read_text(encoding="utf-8")
    assert text.splitlines()[:2] == ["{", '  "x-basis-release": "v0.1.0",']
    assert MARKER not in text
    assert json.loads(text) == {"x-basis-release": "v0.1.0", **openapi.document(api.Service(name="bonds", settings=api.Settings, routes=BEFORE))}


def test_the_check_needs_no_history_and_no_tags(released: Path, git: Git, tmp_path: Path) -> None:
    """A shallow clone that fetched no tags, as a pipeline's default checkout may be, checks as any other."""
    git(released, "init", "--quiet", "--initial-branch=main")
    git(released, "config", "user.email", "ci@example.com")
    git(released, "config", "user.name", "ci")
    git(released, "add", "--all")
    git(released, "commit", "--quiet", "--message", "The first release")
    write(released, get_bond(BondWithoutCoupon), *BEFORE[1:])
    git(released, "commit", "--quiet", "--all", "--message", "No coupon")
    git(tmp_path, "clone", "--quiet", "--depth", "1", "--no-tags", released.as_uri(), "partial")
    with pytest.raises(contract.ContractError, match=re.escape("openapi.json breaks its contract with release v0.1.0's: GET /bonds/{security} (bond): the response.coupon is removed")):
        contract.contract(tmp_path / "partial")


def test_with_no_released_document_there_is_nothing_to_break(released: Path) -> None:
    (released / "openapi.released.json").unlink()
    assert contract.contract(released) == "No earlier release to compare with: no openapi.released.json is committed, which `basis release-document VERSION` writes at a release."


def test_the_api_part_gives_basis_its_contract_and_the_verb_that_records_a_release(released: Path, capsys: pytest.CaptureFixture[str], logging_restored: None) -> None:
    del logging_restored
    given = cli.part().contract
    assert given is not None
    assert given(released) is None
    write(released, get_bond(BondWithIssuer), *BEFORE[1:])
    assert command.run(["release-document", "v0.2.0"], cwd=released, installed=command.installed()) == 0
    assert capsys.readouterr().out == "openapi.released.json: release v0.2.0's document; commit it with the release\n"
    assert given(released) is None


def test_a_missing_document_says_to_sync(released: Path) -> None:
    (released / "openapi.json").unlink()
    with pytest.raises(contract.ContractError, match=r"openapi.json is missing, so its contract cannot be checked: run `uv run basis sync`"):
        contract.contract(released)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('[tool.basis.api]\nreleases = "v*"\n', "declares releases, the glob of release tags basis-api reads no longer: remove it"),
        ("[tool.basis.api]\nrelases = 1\n", "has relases, which basis-api does not read: remove it"),
    ],
    ids=["the-old-glob", "a-misspelled-key"],
)
def test_the_api_table_holds_only_what_basis_api_reads(tmp_path: Path, table: str, message: str) -> None:
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
    return api.post("/take", take, deadline_s=1, response=Bond, errors=[], anonymous=True)


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
        (_giving(Sized), _giving(SizedAsNumber), ["GET /give (give): the response.size may be null or number"], []),  # NaN answers null
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


def _raw(body: JsonValue = None, answer: JsonValue = None, **schemas: JsonValue) -> dict[str, JsonValue]:
    """A document of one operation, POST /take, that takes `body` and answers `answer`, with `schemas` as its
    components."""
    operation: dict[str, JsonValue] = {"operationId": "take", "responses": {"200": {"content": {"application/json": {"schema": answer or {"type": "string"}}}}}}
    if body is not None:
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return {"openapi": "3.1.0", "paths": {"/take": {"post": operation}}, "components": {"schemas": dict(schemas)}}


def _object(**properties: JsonValue) -> dict[str, JsonValue]:
    return {"type": "object", "properties": dict(properties), "required": list(properties), "additionalProperties": False}


UNION: JsonValue = {"anyOf": [{"$ref": "#/components/schemas/Swap"}, {"$ref": "#/components/schemas/Bond"}]}
TEXT: JsonValue = {"type": "string"}
NUMBER: JsonValue = {"type": "number"}
INTEGER: JsonValue = {"type": "integer"}


@pytest.mark.parametrize(
    ("before", "after", "breaking"),
    [
        (
            _raw(UNION, Swap=_object(rate=NUMBER), Bond=_object(coupon=NUMBER)),
            _raw(UNION, Swap=_object(rate=NUMBER, notional=NUMBER), Bond=_object(coupon=NUMBER)),
            ["POST /take (take) requires body<Swap>.notional, which it did not take"],
        ),
        (
            _raw(answer=UNION, Swap=_object(size=INTEGER), Bond=_object(size=TEXT)),
            _raw(answer=UNION, Swap=_object(size=TEXT), Bond=_object(size=TEXT)),
            ["POST /take (take): the response<Swap>.size may be string"],
        ),
        (
            _raw(answer={"$ref": "#/components/schemas/Swap"}, Swap=_object(rate=NUMBER)),
            _raw(answer=UNION, Swap=_object(rate=NUMBER), Bond=_object(coupon=NUMBER)),
            ["POST /take (take): the response may be Bond"],
        ),
        (_raw(_object(side=TEXT)), _raw(_object(side={"type": "string", "enum": ["buy", "sell"]})), ['POST /take (take) no longer takes body.side as any value but "buy" or "sell"']),
        (
            _raw(_object(pair={"type": "array", "prefixItems": [INTEGER, TEXT], "minItems": 2, "maxItems": 2})),
            _raw(_object(pair={"type": "array", "prefixItems": [INTEGER, INTEGER], "minItems": 2, "maxItems": 2})),
            ["POST /take (take) no longer takes body.pair[1] as string"],
        ),
    ],
    ids=["a-required-field-added-to-one-arm-of-a-request", "a-field-retyped-in-one-arm-of-a-response", "an-arm-added-to-a-response", "text-narrowed-to-a-literal", "a-tuples-item-retyped"],
)
def test_each_arm_of_a_union_and_each_item_of_a_tuple_is_compared_by_itself(before: dict[str, JsonValue], after: dict[str, JsonValue], breaking: list[str]) -> None:
    assert contract.compare(before, after).breaking == breaking


def test_a_keyword_the_check_does_not_compare_is_said() -> None:
    found = contract.compare(_raw(_object(note={"allOf": [TEXT]})), _raw(_object(note={"allOf": [TEXT], "not": {"const": ""}})))
    assert found.skipped == ["POST /take (take): body.note's allOf", "POST /take (take): body.note's not"]


def _erring(*errors: JsonValue) -> dict[str, JsonValue]:
    document = _raw(_object(side=TEXT))
    operation = openapi.mapping(openapi.mapping(openapi.mapping(document["paths"])["/take"])["post"])
    operation["x-basis-errors"] = list(errors)
    return document


GONE: dict[str, JsonValue] = {"status": 410, "type": "gone", "title": "The order is gone"}


@pytest.mark.parametrize(
    ("before", "after", "breaking", "compatible"),
    [
        (_erring(GONE), _erring(GONE | {"status": 404}), ["POST /take (take) answers gone with 404, not 410"], []),
        (_erring(), _erring(GONE), [], ["POST /take (take) may answer gone, 410"]),
        (_erring(GONE), _erring(), [], ["POST /take (take) no longer answers gone"]),
        (_erring(GONE), _erring(GONE | {"title": "The order has gone"}), [], ["POST /take (take) titles gone 'The order has gone', not 'The order is gone'"]),
    ],
    ids=["a-type-that-answers-another-status", "a-type-added", "a-type-removed", "a-title-changed"],
)
def test_an_operations_errors_are_compared_by_type(before: dict[str, JsonValue], after: dict[str, JsonValue], breaking: list[str], compatible: list[str]) -> None:
    found = contract.compare(before, after)
    assert (found.breaking, found.compatible) == (breaking, compatible)


def test_a_display_format_that_changes_is_listed() -> None:
    money: dict[str, JsonValue] = {"type": "number", "x-basis-format": {"kind": "money", "currency": "USD"}}
    found = contract.compare(_raw(answer=_object(notional=money)), _raw(answer=_object(notional=money | {"x-basis-format": {"kind": "pct", "decimals": 2}})))
    assert (found.breaking, found.compatible) == ([], ['POST /take (take): the response.notional is shown as {"decimals":2,"kind":"pct"}, not {"currency":"USD","kind":"money"}'])
