"""A file's path inside a shared folder, as a job declares it and `basis.core.folders` opens it: pure, so a
declaration checks it where it is made."""

import re
from pathlib import PurePosixPath, PureWindowsPath

WILD = re.compile(r"[*?]")
"""What a file's name in a pattern may hold: any run of characters, or any one."""


class OutsideError(ValueError):
    """A path that leaves its folder."""


def inside(relative: str, folder: str) -> tuple[str, ...]:
    """The parts of `relative`, a path inside the folder `folder`: relative, of `/`-separated parts, none of them `..`."""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or PureWindowsPath(relative).is_absolute() or PurePosixPath(relative).is_absolute() or ".." in parts or PureWindowsPath(relative).drive:
        msg = f"{relative!r} is no path inside the folder {folder}: give one relative to it, as positions/2026-11-05.xlsx, which never leaves it"
        raise OutsideError(msg)
    return parts
