"""Where each incremental query of a job starts (`etl.Watermark`): the highest value its column held in the rows the
job's newest run read, of the runs that published or began to, so a run that only checked, or failed before it wrote,
moves nothing, and a run that stopped partway, which `basis resume` finishes, is not read again. Before any, the
watermark's `initial`. The run store is its only state, as each run's manifest records what its query bound and the
highest value it read (`snapshots.WatermarkRecord`)."""

from basis.core import snapshots
from basis.core.storage import Root
from basis.job.inputs import Inputs, Query

WRITTEN = frozenset({"published", "partial", "publishing"})
"""The runs whose rows were written, or will be once resumed: those a watermark moves on from."""


def since(root: Root, job: str, inputs: type[Inputs], *, package: str) -> dict[str, str]:
    """The text of each watermarked query's watermark, by its dataset's name: the newest written run's `through`,
    or its `initial` where no run has one."""
    marked = {name: dataset for name, dataset in inputs.declared().items() if isinstance(dataset, Query) and dataset.watermark is not None}
    found: dict[str, str] = {}
    if not marked:
        return found
    for snapshot in snapshots.runs_of(root, job, package=package):
        record = snapshot.record()
        if record.status not in WRITTEN or record.replay_of is not None:
            continue
        for name in marked.keys() - found.keys():
            entry = record.inputs.get(name)
            if entry is not None and entry.watermark is not None:
                found[name] = entry.watermark.through
        if found.keys() == marked.keys():
            break
    return {name: found.get(name, dataset.watermark.initial if dataset.watermark is not None else "") for name, dataset in marked.items()}
