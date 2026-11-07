# A job repo as `etl new repo` makes it from etl-framework v0.1.0, for a developer's agent to work in. The lines down
# to this task's own are the same in every task, so Docker builds them once for all of them.
FROM python:3.14.7-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates curl git && rm -rf /var/lib/apt/lists/*
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN git config --global user.name Developer && git config --global user.email developer@example.com
WORKDIR /app
# `etl new repo` runs git init and uv sync itself. The repo then moves to /app, so its environment is made again there.
RUN uvx --from git+https://github.com/jacobragsdale/etl-framework@v0.1.0 etl new repo trades-etl positions --dir /tmp/new \
    && cp -a /tmp/new/trades-etl/. /app/ && rm -rf /tmp/new /app/.venv \
    && grep -q dd2bab8821c4bf3c1cd684cc2d06c932bbac21a0 uv.lock \
    && uv sync --locked

# The fault: the check that no position falls after the interval compares with its start, not its end, so the job
# refuses its own fixture.
RUN sed -i 's/>= inputs.interval.end.date()).height/>= inputs.interval.start.date()).height/' jobs/positions/job.py \
    && grep -q 'interval.start.date()).height' jobs/positions/job.py
RUN git add --all && git commit --quiet --message trades-etl && git tag eval-base
