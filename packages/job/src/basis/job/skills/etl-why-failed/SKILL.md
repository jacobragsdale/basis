---
name: etl-why-failed
description: Find out why a basis-job run failed, refused to publish, or published partly, from its manifest and logs. Use when the user asks why a job failed, or what a run did.
---

# Why a run failed

1. Find the run: its id is in every log line (`run_id`). Its manifest is `ETL_SNAPSHOT_ROOT/runs/job/JOB/RUN_ID.json`,
   or the repo's `snapshots/runs/job/JOB/RUN_ID.json` for a run from Python.
2. Read the manifest. `status` says how it ended: `failed` (see `error` and `reasons`), `checked` (passed, not asked to
   publish), `published`, or `partial` (some databases committed; `etl resume JOB RUN_ID` finishes it).
3. Each entry in `reasons` names its output and what failed: a row check, a destination check, a rerun guard, or the
   job's own `check`. A transform error names the line of job code that raised.
4. To reproduce on a laptop, replay it: `uv run etl replay JOB RUN_ID`, or `replay(job, RUN_ID, env=".env.prod")`
   from `main.py`. A replay reads only the snapshot and writes nothing to a database.
5. After a fix, replay again and compare; `uv run etl regress JOB --last 10` shows what the fix changes in recent
   published runs.
