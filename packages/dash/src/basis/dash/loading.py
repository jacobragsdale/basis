"""Finding an app: `load`, by its entry point in the `basis.dash_apps` group, its module, or `module:attribute`, as
`basis serve` and `basis dev` do."""

import importlib
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points

import dash
from basis.dash.app import App
from basis.dash.errors import DeclarationError

GROUP = "basis.dash_apps"
"""The entry point group a repo lists its app in, `NAME = "module:attribute"`."""


def load(reference: str | None = None, *, listed: Iterable[EntryPoint] | None = None) -> App:
    """The app `reference` names: an entry point in the `basis.dash_apps` group, a module whose `app` it is, or
    `module:attribute`. With none, the one app listed. `listed` is the entry points to choose from, by default the
    installed packages' `basis.dash_apps`."""
    entries = {entry.name: entry for entry in (listed if listed is not None else entry_points(group=GROUP))}
    if reference is None:
        if len(entries) != 1:
            listing = f'[project.entry-points."{GROUP}"]'
            msg = f"the installed packages list {sorted(entries) or 'no apps'} in the {GROUP} entry points: name the app to serve, or list the repo's one under {listing} in its pyproject.toml"
            raise DeclarationError(msg)
        reference = next(iter(entries))
    entry = entries.get(reference) or EntryPoint(name=reference, value=reference, group=GROUP)
    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name is not None and (entry.module == error.name or entry.module.startswith(f"{error.name}.")):
            reason = f"no app named {reference!r}: the installed packages list {sorted(entries) or 'none'} in the {GROUP} entry points, and no module has that path"
            msg = f'{reason}: give its module path, as positions.app, or list it under [project.entry-points."{GROUP}"]'
            raise DeclarationError(msg) from error
        raise  # the module exists, and something it imports does not
    attribute = entry.attr or "app"
    found: object = module
    for part in attribute.split("."):
        found = getattr(found, part, None)
    if isinstance(found, App):
        return found
    where = f"{entry.module}:{attribute}"
    if isinstance(found, dash.Dash):
        msg = f"{where} is a Dash app the repo made itself, which basis does not serve: declare `app = bd.App(name=..., title=..., settings=..., pages=[...])`, and basis builds it"
        raise DeclarationError(msg)
    msg = f"{where} is not a bd.App: declare `app = bd.App(name=..., title=..., settings=..., pages=[...])` there"
    raise DeclarationError(msg)
