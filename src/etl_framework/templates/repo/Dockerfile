# syntax=docker/dockerfile:1
# The image a scheduler runs: `etl run JOB --params jobs/JOB/params/default.json --interval START..END --publish`.
# Build it with the commit, which every run's snapshot records: docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .

# The build stage installs the dependencies. git is here, and not in the image, for an etl-framework taken from git.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
RUN apt-get update && apt-get install --yes --no-install-recommends git && rm -rf /var/lib/apt/lists/*
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

# The image. SQL Server's driver, mssql-python, bundles its ODBC driver, which links against these. tini is the
# container's first process and passes a stop signal on to etl: the first process ignores a signal's default action,
# so etl run as the first process would carry on past a stop. A stopped run commits nothing: its transactions roll back.
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home etl
WORKDIR /app
COPY --from=build /app /app
ARG GIT_SHA
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 ETL_GIT_SHA=$GIT_SHA
USER 10001
ENTRYPOINT ["tini", "--", "etl"]
CMD ["--help"]
