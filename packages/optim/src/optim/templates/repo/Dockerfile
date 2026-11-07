# syntax=docker/dockerfile:1
# The image Airflow runs: `optim run MODEL --params models/MODEL/params/default.json --as-of DATE`, with the portfolios
# the model lists, or `--portfolios ...`. Build it with the commit, which every run's snapshot records:
# docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv
# SQL Server's driver, mssql-python, bundles its ODBC driver, which links against these. tini runs as the container's
# first process and passes the scheduler's SIGTERM on to optim, which stops the run: the first process ignores a
# signal's default action, so optim run as the first process would log the stop and carry on.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home optim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change rebuilds only the last layers. An internal index needs its credentials here,
# as a build secret, such as --mount=type=secret,id=uv_index,env=UV_INDEX_INTERNAL_PASSWORD.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
ARG GIT_SHA
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 OPTIM_GIT_SHA=$GIT_SHA
USER 10001
ENTRYPOINT ["tini", "--", "optim"]
CMD ["--help"]
