`/legacy/daily_cash.py` is a nightly script we want to retire. Port it into this repo as a job named `daily_cash` that does what the script does. Call its input `movements` and its output `daily_cash`, and its databases `ops` and `warehouse`. Keep its query a plain read of the movements, as the script's is: the script's filtering, totals and checks belong in the job.

When you're done, the repo's checks should pass.
