# {repo}

A Dash app, `{app}`, on basis. Its code is in `{package}/`, in its layers: `app.py`, the declaration; `pages/`, one
module per page, its layout and its callbacks; `data.py`, the rows, the databases and every dataset the pages load;
`domain.py`, the pure transforms its views run; `dev.py`, the fakes `basis dev` serves it on, from `fixtures/`.

```sh
uv run basis dev                    # the app on its fakes, on this machine, at http://127.0.0.1:8000
uv run basis render /               # what the first page shows, as JSON
uv run basis load holdings --param as_of=2026-11-12   # a dataset's first rows and timings
uv run basis new page risk          # a page, and its test
uv run basis check                  # the framework files, the hooks, the lockfile, ruff, ty, the gates, pytest and the app's checks
uv run basis docs                   # basis's docs for this repo's parts
```

## Environments

Every command that reaches the firm's systems names its environment, `--env dev`, `qa` or `prod`, and reads your own
settings for it: `~/.config/basis/ENV.env`, or `%APPDATA%\basis\ENV.env` on Windows, with the names `.env.example`
lists. In a pod, the deployment sets them as the process's environment, `BASIS_ENV` among them. The image's command is
`serve`; `basis smoke --env ENV` checks a deploy.
