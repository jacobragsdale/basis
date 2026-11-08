"""A built UI the app serves at `/`, beside its routes under `/api` (`Service.ui`), so one container serves a UI and its
API on one origin (design.md, "One origin").

The folder is found from the folder the app runs in, as it is when the request comes, so a UI built after the app
started is served. A file the folder holds is served as it is; any other path that names no file, as a client-side
route such as `/positions/7` does, is the UI's `index.html`, which routes it in the browser. A path under `/api` that no
route matches is the API's 404, never the UI. A folder not built yet answers 503, saying how to build it, and the
app's routes go on answering.

A file is served compressed where the build wrote its compressed copy beside it, `FILE.br` for a request that accepts
Brotli and `FILE.gz` for one that accepts gzip, with its own type, `Content-Encoding` and `Vary: Accept-Encoding`, and
as it is otherwise; a browser asks for Brotli only over HTTPS. A file under `assets/`, whose name the build hashes, is
cached for a year as immutable; every other file, `index.html` among them, is `no-cache`, so a reload asks again and
gets a 304 from its `ETag` or `Last-Modified` while it is unchanged. Responses the app compresses itself, its JSON and
Arrow among them, are `app.py`'s."""

import mimetypes
import os
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import override

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

from basis.api import errors
from basis.api.routes import API

INDEX = "index.html"
ASSETS = "assets"
"""The folder of the files whose names the build hashes, which never change under one name."""
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
ENCODINGS = (("br", ".br"), ("gzip", ".gz"))
"""Each compressed copy a build writes beside a file, in the order a request that accepts both is given them."""


class Folder(StaticFiles):
    """The UI's folder, `folder`, relative to the folder the app runs in."""

    def __init__(self, folder: str) -> None:
        super().__init__(directory=folder, html=True, check_dir=False)
        self.folder = folder

    @override
    async def check_config(self) -> None:
        """Nothing: a folder not built yet is the 503 `get_response` answers, not a defect."""

    @override
    async def get_response(self, path: str, scope: Scope) -> Response:
        if f"/{path}".startswith(f"{API}/") or f"/{path}" == API:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND)  # a route of the API's it does not have, which is no page of the UI's
        if not (Path(self.folder) / INDEX).is_file():
            msg = f"the UI's folder {self.folder} holds no {INDEX}: build the UI into it, as its build does, and reload the page"
            raise errors.UiNotBuiltError(msg)
        try:
            return await super().get_response(path, scope)
        except HTTPException as missing:
            if missing.status_code != HTTPStatus.NOT_FOUND or "." in PurePosixPath(path).name:  # a file the folder lacks stays a 404
                raise
            return await super().get_response(INDEX, scope)

    @override
    def file_response(self, full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope, status_code: int = HTTPStatus.OK) -> Response:
        """The file at `full_path`, or its compressed copy where the request accepts it, with the caching its place in
        the folder gives it, or a 304 where the request's validators match."""
        path = Path(full_path)
        request = Headers(scope=scope)
        accepted = _accepted(request.get("accept-encoding", ""))
        served, encoding = path, None
        for name, suffix in ENCODINGS:
            copy = path.with_name(path.name + suffix)
            if name in accepted and copy.is_file():
                served, encoding = copy, name
                break
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = FileResponse(served, status_code=status_code, stat_result=stat_result if encoding is None else served.stat(), media_type=media_type)
        if encoding is not None:
            response.headers["content-encoding"] = encoding
        response.headers["vary"] = "Accept-Encoding"
        relative = path.resolve().relative_to(Path(self.folder).resolve()).as_posix()
        response.headers["cache-control"] = IMMUTABLE if relative.startswith(f"{ASSETS}/") else REVALIDATE
        if self.is_not_modified(response.headers, request):
            return NotModifiedResponse(response.headers)
        return response


def _accepted(header: str) -> set[str]:
    """The encodings an `Accept-Encoding` header accepts: each it names but with `q=0`."""
    found: set[str] = set()
    for part in header.split(","):
        name, _, parameters = part.partition(";")
        quality = parameters.strip().removeprefix("q=").strip() if parameters.strip().startswith("q=") else "1"
        try:
            accepted = float(quality) > 0
        except ValueError:
            accepted = False
        if name.strip() and accepted:
            found.add(name.strip().lower())
    return found
