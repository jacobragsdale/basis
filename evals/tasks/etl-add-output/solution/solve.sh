#!/bin/bash
# The reference solution: the row, the output, its frame from the transform, and a test of it.
set -euo pipefail
cp -r "$(dirname "$0")/files/." "${EVAL_APP:-/app}/"
