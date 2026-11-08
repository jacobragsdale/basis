"""The app compresses its JSON and Arrow above `app.COMPRESS_FROM` for a client that accepts gzip."""

import gzip
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from basis import api

ARROW = "application/vnd.apache.arrow.stream"


class Quote(BaseModel):
    security: str
    price: float


def quotes(count: int) -> list[Quote]:
    return [Quote(security=f"US91282{index:05d}", price=100.0 + index / 8) for index in range(count)]


def many() -> list[Quote]:
    return quotes(2_000)


def few() -> list[Quote]:
    return quotes(2)


SERVICE = api.Service(
    name="quotes",
    settings=api.Settings,
    routes=[api.get("/many", many, deadline_s=5, response=list[Quote], errors=[], dataset=True), api.get("/few", few, deadline_s=5, response=list[Quote], errors=[], dataset=True)],
)


def sent(path: str, **headers: str) -> tuple[int, dict[str, str], bytes]:
    with TestClient(api.build(SERVICE, settings=api.Settings.given()).asgi) as client, client.stream("GET", path, headers=headers) as response:
        return response.status_code, dict(response.headers), b"".join(response.iter_raw())


@pytest.mark.parametrize("accept", ["application/json", ARROW])
def test_a_large_answer_is_gzipped_for_a_client_that_accepts_it(accept: str) -> None:
    status, headers, body = sent("/many", accept=accept, **{"accept-encoding": "gzip, br"})
    plain = sent("/many", accept=accept, **{"accept-encoding": "identity"})[2]
    assert (status, headers["content-encoding"], headers["vary"], headers["content-type"].split(";")[0]) == (HTTPStatus.OK, "gzip", "Accept, Accept-Encoding", accept)
    assert gzip.decompress(body) == plain
    assert len(body) < len(plain) / 2


def test_a_small_answer_and_a_client_that_accepts_no_gzip_get_it_as_it_is() -> None:
    assert "content-encoding" not in sent("/few", **{"accept-encoding": "gzip"})[1]
    assert "content-encoding" not in sent("/many", **{"accept-encoding": "identity"})[1]
