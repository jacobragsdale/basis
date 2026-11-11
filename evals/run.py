"""basis's eval, from the workspace's root:

    uv run evals/run.py validate                  # each task's untouched repo fails its grade, and its reference passes
    uv run evals/run.py trials                    # three trials of each task with Claude Code's sonnet, two at a time
    uv run evals/run.py trials --models haiku     # a second column
    uv run evals/run.py report                    # evals/results/report.md from the recorded trials

`evals/README.md` says what each does and how a trial is graded."""

import sys

from basis_eval import cli

if __name__ == "__main__":
    sys.exit(cli.main())
