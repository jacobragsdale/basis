"""Every function, class, method and property in the workspace nothing uses, which ruff does not see: one line each,
`PATH:LINE: unused KIND NAME`, and exit 1 where there is any. The commit hook runs it, and so CI does.

    uv run python scripts/dead_code.py

It reads the packages' code and their tests together, through vulture, so a name only the tests use counts as used:
a test is how a helper of the public surface is held to what it says. What vulture cannot see a caller of, such as a
validator pydantic calls, a pytest hook, or a method a library calls by name, is passed over by its decorator or its
name, or kept in `KEPT` with the reason."""

import sys
from pathlib import Path

from vulture import Vulture

ROOT = Path(__file__).resolve().parent.parent
PATHS = [ROOT / "packages", ROOT / "scripts", ROOT / "evals"]
EXCLUDE = ["*/templates/*", "*/.venv/*", "*/__pycache__/*"]
KINDS = frozenset({"function", "class", "method", "property"})
"""What it reports: a variable or an attribute is as often a field a library reads, which vulture cannot tell."""
DECORATORS = ["@field_validator", "@model_validator", "@override", "@pytest.fixture", "@*.setter", "@pytest.hookimpl", "@*.get", "@*.post", "@*.put", "@*.patch", "@*.delete", "@*.api_route"]
"""A function behind one of these is called by its library, not by name: a validator, a fixture, a hook, a route."""
NAMES = ["pytest_*", "__getattr__", "do_GET", "do_HEAD", "do_POST"]
"""A pytest hook, a module's lookup of a name it does not define, and an `http.server` handler's methods."""
KEPT: dict[str, str] = {
    "Poster": "the protocol of a Slack channel's poster, which `basis.core.sending.SlackPoster` and the fakes meet",
    "Mailer": "the protocol of an email's mailer, which `basis.core.sending.SmtpMailer` and the fakes meet",
    "get_type_name": "a pyarrow filesystem handler's, which pyarrow calls by name",
    "normalize_path": "a pyarrow filesystem handler's, as above",
    "get_file_info_selector": "a pyarrow filesystem handler's, as above",
    "delete_root_dir_contents": "a pyarrow filesystem handler's, as above",
    "Hidden": "a test's class whose being unused is what the test checks",
    "Twice": "a test's class whose declaration is what the test refuses, so it is never made",
    "without_pricing": 'a test\'s fakes, which `Service(fakes="module:function")` names as text',
}
"""Each name vulture finds no use of that is used, with why."""


def unused() -> list[str]:
    """Each unused function, class, method and property, as `PATH:LINE: unused KIND NAME`."""
    found = Vulture(ignore_names=[*NAMES, *KEPT], ignore_decorators=DECORATORS)
    found.scavenge([str(path) for path in PATHS if path.is_dir()], exclude=EXCLUDE)
    return [
        f"{Path(item.filename).resolve().relative_to(ROOT).as_posix()}:{item.first_lineno}: unused {item.typ} {item.name}"
        for item in sorted(found.get_unused_code(min_confidence=60), key=lambda item: (str(item.filename), item.first_lineno))
        if item.typ in KINDS
    ]


def main() -> int:
    lines = unused()
    for line in lines:
        sys.stdout.write(f"{line}\n")
    if lines:
        sys.stderr.write("ERROR: the code above is never used: remove it, or, where something calls it that vulture cannot see, add it to scripts/dead_code.py's KEPT with why\n")
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
