#!/bin/bash
# The reference solution: the param, its use in the transform, and a test of it.
set -euo pipefail
cp -r "$(dirname "$0")/files/." "${EVAL_APP:-/app}/"
