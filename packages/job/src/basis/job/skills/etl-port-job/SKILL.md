---
name: etl-port-job
description: Port an existing ETL script or job onto basis-job, keeping its behaviour and proving it with a replay or a comparison of outputs. Use when the user wants to move a legacy job, script or notebook onto the framework.
---

# Port a job

Keep a `PORTING.md` in the job's folder as you go: what the old job read, wrote and checked, each decision, and each
difference you find.

1. Read the old job end to end. List every table it reads (and the exact filters), every table it writes and how
   (insert, delete-then-insert, merge, truncate), every check it makes, and every side effect.
2. Make the job with the `etl-new-job` skill's steps. Map each write to a mode: delete-then-insert by date is
   `ReplaceWhere(interval=...)`, truncate-and-load is `Replace()`, merge is `Upsert()`, insert-if-missing is
   `InsertNew()`, anything else is `Manual()` with `Changes`. A plain insert is `Append()`, which the ledger guards
   against a second publish of the same interval.
3. Move every query filter that depends on a parameter into the transform: queries bind only the interval.
4. Port the old job's checks into `check`, and add a reason for anything it used to log and carry on past.
5. Prove the port: run the old job and the new one (`uv run etl run NAME ... --data DIR` on the same inputs, or
   against QA) and compare the outputs row for row by key. Record every difference in `PORTING.md`, and resolve each
   with the user.
