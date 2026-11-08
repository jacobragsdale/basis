"""A built UI the app serves at `/`, beside its routes under `/api` (`Service.ui`), so one container serves a UI and its
API on one origin (design.md, "One origin").

The folder is found from the folder the app runs in, as it is when the request comes, so a UI built after the app
started is served. A file the folder holds is served as it is; any other path that names no file, as a client-side
route such as `/positions/7` does, is the UI's `index.html`, which routes it in the browser. A path under `/api` that no
route matches is the API's 404, never the UI. A folder not built yet answers 503, saying how to build it, and the
app's routes go on answering."""

from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import override

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from basis.api import errors
from basis.api.routes import API

INDEX = "index.html"


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
