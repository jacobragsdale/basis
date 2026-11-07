"""The dataset cache: each entry a Parquet file under `OPTIM_CACHE_ROOT`, with the time its source was read in the file's
metadata, so a run can tell how old an entry is and record it. A laptop points the root at a local folder, and
production at shared storage, so every run shares the entries. Without the setting, nothing is cached. Parquet keeps
dtypes such as ordered categories and dates. `Cache` is the interface, so another backend can sit behind it if files
prove too slow for some dataset."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd

from optim.storage import Root

_FETCHED_AT = b"optim.fetched_at"


class Cache(Protocol):
    def get(self, key: str) -> tuple[pd.DataFrame, datetime] | None:
        """The frame, and when its source was read; None when there is no entry."""
        ...

    def put(self, key: str, frame: pd.DataFrame, fetched_at: datetime) -> None: ...

    def clear(self, dataset: str) -> None:
        """Delete every entry of one dataset, whatever its params."""
        ...


@dataclass(frozen=True)
class FileCache:
    """Keys are `<dataset>/<digest>`: one directory per dataset."""

    root: Root

    def get(self, key: str) -> tuple[pd.DataFrame, datetime] | None:
        entry = self.root / f"{key}.parquet"
        if not entry.exists():
            return None
        frame, metadata = entry.read_frame()
        if _FETCHED_AT not in metadata:
            msg = f"{entry.location} has no fetch time: it is not a cache entry"
            raise ValueError(msg)
        return frame, datetime.fromisoformat(metadata[_FETCHED_AT].decode())

    def put(self, key: str, frame: pd.DataFrame, fetched_at: datetime) -> None:
        (self.root / f"{key}.parquet").write_frame(frame, {_FETCHED_AT: fetched_at.isoformat().encode()})

    def clear(self, dataset: str) -> None:
        if (entries := self.root / dataset).exists():
            entries.delete_dir()
