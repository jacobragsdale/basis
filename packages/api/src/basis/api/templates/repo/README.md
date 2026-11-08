# {repo}

An API, `{app}`, and the jobs beside it, on basis. Its code is in `{package}/`, in its layers: `service.py`, the
routes; `handlers.py`, one function per route; `repository.py`, every SQL statement and call to another service;
`domain.py`, the pure types and rules; `resources.py` and `errors.py`, what it declares. Its jobs are in `jobs/`.

```sh
uv run basis dev          # the API on its fakes, on this machine
uv run basis check        # the framework files, the hooks, the lockfile, ruff, ty, the gates and pytest
uv run basis new job NAME # a job, copied from basis-job's example
uv run basis docs         # basis's docs for this repo's parts
```
