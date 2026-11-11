"""How an app opens its resources, worked out when it is built: `plan` checks that each fake a test gives fakes a
resource of the service's, of its kind, or a compute function its routes ask for, and that the settings configure every
resource that has none, and the process pool where a compute function has none, so that a missing setting stops the
app before it serves. `basis.api.opened` opens what it plans."""

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import assert_never

from basis.api import fakes
from basis.api.compute import ComputeFunction
from basis.api.resources import CacheResource, ClockResource, DatabaseResource, HttpResource, IdsResource, Resource
from basis.api.service import DeclarationError, Service
from basis.api.settings import Settings
from basis.core.settings import MssqlSettings, SettingsError, variable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    """How an app opens its resources: each name's fake, where a test gave one, and otherwise its settings; and the
    compute functions its routes ask for, each run in the pool unless a test fakes it."""

    service: Service
    settings: Settings
    fakes: Mapping[str, fakes.Fake]
    compute: tuple[ComputeFunction[..., object], ...]
    in_process: frozenset[ComputeFunction[..., object]]
    """The compute functions a test fakes, which run in this process."""

    @property
    def pooled(self) -> tuple[ComputeFunction[..., object], ...]:
        return tuple(declared for declared in self.compute if declared not in self.in_process)


def plan(service: Service, settings: Settings, given: fakes.Fakes) -> Plan:
    """The resources' plan, after checking that each fake fakes a resource of the service's, of its kind, or a compute
    function its routes ask for, and that the settings configure every resource that has none: a database, and its
    write login where a route writes it, another service's URL, and the pool for compute functions."""
    asked: dict[ComputeFunction[..., object], None] = {}
    for route in service.routes:
        for ask in route.asks:
            if isinstance(ask.given, ComputeFunction):
                asked[ask.given] = None
    compute = tuple(asked)
    in_process: set[ComputeFunction[..., object]] = set()
    for resource, fake in given.items():
        if isinstance(resource, ComputeFunction):
            if resource not in compute:
                msg = f"a fake is given for the compute function {resource.name}, which no route of service {service.name} asks for: fake the ones its routes run"
                raise DeclarationError(msg)
            if not isinstance(fake, fakes.Compute):
                msg = f"the compute function {resource.name} is given a fakes.{type(fake).__name__}: give it fakes.Compute()"
                raise DeclarationError(msg)
            in_process.add(resource)
    outside = f"which service {service.name} does not declare: fake the service's own resources"
    by_name = faked({resource: fake for resource, fake in given.items() if not isinstance(resource, ComputeFunction)}, service.resources, outside)
    written = {route.writes.name for route in service.routes if route.writes is not None}
    unset = [found for resource in service.resources if resource.name not in by_name and (found := unconfigured(resource, settings, written=resource.name in written)) is not None]
    if settings.compute is None and (pooled := [declared.name for declared in compute if declared not in in_process]):
        unset.append(f"{variable(('compute', 'workers'))}, the processes the compute functions {', '.join(pooled)} run in")
    if unset:
        msg = f"service {service.name}'s resources are not all configured, so the API cannot start: set {'; '.join(unset)}"
        raise SettingsError(msg)
    return Plan(service, settings, by_name, compute, frozenset(in_process))


def unconfigured(resource: Resource, settings: Settings, *, written: bool) -> str | None:
    """The settings `resource` needs that are not set, by their variables, or None."""
    match resource:
        case DatabaseResource():
            if (database := settings.databases.get(resource.name)) is None:
                return f"{variable(('databases', resource.name, 'kind'))} and the rest of its block, for the database {resource.name}"
            if written and database.write is None:
                logins = ("connection_string",) if isinstance(database, MssqlSettings) else ("user", "password")
                return f"{' and '.join(variable(('databases', resource.name, 'write', part)) for part in logins)}, the login through which routes write {resource.name}"
            return None
        case HttpResource():
            return None if resource.name in settings.http else f"{variable(('http', resource.name, 'url'))}, the base URL of the service {resource.name}"
        case CacheResource() | ClockResource() | IdsResource():
            return None
        case unreachable:
            assert_never(unreachable)


def faked(given: fakes.Fakes, reached: Collection[Resource], outside: str) -> dict[str, fakes.Fake]:
    """Each fake `given`, by its resource's name, after checking that it fakes one of the resources `reached`, of its
    kind; `outside` says why a fake of any other is refused, and the fix."""
    by_name: dict[str, fakes.Fake] = {}
    for resource, fake in given.items():
        if isinstance(resource, ComputeFunction) or resource not in reached:
            msg = f"a fake is given for {resource.name}, {outside}"
            raise DeclarationError(msg)
        if not fits(resource, fake):
            msg = f"{resource.name} is given a fakes.{type(fake).__name__}, which does not fake its kind of resource: give it the fake of its kind"
            raise DeclarationError(msg)
        by_name[resource.name] = fake
    return by_name


def fits(resource: Resource, fake: fakes.Fake) -> bool:
    """Whether `fake` fakes `resource`'s kind of resource."""
    match resource:
        case DatabaseResource():
            return isinstance(fake, fakes.Database)
        case HttpResource():
            return isinstance(fake, fakes.Http)
        case CacheResource():
            return isinstance(fake, fakes.Cache)
        case ClockResource():
            return isinstance(fake, fakes.Clock)
        case IdsResource():
            return isinstance(fake, fakes.Ids)
        case unreachable:
            assert_never(unreachable)
