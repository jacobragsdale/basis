"""Running a model from Python, as a model's `main.py` does: `run`, `replay` and `sweep`, which `optim` exports. Each
takes the model's module first, `from models.income import model`, and runs as `basis run`, `basis replay` and
`basis sweep` do, through `optim.runs`, with four differences that suit an analyst's machine:

- A relative path is the model's folder's, wherever the IDE runs the file from, and `env` is found in the model's folder
  or a folder above it, up to the repo's root.
- The settings file's variables are set for the call alone and restored after it, so calls with different settings
  files do not mix, and logs are text unless the settings say otherwise.
- The run is saved in the repo's `snapshots/`, a run store of its own, unless `snapshots` says elsewhere; delete the
  folder whenever you like.
- No orders are written, whatever the settings file configures: only `basis run` writes them.

An error raises, with its traceback, so a debugger stops where it surfaced; a hook's error rejects its portfolio alone,
and its reason names the line of model code that raised."""

import os
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import ModuleType

import pandas as pd
from dotenv import dotenv_values

from basis.core import logs
from basis.core.storage import Root
from optim import models, research, snapshots
from optim.context import open_context
from optim.models import LoadedModel
from optim.runs import NO_LABELS, Run, datasets_of, fixture_date, read_snapshot, replay_run, run_model, today
from optim.settings import Settings, refuse_renamed
from optim.snapshots import Saved


def run(
    model: ModuleType,
    *,
    params: str | Path = "params/default.json",
    portfolios: Sequence[str] | None = None,
    as_of: date | str | None = None,
    env: str | Path | None = ".env",
    data: str | Path | None = None,
    workers: int | None = None,
    labels: Mapping[str, str] = NO_LABELS,
    snapshots: str | Path | None = None,
    cache: bool = True,
) -> Run:
    """Decide portfolios with `model`, a model's `model.py` module, save the run, print what `basis run` prints, and
    return the run, whose `decisions`, `trades`, `reasons` and `metrics` are tables.

    - `portfolios`: None decides every portfolio the model lists, from `data` where it has the file; for a model that
      lists none, every portfolio in the holdings file in `data`.
    - `as_of`: a date or `YYYY-MM-DD`; None is the fixture's date with `data`, or else today in New York.
    - `env`: the settings file, which says where each dataset's source is; None reads the process's environment alone.
    - `data`: a folder whose files, `NAME.csv` or `NAME.parquet`, replace those datasets' sources, such as
      `tests/fixture`.
    - `workers`: processes that decide; None is `BASIS_OPTIM__WORKERS`, or 1, which decides in this process.
    - `snapshots`: where to save the run, a path or a URL; None is the repo's `snapshots/`."""
    loaded = _loaded(model)
    data_dir = loaded.directory / data if data is not None else None
    settings_file = _settings_file(loaded.directory, env)
    if env is not None and settings_file is None and any(dataset.source != "file" for dataset in datasets_of(loaded, data_dir).values()):
        msg = f"there is no {env} in {loaded.directory} or a folder above it: copy .env.example to {env} and set the sources {loaded.name} reads"
        msg += ', or run on its fixture with data="tests/fixture"'
        raise FileNotFoundError(msg)
    as_of_date = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or fixture_date(data_dir) or today()
    with _environment(settings_file):
        settings = Settings()
        with open_context(settings, cache=cache, orders_table=False) as ctx:
            ran = run_model(
                ctx.loader,
                _snapshot_root(loaded.directory, snapshots),
                loaded,
                loaded.directory / params,
                as_of_date=as_of_date,
                portfolio_ids=portfolios,
                data_dir=data_dir,
                workers=workers or settings.optim.workers,
                labels=labels,
            )
    sys.stdout.write(ran.summary())
    return ran


def replay(
    model: ModuleType,
    run: str,
    *,
    params: str | Path | None = None,
    portfolios: Sequence[str] | None = None,
    env: str | Path | None = ".env",
    workers: int | None = None,
    labels: Mapping[str, str] = NO_LABELS,
    snapshots: str | Path | None = None,
) -> Run:
    """Decide a saved run of `model` again, with the code you have now and other params if given, save it as a new run,
    print what `basis replay` prints, and return it. `run` is a run's id, in the repo's `snapshots/` or under the
    settings file's `BASIS_SNAPSHOT_ROOT`, such as production's, or the path or URL of its manifest. `portfolios`
    decides those alone; for a model that reserves, the portfolios before them in its order decide again too."""
    loaded = _loaded(model)
    root = _snapshot_root(loaded.directory, snapshots)
    with _environment(_settings_file(loaded.directory, env)):
        settings = Settings()
        snapshot = _saved(loaded, run, root, settings)
        ran = replay_run(snapshot, root, params_file=loaded.directory / params if params is not None else None, portfolio_ids=portfolios, workers=workers or settings.optim.workers, labels=labels)
    sys.stdout.write(ran.summary())
    return ran


def sweep(model: ModuleType, run: str, *, params: Sequence[str | Path], env: str | Path | None = ".env", workers: int | None = None, snapshots: str | Path | None = None) -> pd.DataFrame:
    """Decide a saved run of `model` once per params file, saving nothing, and return one table, as `basis sweep`
    writes it: a row per params file and portfolio, with whether it was accepted, its objective, turnover, trades,
    cash, solve time and metrics, and why it was rejected. `run` is found as `replay` finds it."""
    loaded = _loaded(model)
    with _environment(_settings_file(loaded.directory, env)):
        settings = Settings()
        snapshot = read_snapshot(_saved(loaded, run, _snapshot_root(loaded.directory, snapshots), settings))
        return research.sweep(snapshot, [loaded.directory / path for path in params], workers=workers or settings.optim.workers)


def _loaded(model: ModuleType) -> LoadedModel:
    return models.read(models.name_of(model), model)


def _settings_file(folder: Path, env: str | Path | None) -> Path | None:
    """`env` in the model's folder, or the nearest folder above it that has it, up to the repo's root; None where none
    does."""
    if env is None:
        return None
    for directory in (folder, *folder.parents):
        if (file := directory / env).exists():
            return file
        if (directory / "pyproject.toml").exists():
            break
    return None


@contextmanager
def _environment(settings_file: Path | None) -> Generator[None]:
    """The settings file's variables set in this process, as `uv run --env-file` sets them, until the block ends, and
    the environment as it was after it. Worker processes the run starts inherit them."""
    saved = dict(os.environ)
    os.environ.update({key: value for key, value in (dotenv_values(settings_file) if settings_file else {}).items() if value is not None})
    os.environ.setdefault("BASIS_LOG_FORMAT", "text")  # set, rather than setup's default, so the run's worker processes read it too
    try:
        logs.include("optim")
        logs.setup()
        refuse_renamed(os.environ)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _snapshot_root(folder: Path, snapshots: str | Path | None) -> Root:
    """The run store at `snapshots`, or else the repo's `snapshots/`: the folder beside the nearest `pyproject.toml`
    above the model's."""
    if snapshots is not None:
        return Root.at(snapshots if "://" in str(snapshots) else folder / snapshots)
    repo = next((directory for directory in (folder, *folder.parents) if (directory / "pyproject.toml").exists()), Path.cwd())
    return Root.at(repo / "snapshots")


def _saved(model: LoadedModel, run: str, local: Root, settings: Settings) -> Saved:
    """The run `run` names, which must be of `model`: its manifest's path or URL, or its id in `local` or under
    `BASIS_SNAPSHOT_ROOT`."""
    if "/" in run or "\\" in run:
        saved = snapshots.read(Root.at(run if "://" in run else model.directory / run))
    else:
        roots = [local, *([Root.at(settings.snapshot_root)] if settings.snapshot_root is not None else [])]
        try:
            saved = snapshots.find(roots, run)
        except LookupError as error:
            error.add_note("is BASIS_SNAPSHOT_ROOT set in the settings file?")
            raise
    if (of := saved.info.model).module != model.module:
        msg = f"run {saved.info.run_id} is a run of {of.name} ({of.module}), not of {model.name}"
        raise ValueError(msg)
    return saved
