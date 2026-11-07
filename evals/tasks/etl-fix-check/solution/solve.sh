#!/bin/bash
# The reference fix: compare positions with the interval's end, which it excludes.
set -euo pipefail
cd "${EVAL_APP:-/app}"
sed -i 's/>= inputs.interval.start.date()).height/>= inputs.interval.end.date()).height/' jobs/positions/job.py
grep -q 'interval.end.date()).height' jobs/positions/job.py
