"""The firm's environments, `dev`, `qa` and `prod`, and where each one's settings are. Every command that touches the
firm's systems names one, as `--env qa` or `BASIS_ENV=qa`, and none has a default, so no command reaches an environment
nobody named. Its settings come from one place:

- on a desktop, the user's file for it, `basis/ENV.env` in their own configuration folder, beside `desktop.toml`:
  `%APPDATA%\\basis\\qa.env` on Windows, and `$XDG_CONFIG_HOME/basis/qa.env`, or `~/.config/basis/qa.env`, in WSL and
  on Linux. It holds the environment's hosts, paths and secrets, as `BASIS_*` lines, every repo the user works in
  shares it, and it is read alone, never with the process's environment, so a variable set for another environment
  never mixes in. It says which environment it is, `BASIS_ENV=qa`, so a copy of another's is refused, and each value
  is read as written (`basis.core.settings.read_values`);
- in a pod, which has no such file, the process's environment, which its deployment sets, `BASIS_ENV` among it,
  which must name the environment, so a shell's leftover `BASIS_*` variables are never taken for one.

Prod is read-only unless the process's environment sets `BASIS_PROD_WRITES=1`: without it, a run whose plan publishes
or writes anything is refused at the start (`Environment.refuse_writes`). A deployment sets it, and a user's file
cannot, though anyone can set a variable, so it stops accidents, not malice: the real control is a read-only prod login
for desktops. Desktops write in dev and QA."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from basis.core import settings, vault
from basis.core.errors import BasisError
from basis.core.settings import Settings

type Env = Literal["dev", "qa", "prod"]
NAMES: tuple[Env, ...] = ("dev", "qa", "prod")
VARIABLE = "BASIS_ENV"
"""The environment a command names when it is given no `--env`: a pod's, which its deployment sets."""
PROD_WRITES = "BASIS_PROD_WRITES"
"""Set to `1` by a deployment whose runs write to prod; read from the process's environment alone."""
_IGNORED = frozenset({VARIABLE, PROD_WRITES})
"""`BASIS_*` variables that are not settings, and that a pod's environment sets beside them."""

log = logging.getLogger(__name__)


class EnvError(BasisError, ValueError):
    """A command that names no environment, or one it may not act in as asked; the message gives the fix."""


def chosen(given: str | None, environ: Mapping[str, str]) -> Env:
    """The environment `given`, as `--env` names it, or else `BASIS_ENV`; refused when neither names one, or they
    differ."""
    named = environ.get(VARIABLE) or None
    if given is not None and named is not None and given != named:
        msg = f"--env {given} and {VARIABLE}={named} name different environments: give one, or make them agree"
        raise EnvError(msg)
    env = given if given is not None else named
    if env is None:
        msg = f"this command reaches the firm's systems, and names no environment, which basis never assumes: give --env {'|'.join(NAMES)}, or set {VARIABLE}"
        raise EnvError(msg)
    for name in NAMES:
        if env == name:
            return name
    msg = f"{env!r} is not an environment: name one of {', '.join(NAMES)}, with --env or {VARIABLE}"
    raise EnvError(msg)


def folder(environ: Mapping[str, str], *, windows: bool, home: Path) -> Path:
    """The user's own basis folder, which holds each environment's file and `desktop.toml`."""
    if windows:
        if (appdata := environ.get("APPDATA", "")) == "":
            msg = "APPDATA is not set, so basis cannot find your configuration folder, where each environment's settings are: run basis from your own Windows session"
            raise EnvError(msg)
        return Path(appdata) / "basis"
    config = Path(xdg) if (xdg := environ.get("XDG_CONFIG_HOME", "")) != "" else home / ".config"
    return config / "basis"


def user_file(env: Env, environ: Mapping[str, str] | None = None, *, windows: bool | None = None, home: Path | None = None) -> Path:
    """The user's file of `env`'s settings, which may not exist: a pod has none."""
    return folder(os.environ if environ is None else environ, windows=os.name == "nt" if windows is None else windows, home=Path.home() if home is None else home) / f"{env}.env"


@dataclass(frozen=True)
class Environment[S: Settings]:
    """An environment a command acts in, and its settings."""

    name: Env
    settings: S
    source: Path | None
    """The user's file the settings were read from, or None for the process's environment, as in a pod."""
    writes: bool
    """Whether a run may write: always outside prod, and in prod only where the process sets `BASIS_PROD_WRITES=1`."""

    def refuse_writes(self, what: str) -> None:
        """Refuse `what`, a run whose plan publishes or writes, such as `basis run positions --publish`, where it may
        not write."""
        if not self.writes:
            msg = (
                f"{what} would write to prod, which basis refuses unless the process sets {PROD_WRITES}=1, as a deployment that writes does: "
                "run it in dev or qa from a desktop, or in prod from its deployment"
            )
            raise EnvError(msg)


def load[S: Settings](given: str | None, kind: type[S], *, windows: bool | None = None, home: Path | None = None) -> Environment[S]:
    """The environment `given` names, as `--env` does, or `BASIS_ENV`, with its settings, as `kind`, core's `Settings`
    or a part's own: from the user's file for it, alone, where there is one, and from the process's environment
    otherwise. `windows` and `home` are the machine's unless a test gives them."""
    environ = os.environ
    env = chosen(given, environ)
    writes = env != "prod" or environ.get(PROD_WRITES) == "1"
    path = user_file(env, environ, windows=windows, home=home)
    if path.is_file():
        if ignored := _settings_variables(environ, kind):
            log.warning("reading %s alone, and not the process's %s", path, ", ".join(ignored), extra={"settings": str(path), "ignored": ignored})
        declared = settings.read_values(path).get(VARIABLE)
        if declared is None:
            msg = f"{path} does not say which environment it is: add the line {VARIABLE}={env} to it, so a copy of another environment's settings is never taken for {env}'s"
            raise EnvError(msg)
        if declared != env:
            msg = f"{path} says {VARIABLE}={declared}, as a copy of {declared}'s settings would: give {env} its own settings, with {VARIABLE}={env}"
            raise EnvError(msg)
        return Environment(env, vault.resolved(settings.read_file(kind, path)), path, writes)
    settings.refuse_renamed(environ)
    if not _settings_variables(environ, kind):
        msg = f"there is no {path}, and the process's environment sets no BASIS_ settings, so nothing configures {env}: write {path}, with the names the repo's .env.example lists"
        raise EnvError(msg)
    if environ.get(VARIABLE) != env:  # a shell's leftovers, which a deployment's settings never are: it names its environment
        msg = f"the process's BASIS_ settings do not say which environment they are: set {VARIABLE}={env} with them, as a deployment does, or write {path}"
        raise EnvError(msg)
    return Environment(env, vault.resolved(kind()), None, writes)


def _settings_variables(environ: Mapping[str, str], kind: type[Settings]) -> list[str]:
    """The `BASIS_*` variables `environ` sets that set one of `kind`'s settings, by name."""
    prefixes = tuple(f"{settings.PREFIX}{name.upper()}" for name in kind.model_fields)
    return sorted(name for name in environ if name.startswith(prefixes) and name not in _IGNORED)
