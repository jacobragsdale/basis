"""The docs for job authors, which ship with the package, so a repo reads the ones for the version it installed:
`etl docs` lists them, and `etl docs PAGE` prints one."""

from pathlib import Path

_DOCS = Path(__file__).parent


def pages() -> list[str]:
    return sorted(path.relative_to(_DOCS).with_suffix("").as_posix() for path in _DOCS.rglob("*.md"))


def read(page: str) -> str:
    if page not in pages():
        msg = f"no page {page!r}: the pages are {', '.join(pages())}"
        raise LookupError(msg)
    return (_DOCS / f"{page}.md").read_text(encoding="utf-8")
