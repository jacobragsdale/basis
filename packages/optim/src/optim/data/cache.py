"""The dataset cache: each entry a Parquet file under `BASIS_OPTIM__CACHE_ROOT`, with the time its source was read in the
file's metadata, so a run can tell how old an entry is and record it. A laptop points the root at a local folder, and
production at shared storage, so every run shares the entries. Without the setting, nothing is cached. Parquet keeps
dtypes such as ordered categories and dates. An entry is written whole under a temporary name and renamed into place,
through basis-core's storage, so no run reads half of one. `Cache` is the interface, so another backend can sit behind
it if files prove too slow for some dataset."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from basis.core.storage import Root

_FETCHED_AT = b"optim.fetched_at"


class Cache(Protocol):
    def get(self, key: str) -> tuple[pd.DataFrame, datetime] | None:
        """The frame, and when its source was read; None when there is no entry."""
        ...

    def put(self, key: str, frame: pd.DataFrame, fetched_at: datetime) -> None: ...


@dataclass(frozen=True)
class FileCache:
    """Keys are `<dataset>/<digest>`: one directory per dataset."""

    root: Root

    def get(self, key: str) -> tuple[pd.DataFrame, datetime] | None:
        entry = self.root / f"{key}.parquet"
        if not entry.exists():
            return None
        table = entry.read_table()
        metadata: dict[bytes, bytes] = dict(table.schema.metadata or {})
        if _FETCHED_AT not in metadata:
            msg = f"{entry.location} has no fetch time: it is not a cache entry"
            raise ValueError(msg)
        frame = cast("pd.DataFrame", table.to_pandas())  # pyarrow is untyped; as `DataFrame.to_parquet` wrote it, so its dtypes come back
        return frame, datetime.fromisoformat(metadata[_FETCHED_AT].decode())

    def put(self, key: str, frame: pd.DataFrame, fetched_at: datetime) -> None:
        table = pa.Table.from_pandas(frame, preserve_index=False)
        table = table.replace_schema_metadata((table.schema.metadata or {}) | {_FETCHED_AT: fetched_at.isoformat().encode()})
        written = pa.BufferOutputStream()
        pq.write_table(table, written)
        (self.root / f"{key}.parquet").write_bytes(written.getvalue().to_pybytes())
