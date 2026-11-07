#!/bin/bash
# Grades one trial, in the task's container after the agent stops. It runs the job repo's own checks, then the task's
# assertions in test_task.py, then checks that the files protected.txt names are as the task left them. Every check
# runs; the reward is 1 only when every one passes. Each check's output is in /logs/verifier/NAME.txt, and its exit
# status and milliseconds in /logs/verifier/checks.tsv.
#
# This file is the same in every task: change them all together.
set -u
app=${EVAL_APP:-/app}
tests=${EVAL_TESTS:-/tests}
logs=${EVAL_LOGS:-/logs/verifier}
mkdir -p "$logs"
: > "$logs/checks.tsv"
cd "$app" || exit 1
export UV_NO_PROGRESS=1 NO_COLOR=1

declare -A passed
check() {  # NAME COMMAND...
  local name=$1 start status
  shift
  start=$(date +%s%N)
  "$@" > "$logs/$name.txt" 2>&1
  status=$?
  printf '%s\t%d\t%d\n' "$name" "$status" $((($(date +%s%N) - start) / 1000000)) >> "$logs/checks.tsv"
  passed[$name]=$((status == 0 ? 1 : 0))
}

untouched() {
  local paths
  mapfile -t paths < <(grep -v '^#' "$tests/protected.txt" | grep .)
  if ! git diff --quiet eval-base -- "${paths[@]}"; then
    echo "changed since the task began:"
    git diff --stat eval-base -- "${paths[@]}"
    return 1
  fi
  local added
  added=$(git ls-files --others --exclude-standard -- "${paths[@]}")
  if [ -n "$added" ]; then
    echo "added since the task began:"
    echo "$added"
    return 1
  fi
  # pyproject.toml's [tool] tables decide what ruff, ty and pytest check, so they stay as the repo was generated.
  git show eval-base:pyproject.toml | python3 -c '
import sys, tomllib
before = tomllib.loads(sys.stdin.read()).get("tool")
with open("pyproject.toml", "rb") as file:
    after = tomllib.load(file).get("tool")
if before != after:
    sys.exit("pyproject.toml: the [tool] tables changed since the task began")
'
}

# The repo's own checks, as its AGENTS.md, skills and pre-commit config give them. `uv sync --locked` fails when
# pyproject.toml changed without uv.lock, and installs the project again, so new entry points are listed.
check lock uv sync --locked
check pytest uv run --no-sync pytest -q -p no:cacheprovider
check ruff_check uv run --no-sync ruff check --no-cache .
check ruff_format uv run --no-sync ruff format --check --no-cache .
check ty uv run --no-sync ty check
# This task's own assertions, run with the repo's pytest settings.
check task uv run --no-sync pytest -q -p no:cacheprovider -c pyproject.toml --rootdir . "$tests/test_task.py"
check untouched untouched

reward=1
json=""
for name in lock pytest ruff_check ruff_format ty task untouched; do
  [ "${passed[$name]}" -eq 1 ] || reward=0
  json+=", \"$name\": ${passed[$name]}"
done
echo "{\"reward\": $reward$json}" > "$logs/reward.json"
column -t "$logs/checks.tsv" 2>/dev/null || cat "$logs/checks.tsv"
echo "reward $reward"
