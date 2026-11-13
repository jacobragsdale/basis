"""One number, one text: the browser's formatter, `basisFormat` in `static/basis.js`, run under node against every case
of basis-core's `format.json` its kinds cover, a float in each format, and against basis-core's own text for each
display format a column declares; and `bd.text` on the server.

node is on CI's path, where these tests are required; a machine without it skips them."""

import json
import math
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import basis.dash as bd
from basis.core.display import Display, Format
from basis.dash import texts as text
from basis.dash.theme import STATIC

FIXTURE = Path(__file__).parents[2] / "core" / "tests" / "fixtures" / "display" / "format.json"
"""basis-core's copy of parallax's cases, which its own formatter is held to."""
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None and "CI" not in os.environ, reason="the browser's formatter runs under node, which this machine lacks; CI has it, and requires it")

RUNNER = """
const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
globalThis.window = {};
globalThis.document = {documentElement: {getAttribute: () => "light", setAttribute: () => {}}};
eval(SCRIPT);
const decode = (value) => value === "NaN" ? NaN : value === "inf" ? Infinity : value === "-inf" ? -Infinity : value;
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const format = window.dashAgGridFunctions.basisFormat;
console.log(JSON.stringify(cases.map(([value, spec]) => format(decode(value), spec))));
"""

DISPLAYS = (
    Display(kind="number"),
    Display(kind="number", decimals=0),
    Display(kind="number", decimals=4),
    Display(kind="money", currency="USD"),
    Display(kind="money", currency="USD", decimals=0),
    Display(kind="money", currency="JPY"),
    Display(kind="money", currency="EUR", decimals=3),
    Display(kind="money", currency="CHF"),
    Display(kind="money", currency="BHD"),
    Display(kind="pct"),
    Display(kind="pct", decimals=1),
    Display(kind="bps"),
    Display(kind="bps", decimals=0),
    Display(kind="price", decimals=3),
    Display(kind="price32"),
    Display(kind="price32", step="half"),
    Display(kind="price32", step="quarter"),
    Display(kind="price32", step="eighth"),
)
VALUES = (0.0, -0.0, 2.675, -2.675, 1.005, 0.125, 0.045, 999.995, 1234567.891, -1234567.891, 0.0525, -0.000049, 99.5078125, 101.015625, 1e-9, 123456789012.345, 0.1 + 0.2, float("nan"))


def run(cases: Sequence[tuple[object, dict[str, object]]]) -> list[str]:
    """Each case's text, as the browser's formatter writes it."""
    encoded = json.dumps([[_encoded(value), spec] for value, spec in cases])
    ran = subprocess.run([str(NODE), "-e", RUNNER, str(STATIC / "basis.js")], input=encoded, capture_output=True, text=True, encoding="utf-8", check=True, timeout=60)
    found: object = json.loads(ran.stdout)
    assert isinstance(found, list)
    return [str(shown) for shown in found]


def _encoded(value: object) -> object:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, float) and math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value


def _format(rules: dict[str, object]) -> Format:
    renamed = {("thirty_seconds" if name == "thirtySeconds" else name): given for name, given in rules.items()}
    return Format(**renamed)  # ty: ignore[invalid-argument-type] -- the fixture's fields, which basis-core's own test reads the same way


@needs_node
def test_the_browser_writes_every_float_case_of_basis_core_s_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases, expected, where = [], [], []
    for group in fixture["groups"]:
        made = text.spec(_format(group["format"]))
        for case in group["cases"]:
            if case["kind"] != "float":
                continue  # a cell holds a float; decimals, integers, dates and instants reach it as JSON numbers or text
            value = case["value"]
            cases.append((float(value) if isinstance(value, str) else value, made))
            expected.append(case["text"])
            where.append(f"{group['format']} {value!r}")
    assert len(cases) > 3000
    shown = run(cases)
    wrong = [f"{place}: {got!r}, not {want!r}" for place, got, want in zip(where, shown, expected, strict=True) if got != want]
    assert not wrong, "\n".join(wrong[:20])


@needs_node
def test_the_browser_writes_each_display_format_as_the_server_does() -> None:
    cases = [(value, text.spec(display)) for display in DISPLAYS for value in VALUES]
    expected = [display.text(value) for display in DISPLAYS for value in VALUES]
    shown = run(cases)
    wrong = [f"{display.as_json()} {value!r}: {got!r}, not {want!r}" for (display, value), got, want in zip(((d, v) for d in DISPLAYS for v in VALUES), shown, expected, strict=True) if got != want]
    assert not wrong, "\n".join(wrong[:20])


@needs_node
def test_a_missing_value_is_empty_and_text_passes_through() -> None:
    assert run([(None, {}), ("", {}), ("AAA", {"decimals": 2})]) == ["", "", "AAA"]


@needs_node
def test_the_script_adds_to_the_grid_s_functions_and_never_replaces_them() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {dashAgGridFunctions: {appUpper: (v) => String(v).toUpperCase()}, dash_clientside: {app: {own: 1}}};
    eval(SCRIPT);
    console.log(JSON.stringify({grid: Object.keys(window.dashAgGridFunctions).sort(), clientside: Object.keys(window.dash_clientside).sort()}));
    """
    ran = subprocess.run([str(NODE), "-e", probe, str(STATIC / "basis.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)
    assert json.loads(ran.stdout) == {"grid": ["appUpper", "basisCheck", "basisFormat", "basisScale", "basisTotals"], "clientside": ["app", "basis"]}


def test_a_spec_resolves_every_default_the_browser_would_need() -> None:
    assert text.spec(None) == {}
    assert text.spec(Display(kind="money", currency="USD")) == {"decimals": 2, "symbol": "$"}
    assert text.spec(Display(kind="money", currency="JPY")) == {"decimals": 0, "symbol": "\N{YEN SIGN}"}
    assert text.spec(Display(kind="money", currency="CHF", decimals=0)) == {"decimals": 0, "symbol": "CHF\N{NO-BREAK SPACE}"}
    assert text.spec(Display(kind="pct")) == {"decimals": 2, "percent": True}
    assert text.spec(Display(kind="bps", decimals=1)) == {"decimals": 1, "bps": True}
    assert text.spec(Display(kind="price32", step="quarter")) == {"thirtySeconds": "quarters"}
    assert text.spec(Format(negative="parentheses", abbreviate="M", suffixes="par")) == {"abbreviate": "M", "suffixes": "par", "negative": "parentheses"}


def test_a_column_s_formatter_calls_basis_s_function() -> None:
    assert text.formatter(bd.format.money("USD", 0)) == {"function": 'basisFormat(params.value, {"decimals":0,"symbol":"$"})'}


def test_the_server_writes_a_value_as_basis_core_does() -> None:
    assert bd.text(2.675, bd.format.number()) == "2.68"
    assert bd.text(-1234.5, bd.format.money("USD")) == "-$1,234.50"
    assert bd.text(0.0525, bd.format.bps(0)) == "525\N{NO-BREAK SPACE}bp"
    assert bd.text(float("nan"), bd.format.pct()) == ""
    assert bd.text(None) == ""


@needs_node
def test_a_bool_is_a_quiet_check_mark_for_true_and_nothing_else() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {};
    eval(SCRIPT);
    console.log(JSON.stringify([true, false, null, undefined, "true"].map(window.dashAgGridFunctions.basisCheck)));
    """
    ran = subprocess.run([str(NODE), "-e", probe, str(STATIC / "basis.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)
    assert json.loads(ran.stdout) == ["\N{CHECK MARK}", "", "", "", ""]
