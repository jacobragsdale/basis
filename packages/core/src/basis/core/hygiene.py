"""What `basis check --staged` adds, which the commit hook runs: the checks the files staged for a commit must pass
that no linter makes. A staged file holds no private key, is no larger than `LIMIT_KB`, and parses, if it is YAML or
TOML. Each file is read as it is staged, from the index the commit is made from, which `GIT_INDEX_FILE` names when
git commits with a temporary one, as `git commit -a` does."""

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

LIMIT_KB = 500
"""The most a staged file may hold, in KiB, as pre-commit's `check-added-large-files` allows by default: a fixture is a
few rows, and data larger than this belongs in the run store, not in every clone's history."""
PRIVATE_KEYS = (
    *(b"BEGIN " + kind + b"PRIVATE KEY" for kind in (b"RSA ", b"DSA ", b"EC ", b"OPENSSH ", b"", b"ENCRYPTED ", b"SSH2 ENCRYPTED ")),
    b"BEGIN PGP " + b"PRIVATE KEY BLOCK", b"PuTTY-User-" + b"Key-File-", b"BEGIN OpenVPN " + b"Static key V1",
)  # fmt: skip
"""The lines that begin a private key, as pre-commit's `detect-private-key` looks for them. Each is written in pieces,
so that no file of basis's own holds one."""
_YAML = (".yaml", ".yml")
_TOML = (".toml",)
_SUBMODULE = "160000"
_HEADER = re.compile(rb"^(?P<id>[0-9a-f]+) (?P<type>\w+) (?P<size>\d+)$")

type Problem = tuple[str, str]


@dataclass(frozen=True)
class Staged:
    name: str
    """Its path, relative to the repo."""
    blob: str
    """Its object id in the index."""


def problems(root: Path) -> list[Problem]:
    """Each problem of each file staged in the clone that holds the repo at `root`, with its fix."""
    git = shutil.which("git")
    if git is None:
        return [("git is not on the PATH, so the staged files cannot be read", "install git, and put it on the PATH")]
    return _checked(git, root, _staged(git, root))


def pushing(root: Path, commit: str) -> list[Problem]:
    """Each problem of each file that the commits up to `commit` no remote holds yet add or change, as the push hook's
    `basis check --full` reads them: what a commit made with `git commit --no-verify` skipped, a key since deleted
    among it, since the history holds it."""
    git = shutil.which("git")
    if git is None:
        return [("git is not on the PATH, so the commits being pushed cannot be read", "install git, and put it on the PATH")]
    argv = [git, "log", "--raw", "-z", "--no-renames", "--no-abbrev", "--relative", "--format=", "--diff-filter=AM", commit, "--not", "--remotes"]
    out = subprocess.run(argv, cwd=root, capture_output=True, check=True).stdout
    files: dict[str, Staged] = {}
    fields = out.split(b"\0")
    index = 0
    while index < len(fields) - 1:
        header = fields[index].strip(b"\n")
        if not header.startswith(b":"):
            index += 1
            continue
        _, mode, _, blob, _ = header.decode().split(" ")
        name = fields[index + 1].decode()
        if mode != _SUBMODULE:
            files.setdefault(blob, Staged(name, blob))
        index += 2
    found = _checked(git, root, list(files.values()))
    pushed = [(reason, _pushed_fix(fix)) for reason, fix in found]
    return list(dict.fromkeys(pushed))


def _pushed_fix(fix: str) -> str:
    """A staged file's fix, as it reads for a file in a commit being pushed, which unstaging no longer takes out."""
    found = re.match(r"unstage it with `git rm --cached (?P<name>[^`]+)`(?P<rest>.*)$", fix)
    if found is None:
        return fix
    name = found["name"]
    return f"take it out of every commit being pushed, as `git reset --soft @{{upstream}}`, `git rm --cached {name}` and committing again do{found['rest']}"


def _checked(git: str, root: Path, files: list[Staged]) -> list[Problem]:
    """Each problem of each of `files`, by its object in git."""
    sizes = _sizes(git, root, [file.blob for file in files])
    found: list[Problem] = []
    for file in files:
        if (size := int(sizes[file.blob])) > LIMIT_KB * 1024:
            unstage = f"unstage it with `git rm --cached {file.name}`"
            found.append((f"{file.name} is {size // 1024} KB, over the {LIMIT_KB} KB a staged file may be", f"{unstage}, and keep data that large in the run store, or a fixture of a few rows"))
    small = [file for file in files if int(sizes[file.blob]) <= LIMIT_KB * 1024]
    contents = dict(zip((file.blob for file in small), _contents(git, root, [file.blob for file in small]), strict=True))
    for file in small:
        found += _problems(file.name, contents[file.blob])
    return found


def _problems(name: str, content: bytes) -> list[Problem]:
    found: list[Problem] = []
    if any(key in content for key in PRIVATE_KEYS):
        found.append((f"{name} holds a private key", f"unstage it with `git rm --cached {name}`, keep the key out of the repo, and replace it if it was ever pushed"))
    kind = "YAML" if name.endswith(_YAML) else "TOML" if name.endswith(_TOML) else None
    if kind is None:
        return found
    try:
        text = content.decode("utf-8")
        if kind == "YAML":
            for _ in yaml.compose_all(text, Loader=yaml.SafeLoader):  # parses each document; a tag needs no constructor
                pass
        else:
            tomllib.loads(text)
    except yaml.MarkedYAMLError as error:  # the problem, and where, without the parser's quote of the source
        where = error.problem_mark
        why = f"{error.problem}, at line {where.line + 1}, column {where.column + 1}" if where is not None else str(error.problem)
        found.append((f"{name} is not {kind} ({why})", "fix it, and stage it again"))
    except (UnicodeDecodeError, yaml.YAMLError, tomllib.TOMLDecodeError) as error:
        why = " ".join(str(error).split())
        found.append((f"{name} is not {kind} ({why})", "fix it, and stage it again"))
    return found


def _staged(git: str, root: Path) -> list[Staged]:
    """Each file added, copied or modified in the index, against HEAD, with its object id in the index. A submodule
    is a commit, not a file, and is left out."""
    argv = [git, "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames", "--relative", "--diff-filter=ACM"]
    out = subprocess.run(argv, cwd=root, capture_output=True, check=True).stdout  # in git's environment, whose GIT_INDEX_FILE is the commit's index
    fields = out.split(b"\0")
    found: list[Staged] = []
    for header, name in zip(fields[0::2], fields[1::2], strict=False):
        _, mode, _, blob, _ = header.decode().split(" ")  # ":OLDMODE NEWMODE OLDID NEWID STATUS"
        if mode != _SUBMODULE:
            found.append(Staged(name.decode(), blob))
    return found


def _sizes(git: str, root: Path, blobs: list[str]) -> dict[str, str]:
    """Each object's size, by id, as `git cat-file --batch-check` gives it."""
    out = subprocess.run([git, "cat-file", "--batch-check"], cwd=root, input="".join(f"{blob}\n" for blob in blobs).encode(), capture_output=True, check=True).stdout
    return {match["id"].decode(): match["size"].decode() for line in out.splitlines() if (match := _HEADER.match(line))}


def _contents(git: str, root: Path, blobs: list[str]) -> list[bytes]:
    """Each object's content, in order, from one `git cat-file --batch`."""
    out = subprocess.run([git, "cat-file", "--batch"], cwd=root, input="".join(f"{blob}\n" for blob in blobs).encode(), capture_output=True, check=True).stdout
    found: list[bytes] = []
    at = 0
    for _ in blobs:
        end = out.index(b"\n", at)
        header = _HEADER.match(out[at:end])
        if header is None:
            msg = f"git cat-file --batch gave {out[at:end]!r}, not an object's header"
            raise ValueError(msg)
        size = int(header["size"])
        found.append(out[end + 1 : end + 1 + size])
        at = end + 1 + size + 1  # the content, then a newline
    return found
