"""Finding a service: `load`, by its entry point in the `basis.apis` group, its module, or `module:attribute`, as
`basis serve` and every verb that takes a SERVICE do."""

import importlib
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points

from starlette.applications import Starlette

from basis.api.errors import DeclarationError
from basis.api.service import Service

GROUP = "basis.apis"
"""The entry point group a repo lists its service in, `NAME = "module:attribute"`."""


def load(reference: str | None = None, *, listed: Iterable[EntryPoint] | None = None) -> Service:
    """The service `reference` names: an entry point in the `basis.apis` group, a module whose `service` it is, or
    `module:attribute`. With none, the one service listed. `listed` is the entry points to choose from, by default the
    installed packages' `basis.apis`."""
    entries = {entry.name: entry for entry in (listed if listed is not None else entry_points(group=GROUP))}
    if reference is None:
        if len(entries) != 1:
            listing = f'[project.entry-points."{GROUP}"]'
            msg = f"the installed packages list {sorted(entries) or 'no services'} in the {GROUP} entry points: name the service to serve, or list the repo's one under {listing} in its pyproject.toml"
            raise DeclarationError(msg)
        reference = next(iter(entries))
    entry = entries.get(reference) or EntryPoint(name=reference, value=reference, group=GROUP)
    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name is not None and (entry.module == error.name or entry.module.startswith(f"{error.name}.")):
            reason = f"no service named {reference!r}: the installed packages list {sorted(entries) or 'none'} in the {GROUP} entry points, and no module has that path"
            msg = f'{reason}: give its module path, as basis.api.example.service, or list it under [project.entry-points."{GROUP}"]'
            raise DeclarationError(msg) from error
        raise  # the module exists, and something it imports does not
    attribute = entry.attr or "service"
    found: object = module
    for part in attribute.split("."):
        found = getattr(found, part, None)
    if isinstance(found, Service):
        return found
    where = f"{entry.module}:{attribute}"
    if isinstance(found, Starlette):
        msg = f"{where} is an app the service made itself, which basis does not serve: declare `service = api.Service(name=..., settings=..., routes=[...])`, and basis builds its app"
        raise DeclarationError(msg)
    msg = f"{where} is not an api.Service: declare `service = api.Service(name=..., settings=..., routes=[...])` there"
    raise DeclarationError(msg)
