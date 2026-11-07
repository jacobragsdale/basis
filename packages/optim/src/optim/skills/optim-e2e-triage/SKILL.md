---
name: optim-e2e-triage
description: Triage a failing or refused optim e2e run against QA, and tell a model bug from data drift, a QA outage or an unsafe environment. Use when the e2e suite, a QA run or a smoke case fails, errors or refuses to run.
---

# Triage an e2e failure

Follow the triage section of `uv run optim docs how-to/run-the-qa-suite`. In short, sort the failure first:

1. **Refused to run**: the `optim_qa` fixture found a source or destination without the QA marker, or a write outside
   `optim_e2e_writes`. Fix the environment, never the safeguard.
2. **The run failed**: read its JSON logs, filtered on its `run_id`; the failing record names the root cause and
   where it surfaced. A source that timed out or refused a login is a QA outage.
3. **A portfolio was rejected**: its reasons are in the snapshot's `reasons.parquet`, and a hook's error names the line
   of model code that raised. Follow the `optim-why-rejected` skill to replay it and tell the data from the model.
4. **The replay differs**: a solve that stopped at its time limit may stop elsewhere; any other difference means
   the model depends on something outside its snapshot.

Report the run id, the portfolios and the reasons. Never weaken an assertion or a check to make the suite pass.
