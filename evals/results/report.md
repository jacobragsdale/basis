# Eval report

No agent trials have run yet: `uv run evals/run.py trials`.

## Task validation

| Task | nop (must fail) | oracle (must pass) | Oracle's check suite |
|---|---|---|---|
| etl-add-output | 0 (task failed) | 1 | 1.5s |
| etl-add-param | 0 (task failed) | 1 | 1.5s |
| etl-fix-check | 0 (pytest, task failed) | 1 | 2.3s |
| etl-new-job | 0 (task failed) | 1 | 1.0s |
| etl-port-script | 0 (task failed) | 1 | 1.0s |
