"""Every name of Dash's that basis touches and Dash does not document (`basis.dash.internals`) is where basis expects
it: an upgrade of Dash that moves one fails here, and nowhere else."""

import inspect

import dash
from dash import _callback, _callback_context, _dash_renderer, _pages
from dash.backends import _fastapi

from basis.dash import internals


def test_react_is_chosen_where_dash_reads_it() -> None:
    assert list(inspect.signature(_dash_renderer._set_react_version).parameters) == ["v_react", "v_reactdom"]  # noqa: SLF001
    internals.choose_react()
    assert any(f"react@{internals.REACT}" in str(entry.get("relative_package_path")) for entry in _dash_renderer._js_dist_dependencies)  # noqa: SLF001


def test_dash_keeps_its_callbacks_and_pages_where_basis_reads_them() -> None:
    assert isinstance(_callback.GLOBAL_CALLBACK_MAP, dict)
    assert isinstance(_callback.GLOBAL_CALLBACK_LIST, list)
    assert _pages.PAGE_REGISTRY is dash.page_registry
    assert callable(dash.Dash._setup_server)  # noqa: SLF001
    assert hasattr(dash.Dash(__name__, server=False), "_callback_list")


def test_a_callbacks_function_is_the_one_dash_wraps() -> None:
    def shown(value: str) -> str:
        return value

    @dash.callback(dash.Output("internals-out", "children"), dash.Input("internals-in", "value"))
    def wrapped(value: str) -> str:
        return shown(value)

    entry = _callback.GLOBAL_CALLBACK_MAP.pop("internals-out.children")
    _callback.GLOBAL_CALLBACK_LIST[:] = [found for found in _callback.GLOBAL_CALLBACK_LIST if found.get("output") != "internals-out.children"]
    assert entry["callback"].__wrapped__ is wrapped
    assert wrapped.__module__ == __name__


def test_the_wire_format_and_the_fastapi_backends_variable() -> None:
    assert internals.FASTAPI_CONFIG == _fastapi._ENV_CONFIG  # noqa: SLF001
    assert internals.request("a.children", [{"id": "b", "property": "value", "value": 1}], changed=["b.value"]) == {
        "output": "a.children",
        "outputs": {"id": "a", "property": "children"},
        "inputs": [{"id": "b", "property": "value", "value": 1}],
        "state": [],
        "changedPropIds": ["b.value"],
    }
    assert internals.request("..a.children...b.data..", [])["outputs"] == [{"id": "a", "property": "children"}, {"id": "b", "property": "data"}]


def test_a_callbacks_context_holds_what_set_props_set() -> None:
    assert not internals.in_callback()
    assert internals.updated("basis-status", "data") is None
    context = dash._utils.AttributeDict(updated_props={})  # noqa: SLF001 -- what Dash's dispatch sets for each callback
    token = _callback_context.context_value.set(context)
    try:
        assert internals.in_callback()
        dash.set_props("basis-status", {"data": {"a": 1}})
        assert internals.updated("basis-status", "data") == {"a": 1}
    finally:
        _callback_context.context_value.reset(token)
