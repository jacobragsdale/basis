"""Running a model from Python, as a model's `main.py` does: `run`, `replay` and `sweep`, which `optim` exports. Each
takes the model's module first, `from models.income import model`, and runs as `optim run`, `optim replay` and
`optim sweep` do, through `optim.runs`, with four differences that suit an analyst's machine:

- A relative path is the model's folder's, wherever the IDE runs the file from, and `env` is found in the model's folder
  or a folder above it, up to the repo's root.
- The settings file's variables are set for the call alone and restored after it, so calls with different settings
  files do not mix, and logs are text unless the settings say otherwise.
- The snapshot is saved in the repo's `snapshots/`, unless `snapshots` says elsewhere; delete the folder whenever you
  like.
- No orders are written, whatever the settings file configures: only `optim run` writes them.

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

from optim import logs, models, research
from optim.context import open_context
from optim.models import LoadedModel
from optim.runs import NO_LABELS, Run, datasets_of, fixture_date, open_snapshot, replay_run, run_model, today
from optim.settings import Settings
from optim.snapshots import read_info
from optim.storage import Root


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
    """Decide portfolios with `model`, a model's `model.py` module, save the run, print what `optim run` prints, and
    return the run, whose `decisions`, `trades`, `reasons` and `metrics` are tables.

    - `portfolios`: None decides every portfolio the model lists, from `data` where it has the file; for a model that
      lists none, every portfolio in the holdings file in `data`.
    - `as_of`: a date or `YYYY-MM-DD`; None is the fixture's date with `data`, or else today in New York.
    - `env`: the settings file, which says where each dataset's source is; None reads the process's environment alone.
    - `data`: a folder whose files, `NAME.csv` or `NAME.parquet`, replace those datasets' sources, such as
      `tests/fixture`.
    - `workers`: processes that decide; None is `OPTIM_WORKERS`, or 1, which decides in this process.
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
                workers=workers or settings.workers,
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
    print what `optim replay` prints, and return it. `run` is a run's id, in the repo's `snapshots/` or under the
    settings file's `OPTIM_SNAPSHOT_ROOT`, such as production's, or a snapshot's path or URL. `portfolios` decides
    those alone; for a model that reserves, the portfolios before them in its order decide again too."""
    loaded = _loaded(model)
    root = _snapshot_root(loaded.directory, snapshots)
    with _environment(_settings_file(loaded.directory, env)):
        settings = Settings()
        snapshot = _saved(loaded, run, root, settings)
        ran = replay_run(snapshot, root, params_file=loaded.directory / params if params else None, portfolio_ids=portfolios, workers=workers or settings.workers, labels=labels)
    sys.stdout.write(ran.summary())
    return ran


def sweep(model: ModuleType, run: str, *, params: Sequence[str | Path], env: str | Path | None = ".env", workers: int | None = None, snapshots: str | Path | None = None) -> pd.DataFrame:
    """Decide a saved run of `model` once per params file, saving nothing, and return one table, as `optim sweep`
    writes it: a row per params file and portfolio, with whether it was accepted, its objective, turnover, trades,
    cash, solve time and metrics, and why it was rejected. `run` is found as `replay` finds it."""
    loaded = _loaded(model)
    with _environment(_settings_file(loaded.directory, env)):
        settings = Settings()
        snapshot = open_snapshot(_saved(loaded, run, _snapshot_root(loaded.directory, snapshots), settings))
        return research.sweep(snapshot, [loaded.directory / path for path in params], workers=workers or settings.workers)


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
    os.environ.setdefault("OPTIM_LOG_FORMAT", "text")
    logs.setup()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _snapshot_root(folder: Path, snapshots: str | Path | None) -> Root:
    """`snapshots`, or else the repo's `snapshots/`: the folder beside the nearest `pyproject.toml` above the model's."""
    if snapshots is not None:
        return Root.at(snapshots if "://" in str(snapshots) else folder / snapshots)
    repo = next((directory for directory in (folder, *folder.parents) if (directory / "pyproject.toml").exists()), Path.cwd())
    return Root.at(repo / "snapshots")


def _saved(model: LoadedModel, run: str, local: Root, settings: Settings) -> Root:
    """The snapshot `run` names, which must be of `model`: a path or URL, or a run's id in `local` or under
    `OPTIM_SNAPSHOT_ROOT`."""
    if "/" in run or "\\" in run:
        snapshot = Root.at(run if "://" in run else model.directory / run)
    else:
        roots = [local, *([Root.at(settings.snapshot_root)] if settings.snapshot_root else [])]
        if (snapshot := next((root / run for root in roots if (root / run / "run.json").exists()), None)) is None:
            msg = f"no run {run} in {' or '.join(root.location for root in roots)}: is OPTIM_SNAPSHOT_ROOT set in the settings file?"
            raise LookupError(msg)
    if (saved := read_info(snapshot).model).module != model.module:
        msg = f"{snapshot.location} is a run of {saved.name} ({saved.module}), not of {model.name}"
        raise ValueError(msg)
    return snapshot
