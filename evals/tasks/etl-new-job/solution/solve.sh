#!/bin/bash
# The reference solution: `etl new job`, then the example's files replaced by the job's own.
set -euo pipefail
solution=$(cd "$(dirname "$0")" && pwd)
cd "${EVAL_APP:-/app}"
uv run etl new job security_master
rm -r jobs/security_master/sql/* jobs/security_master/tests/fixture/*
cp -r "$solution/files/." .
