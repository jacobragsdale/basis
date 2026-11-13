"""basis's look, from one table: the table against basis-ui's `theme.css`, pinned in `fixtures/theme.css`; the CSS, the
Mantine theme, the grid's theme and the Plotly template made from it; text that reads on both backgrounds; and the
files an app serves for it, from its own origin."""

import json
import math
import re
from http import HTTPStatus
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
import pytest
from dash import html

import basis.dash as bd
from basis.dash import assets, testing, theme
from basis.dash.theme import SCHEMES, Scheme

PINNED = (Path(__file__).parent / "fixtures" / "theme.css").read_text(encoding="utf-8")
"""basis-ui's generated `packages/ui/src/styles/theme.css`, at the commit its first line names."""

TEXT = (
    ("foreground", "background"),
    ("foreground", "card"),
    ("card-foreground", "card"),
    ("popover-foreground", "popover"),
    ("muted-foreground", "background"),
    ("muted-foreground", "card"),
    ("muted-foreground", "muted"),
    ("secondary-foreground", "secondary"),
    ("accent-foreground", "accent"),
    ("primary-foreground", "primary"),
    ("primary-foreground", "primary-hover"),
    ("primary", "card"),
    ("primary", "background"),
    ("destructive-foreground", "destructive"),
    ("destructive", "card"),
    ("destructive", "destructive-subtle"),
    ("success-foreground", "success"),
    ("success", "success-subtle"),
    ("warning-foreground", "warning"),
    ("warning", "warning-subtle"),
    ("info-foreground", "info"),
    ("info", "info-subtle"),
    ("gain", "card"),
    ("loss", "card"),
    ("gain", "muted"),
    ("loss", "muted"),
    ("foreground", "selection"),
    ("env-dev-foreground", "env-dev"),
    ("env-qa-foreground", "env-qa"),
    ("env-prod-foreground", "env-prod"),
)
"""Each text token a page shows, on the surface it shows on: a cell's gain or loss on a card or a totals row, a
badge's text on its fill, a link on a card."""

CONTROLS = (("ring", "background"), ("ring", "card"), ("input", "card"), ("input", "background"))
"""The focus ring and an input's edge, which WCAG 2.1 holds to 3:1 against what is beside them (1.4.11)."""


class Settings(bd.Settings):
    pass


APP = bd.App(name="themed", title="Themed", settings=Settings, pages=[bd.page("/", "Overview", layout=html.Div("themed"))])


def test_the_table_is_basis_ui_s_default_theme_value_for_value() -> None:
    light, dark = theme.tokens(PINNED, ":root"), theme.tokens(PINNED, ".dark")
    assert list(theme.TOKENS) == list(light)
    assert {name: theme.TOKENS[name][0] for name in light} == light
    assert {name: given for name, (_, given) in theme.TOKENS.items() if given is not None} == dark


def test_the_scale_is_basis_ui_s() -> None:
    shared = theme.tokens(PINNED, "@theme")
    assert {name: shared[name] for name in theme.SCALE} == theme.SCALE


def test_the_served_css_is_the_table_s() -> None:
    written = (theme.STATIC / "theme.css").read_text(encoding="utf-8")
    assert written == theme.stylesheet(), "static/theme.css is stale: run `uv run python -m basis.dash.theme`"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_scheme_sets_every_colour(scheme: Scheme) -> None:
    css = theme.stylesheet()
    block = theme.tokens(css, ':root,\n:root[data-mantine-color-scheme="light"]' if scheme == "light" else ':root[data-mantine-color-scheme="dark"]')
    expected = {f"basis-{name}": theme.value(name, scheme) for name, (_, dark) in theme.TOKENS.items() if scheme == "light" or dark is not None}
    assert {name: given for name, given in block.items() if name in expected} == expected


def test_mantine_s_variables_point_at_tokens_that_exist() -> None:
    assert set(theme.MANTINE.values()) <= set(theme.TOKENS)
    mapped = theme.tokens(theme.stylesheet(), "html:root[data-mantine-color-scheme]")
    for used in re.findall(r"var\(--basis-([\w-]+)\)", " ".join(mapped.values())):
        assert used in theme.TOKENS or used in theme.SCALE


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("text", "surface"), TEXT)
def test_text_reads_at_4_5_to_1(scheme: Scheme, text: str, surface: str) -> None:
    ratio = theme.contrast(theme.value(text, scheme), theme.value(surface, scheme))
    assert ratio >= 4.5, f"{text} on {surface} in {scheme} is {ratio:.2f}:1"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("control", "surface"), CONTROLS)
def test_the_focus_ring_and_inputs_stand_out_at_3_to_1(scheme: Scheme, control: str, surface: str) -> None:
    ratio = theme.contrast(theme.value(control, scheme), theme.value(surface, scheme))
    assert ratio >= 3, f"{control} on {surface} in {scheme} is {ratio:.2f}:1"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_series_reads_on_the_card(scheme: Scheme) -> None:
    card = theme.value("card", scheme)
    for colour in theme.SERIES:
        lightness, chroma = _oklch(colour)
        assert 0.48 <= lightness <= 0.67, f"{colour}'s lightness is {lightness:.3f}"
        assert chroma >= 0.1, f"{colour}'s chroma is {chroma:.3f}"
        assert theme.contrast(colour, card) >= 3, f"{colour} on {scheme}'s card"
    assert len(set(theme.SERIES)) == len(theme.SERIES)


def test_srgb_maps_oklch_as_a_browser_does() -> None:
    assert theme.srgb("card", "light") == "#ffffff"
    assert theme.srgb("chart-1", "light") == "#2a78d6"
    assert theme.srgb("primary", "light") == "#104fca"
    assert theme.srgb("overlay", "dark") == "rgba(14, 16, 19, 0.7)"
    assert all(0 <= channel <= 1 for channel in theme.rgba("oklch(0.62 0.4 25)")[:3])  # far outside sRGB: chroma goes until it is inside
    with pytest.raises(ValueError, match="no colour basis reads"):
        theme.rgba("rebeccapurple")


def test_mantine_s_primary_palette_holds_each_scheme_s_primary() -> None:
    made = theme.mantine()
    shades = made["colors"]["basis"]  # ty: ignore[not-subscriptable] -- the theme's colours, a dict of palettes
    assert len(shades) == 10
    assert shades[theme.PRIMARY_SHADE["light"]] == theme.srgb("primary", "light")
    assert shades[theme.PRIMARY_SHADE["dark"]] == theme.srgb("primary", "dark")
    assert made["primaryColor"] == "basis"


def test_the_grid_s_theme_is_quartz_with_every_colour_a_token() -> None:
    function = theme.GRID["function"]
    assert function.startswith("themeQuartz.withParams(")
    params = json.loads(function.removeprefix("themeQuartz.withParams(").removesuffix(")"))
    colours = {name: given for name, given in params.items() if name.endswith("Color")}
    assert colours
    for name, given in colours.items():
        found = re.fullmatch(r"var\(--basis-([\w-]+)\)", given)
        assert found is not None, f"{name} is {given}, not a token"
        assert found.group(1) in theme.TOKENS
    assert "http" not in function


def test_the_template_is_small_transparent_and_the_process_s_default() -> None:
    made = theme.template().to_plotly_json()
    assert len(json.dumps(made, separators=(",", ":"))) < 512
    assert made["layout"]["paper_bgcolor"] == made["layout"]["plot_bgcolor"] == "rgba(0,0,0,0)"
    assert made["layout"]["colorway"] == list(theme.SERIES)
    assert pio.templates.default == theme.TEMPLATE
    assert go.Figure().layout.template.layout.colorway == theme.SERIES


def test_an_app_serves_the_theme_and_its_fonts_from_its_own_origin() -> None:
    with testing.client(APP) as client:
        index = client.index()
        links = re.findall(r'href="([^"]+\.css)"', index)
        named = [link for link in links if "/basis.dash/" in link]
        assert [Path(link).name.split(".")[0] for link in named] == ["theme", "icons", "basis"]
        assert not [link for link in links if link.startswith(("http:", "https:", "//"))]
        served = client.http.get(named[0])
        assert served.status_code == HTTPStatus.OK
        assert served.text == theme.stylesheet()
        fonts = re.findall(r'url\("([^"]+)"\)', served.text)
        assert len(fonts) == len(theme.FONTS)
        for font in fonts:
            answered = client.http.get(f"{named[0].rsplit('/', 1)[0]}/{font}")
            assert answered.status_code == HTTPStatus.OK, font
            assert answered.content == (theme.STATIC / "fonts" / f"{font.split('/')[-1].split('.')[0]}.woff2").read_bytes()
            assert "max-age=31536000" in answered.headers.get("cache-control", "")


def test_the_provider_takes_basis_s_theme() -> None:
    with testing.client(APP) as client:
        layout = client.layout()
    assert layout["type"] == "MantineProvider"
    assert _props(layout)["theme"] == theme.mantine()
    assert assets.FONTS == ("static/fonts/inter.woff2", "static/fonts/jetbrains-mono.woff2")


def _props(component: dict[str, object]) -> dict[str, object]:
    props = component["props"]
    assert isinstance(props, dict)
    return {str(name): given for name, given in props.items()}


def _oklch(colour: str) -> tuple[float, float]:
    """An sRGB colour's OKLCH lightness and chroma, by Björn Ottosson's matrices."""
    red, green, blue, _ = theme.rgba(colour)
    linear = [channel / 12.92 if channel <= 0.04045 else math.pow((channel + 0.055) / 1.055, 2.4) for channel in (red, green, blue)]
    long = math.cbrt(0.4122214708 * linear[0] + 0.5363325363 * linear[1] + 0.0514459929 * linear[2])
    medium = math.cbrt(0.2119034982 * linear[0] + 0.6806995451 * linear[1] + 0.1073969566 * linear[2])
    short = math.cbrt(0.0883024619 * linear[0] + 0.2817188376 * linear[1] + 0.6299787005 * linear[2])
    lightness = 0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short
    a = 1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short
    b = 0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short
    return lightness, math.hypot(a, b)
