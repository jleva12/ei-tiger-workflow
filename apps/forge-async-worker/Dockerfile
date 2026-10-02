# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-async-worker/Dockerfile .
# The SAQ worker with the ADK workflows task: ADK workflow runs off the
# adk_workflows queue (forge-async-worker worker --help).
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# The app and the local packages it depends on, at their repository paths
# (the app's pyproject finds them by relative path).
WORKDIR /src/apps/forge-async-worker
# Third-party dependencies first, so source changes reuse this layer. uv checks
# the lockfile against every local package's pyproject, so those come too.
COPY apps/forge-async-worker/pyproject.toml apps/forge-async-worker/uv.lock ./
COPY packages/python/task-sdk/pyproject.toml /src/packages/python/task-sdk/
COPY packages/python/adk-workflows/pyproject.toml /src/packages/python/adk-workflows/
COPY packages/python/jsonata/pyproject.toml /src/packages/python/jsonata/
COPY packages/python/common/pyproject.toml /src/packages/python/common/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-install-local
COPY packages/python/jsonata/ /src/packages/python/jsonata/
COPY packages/python/common/ /src/packages/python/common/
COPY packages/python/task-sdk/ /src/packages/python/task-sdk/
COPY packages/python/adk-workflows/ /src/packages/python/adk-workflows/
COPY apps/forge-async-worker/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable
# What the bundled tasks would otherwise download at runtime: each task
# package's prepare().
RUN /app/.venv/bin/forge-async-worker prepare

FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 forge \
    && useradd --uid 10001 --gid forge --no-create-home forge
COPY --from=build /app/.venv /app/.venv
# LLM nodes' model-provider YAMLs, where HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG's
# relative path (../../packages/...) finds them from /app, as the admin API's image does.
COPY packages/python/common/src/forge_common/model_provider/*.yaml \
    /packages/python/common/src/forge_common/model_provider/
WORKDIR /app
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
STOPSIGNAL SIGTERM
CMD ["forge-async-worker", "worker"]
