"""The fakes a service runs on in `basis dev` and `basis call --fake`. A service names the function that makes them, as
`api.Service(..., fakes="accruals.dev:fakes")`: the repo's own code, which takes nothing and returns a fake for each
resource by resource, as `api.build(..., fakes=...)` takes them, with its data written out in it or read from the
repo's synthetic fixtures. basis infers no data, and never mixes a fake with a real database or service: the function
fakes every database and every other service the service declares. A cache, the clock, ids and compute functions reach
nothing outside the process, so each is the real one unless the function fakes it too."""

import importlib
import inspect
from collections.abc import Mapping

from basis.api import fakes as faked
from basis.api.compute import ComputeFunction
from basis.api.resources import CacheResource, ClockResource, DatabaseResource, HttpResource, IdsResource
from basis.api.service import Service, ServiceError

_KEYS = (DatabaseResource, HttpResource, CacheResource, ClockResource, IdsResource, ComputeFunction)
_FAKES = (faked.Database, faked.Http, faked.Cache, faked.Clock, faked.Ids, faked.Compute)


def fakes(service: Service) -> dict[faked.Faked, faked.Fake]:
    """The fakes `service` declares, after checking that they fake every database and other service it reaches."""
    reference = service.fakes
    if reference is None:
        msg = (
            f"service {service.name} declares no fakes to run on: give its api.Service fakes='module:function', a function of the repo's own"
            " that returns a fake for each database and other service it declares, by resource, as api.build(..., fakes=...) takes them"
        )
        raise ServiceError(msg)
    module, _, attribute = reference.partition(":")
    try:
        made: object = getattr(importlib.import_module(module), attribute)
    except ModuleNotFoundError as error:
        if error.name is None or not (module == error.name or module.startswith(f"{error.name}.")):
            raise  # the module exists, and something it imports does not
        msg = f"service {service.name}'s fakes are {reference}, and no module {module} exists: give fakes='module:function' the module that makes them"
        raise ServiceError(msg) from error
    except AttributeError as error:
        msg = f"service {service.name}'s fakes are {reference}, and {module} has no {attribute}: define it there, or name the function that makes them"
        raise ServiceError(msg) from error
    if not inspect.isfunction(made):
        msg = f"service {service.name}'s fakes are {reference}, a {type(made).__name__}: name a function that takes nothing and returns them"
        raise ServiceError(msg)
    given: object = made()
    if not isinstance(given, Mapping):
        msg = f"{reference} returns a {type(given).__name__}: return a fake for each resource, by resource, as {{WAREHOUSE: fakes.Database(...)}}"
        raise ServiceError(msg)
    found: dict[faked.Faked, faked.Fake] = {}
    for key, fake in given.items():
        if not isinstance(key, _KEYS) or not isinstance(fake, _FAKES):
            msg = f"{reference} returns {key!r}: {fake!r}: return a fake from basis.api.fakes for each resource or compute function, by the declaration"
            raise ServiceError(msg)
        found[key] = fake
    if missing := [resource.name for resource in service.resources if isinstance(resource, DatabaseResource | HttpResource) and resource not in found]:
        msg = f"{reference} fakes none of {', '.join(missing)}, and service {service.name} runs on fakes alone here: return a fake for each database and other service it declares"
        raise ServiceError(msg)
    return found
