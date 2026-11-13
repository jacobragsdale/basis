"""The example's synthetic holdings, made from a seed, so its fixtures are made again exactly, and a test holds the
committed CSV to them: a fixed income book of about three hundred bonds, each priced from a Treasury curve and a spread
by its sector and rating, its issuer under a placeholder name, its CUSIP a valid one of nine characters.

    uv run python packages/dash/tests/example_fixtures.py   # writes basis.dash.example's fixtures again

Nothing in them is real: the curve is a plausible one, the issuers are placeholders, and the CUSIPs are made up."""

import bisect
import csv
import io
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 11, 12)
"""The day the holdings are priced on."""
SEED = 20261112
HOLDINGS = 320
FIXTURES = Path(__file__).parents[1] / "src" / "basis" / "dash" / "example" / "fixtures"

CURVE = ((0.25, 0.0385), (0.5, 0.0380), (1, 0.0370), (2, 0.0355), (3, 0.0354), (5, 0.0368), (7, 0.0386), (10, 0.0408), (20, 0.0455), (30, 0.0462))
"""The Treasury curve the book is priced on: each tenor, in years, and its yield."""
TENORS = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y")
"""Each tenor of `CURVE`, as the desk names it."""
MOVES = {
    "Prior day": (0.00005, 0.00008, 0.00010, 0.00016, 0.00012, -0.00004, -0.00015, -0.00024, -0.00031, -0.00033),
    "One week before": (-0.00020, -0.00012, 0.00010, 0.00042, 0.00048, 0.00030, 0.00006, -0.00024, -0.00041, -0.00046),
    "One month before": (-0.00105, -0.00082, -0.00031, 0.00058, 0.00085, 0.00094, 0.00071, 0.00038, 0.00012, 0.00006),
}
"""How far the curve has moved since each earlier close, by tenor: a day's bull flattening, the front end up and the long
end down, and the month's steepening before it."""
RATINGS = {"AAA": 0.0035, "AA": 0.0055, "A": 0.0085, "BBB": 0.0135, "BB": 0.0260}
"""A corporate bond's spread over the curve, by its rating."""
CORPORATES = (
    "Northwind Energy", "Contoso Financial", "Fabrikam Industries", "Tailspin Airways", "Litware Software", "Adatum Health",
    "Proseware Telecom", "Wingtip Retail", "Woodgrove Bank", "Coho Utilities", "Alpine Resorts", "Lucerne Media",
)  # fmt: skip
MUNICIPALS = ("State of Lakeshore GO", "Riverside County Water", "Metro Transit Authority", "Harbor Port Authority", "Summit School District")
SECURITIZED = ("Agency MBS Pool", "Contoso Auto Receivables", "Northwind Card Trust", "Fabrikam Equipment Trust")
SECTORS = (("Treasury", 0.24), ("Agency", 0.10), ("Corporate", 0.38), ("Municipal", 0.14), ("Securitized", 0.14))
BOOKS = {"Treasury": ("Core Rates", "Liquidity"), "Agency": ("Core Rates", "Liquidity"), "Corporate": ("Credit",), "Municipal": ("Municipals",), "Securitized": ("Core Rates", "Credit")}
FIELDS = ("book", "cusip", "issuer", "sector", "rating", "coupon", "maturity", "par", "price", "prior_price", "ytw", "prior_ytw", "curve_change", "duration", "oas")


@dataclass(frozen=True)
class Bond:
    book: str
    cusip: str
    issuer: str
    sector: str
    rating: str
    coupon: float
    maturity: date
    par: float
    price: float
    prior_price: float
    ytw: float
    prior_ytw: float
    curve_change: float
    duration: float
    oas: float | None


def curve(years: float, points: tuple[float, ...] = tuple(value for _, value in CURVE)) -> float:
    """The yield at `years` of a curve with `points` at `CURVE`'s tenors, linear between them: the Treasury curve itself
    by default, or a move of it."""
    tenors = [tenor for tenor, _ in CURVE]
    if years <= tenors[0]:
        return points[0]
    if years >= tenors[-1]:
        return points[-1]
    right = bisect.bisect_left(tenors, years)
    t0, t1, y0, y1 = tenors[right - 1], tenors[right], points[right - 1], points[right]
    return y0 + (y1 - y0) * (years - t0) / (t1 - t0)


def priced(coupon: float, ytw: float, years: float) -> tuple[float, float]:
    """A semiannual bond's clean price per 100 and its modified duration, at `ytw`, `years` from maturity."""
    periods = max(1, math.ceil(years * 2))
    rate = ytw / 2
    flows = [(k, coupon / 2 * 100 + (100 if k == periods else 0)) for k in range(1, periods + 1)]
    price = sum(flow / (1 + rate) ** k for k, flow in flows)
    macaulay = sum(k / 2 * flow / (1 + rate) ** k for k, flow in flows) / price
    return float(price), float(macaulay / (1 + rate))


def cusip(stem: str) -> str:
    """`stem`, eight characters, with its check digit."""
    total = 0
    for index, character in enumerate(stem):
        value = int(character) if character.isdigit() else ord(character) - ord("A") + 10
        if index % 2:
            value *= 2
        total += value // 10 + value % 10
    return f"{stem}{(10 - total % 10) % 10}"


def _issued(rng: random.Random, sector: str, base: float) -> tuple[str, str, float]:
    """A bond's issuer, its rating and its spread over the curve's yield `base`, by its sector."""
    match sector:
        case "Treasury":
            issuer, rating, spread = "US Treasury", "AAA", 0.0
        case "Agency":
            issuer, rating, spread = rng.choice(("Agency Housing Corp", "Agency Farm Credit")), "AA", rng.uniform(0.0012, 0.0030)
        case "Corporate":
            issuer = rng.choice(CORPORATES)
            rating = rng.choices(list(RATINGS), weights=[1, 3, 6, 7, 2])[0]
            spread = RATINGS[rating] * rng.uniform(0.8, 1.25)
        case "Municipal":
            issuer, rating = rng.choice(MUNICIPALS), rng.choice(("AAA", "AA", "AA", "A"))
            spread = -base * rng.uniform(0.24, 0.32)  # tax-exempt: about seventy percent of the curve's yield
        case _:
            issuer, rating = rng.choice(SECURITIZED), rng.choice(("AAA", "AAA", "AA"))
            spread = rng.uniform(0.0060, 0.0115)
    return issuer, rating, spread


def _spread_change(rng: random.Random, sector: str, moved: float) -> float:
    """How far a bond's spread moved over the day, by its sector: credit tighter, securitized a little wider, and
    municipals lagging the curve's move `moved`."""
    match sector:
        case "Corporate":
            spread_change = rng.gauss(-0.00010, 0.00012)
        case "Securitized":
            spread_change = rng.gauss(0.00005, 0.00008)
        case "Agency":
            spread_change = rng.gauss(0.0, 0.00003)
        case "Municipal":
            spread_change = -0.3 * moved + rng.gauss(0.0, 0.00008)
        case _:
            spread_change = 0.0
    return spread_change


def bonds() -> list[Bond]:
    """The book, from the seed."""
    rng = random.Random(SEED)  # noqa: S311 -- synthetic data made again from its seed, never a secret
    found: list[Bond] = []
    stems: set[str] = set()
    for _ in range(HOLDINGS):
        sector = rng.choices([name for name, _ in SECTORS], weights=[weight for _, weight in SECTORS])[0]
        book = rng.choice(BOOKS[sector])
        short = book == "Liquidity"
        years = rng.uniform(0.3, 2.0) if short else rng.choice([rng.uniform(1, 5), rng.uniform(5, 12), rng.uniform(12, 30)])
        if sector == "Securitized":
            years = min(years, rng.uniform(3, 8))  # a pool's or a trust's expected life, which prepayments shorten
        maturity = AS_OF + timedelta(days=round(years * 365.25))
        base = curve(years)
        issuer, rating, spread = _issued(rng, sector, base)
        ytw = base + spread
        coupon = max(0.005, round((ytw + rng.uniform(-0.012, 0.010)) * 800) / 800)  # issued at another time's rate, in eighths
        price, duration = priced(coupon, ytw, years)
        moved = curve(years, MOVES["Prior day"])
        spread_change = _spread_change(rng, sector, moved)
        prior_ytw = ytw - moved - spread_change
        prior_price, _ = priced(coupon, prior_ytw, years + 1 / 365.25)
        par = rng.choice((0.5, 1, 2, 2.5, 5, 5, 10, 10, 15, 25)) * 1_000_000
        stem = ""
        while not stem or stem in stems:
            stem = "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(6)) + f"{rng.randint(0, 99):02d}"
        stems.add(stem)
        oas = None if sector in {"Treasury", "Municipal"} else spread
        found.append(Bond(book, cusip(stem), issuer, sector, rating, coupon, maturity, par, price, prior_price, ytw, prior_ytw, moved, duration, oas))
    return sorted(found, key=lambda bond: (bond.book, bond.sector, bond.issuer, bond.maturity, bond.cusip))


def text() -> str:
    """The holdings' CSV, as the fixture holds it."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(FIELDS)
    for bond in bonds():
        oas = "" if bond.oas is None else f"{bond.oas:.6f}"
        writer.writerow(
            [
                bond.book,
                bond.cusip,
                bond.issuer,
                bond.sector,
                bond.rating,
                f"{bond.coupon:.5f}",
                bond.maturity.isoformat(),
                f"{bond.par:.0f}",
                f"{bond.price:.4f}",
                f"{bond.prior_price:.4f}",
                f"{bond.ytw:.6f}",
                f"{bond.prior_ytw:.6f}",
                f"{bond.curve_change:.6f}",
                f"{bond.duration:.3f}",
                oas,
            ]
        )
    return out.getvalue()


def curve_text() -> str:
    """The Treasury curve's CSV, as the fixture holds it: each tenor at the close asked for, and at each earlier close."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(("close", "tenor", "ytw"))
    closes = {"As of": (0.0,) * len(CURVE), **MOVES}
    for close, moves in closes.items():
        for (_, value), tenor, move in zip(CURVE, TENORS, moves, strict=True):
            writer.writerow((close, tenor, f"{value - move:.6f}"))
    return out.getvalue()


FILES = {"holdings.csv": text, "curve.csv": curve_text}
"""Each fixture of the example's, by its file's name, with what makes its text."""

if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, made in FILES.items():
        (FIXTURES / name).write_text(made(), encoding="utf-8", newline="\n")
