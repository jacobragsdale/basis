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
FIELDS = ("book", "cusip", "issuer", "sector", "rating", "coupon", "maturity", "par", "price", "ytw", "duration", "oas")


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
    ytw: float
    duration: float
    oas: float | None


def curve(years: float) -> float:
    """The Treasury curve's yield at `years`, linear between its tenors."""
    tenors = [tenor for tenor, _ in CURVE]
    if years <= tenors[0]:
        return CURVE[0][1]
    if years >= tenors[-1]:
        return CURVE[-1][1]
    right = bisect.bisect_left(tenors, years)
    (t0, y0), (t1, y1) = CURVE[right - 1], CURVE[right]
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
        ytw = base + spread
        coupon = max(0.005, round((ytw + rng.uniform(-0.012, 0.010)) * 800) / 800)  # issued at another time's rate, in eighths
        price, duration = priced(coupon, ytw, years)
        par = rng.choice((0.5, 1, 2, 2.5, 5, 5, 10, 10, 15, 25)) * 1_000_000
        stem = ""
        while not stem or stem in stems:
            stem = "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(6)) + f"{rng.randint(0, 99):02d}"
        stems.add(stem)
        oas = None if sector in {"Treasury", "Municipal"} else spread
        found.append(Bond(book, cusip(stem), issuer, sector, rating, coupon, maturity, par, price, ytw, duration, oas))
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
                f"{bond.ytw:.6f}",
                f"{bond.duration:.3f}",
                oas,
            ]
        )
    return out.getvalue()


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "holdings.csv").write_text(text(), encoding="utf-8", newline="\n")
