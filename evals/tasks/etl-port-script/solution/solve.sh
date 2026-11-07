#!/bin/bash
# The reference solution: `etl new job`, then the example's files replaced by the job's own.
set -euo pipefail
solution=$(cd "$(dirname "$0")" && pwd)
cd "${EVAL_APP:-/app}"
uv run etl new job daily_cash
rm -r jobs/daily_cash/sql/* jobs/daily_cash/tests/fixture/*
cp -r "$solution/files/." .
