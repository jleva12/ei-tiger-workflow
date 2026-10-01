# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-async-worker/Dockerfile .
# The SAQ worker, with the task packages it bundles (the workflows and
# adk-workflows extras):
# every enabled task type's queue and schedules by default
# (forge-async-worker worker --help); a deployment picks its queues with --queues.
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
COPY packages/python/enhanced-task-framework/pyproject.toml /src/packages/python/enhanced-task-framework/
COPY packages/python/tasks/task-sdk/pyproject.toml /src/packages/python/tasks/task-sdk/
COPY packages/python/tasks/workflows/pyproject.toml /src/packages/python/tasks/workflows/
COPY packages/python/tasks/adk-workflows/pyproject.toml /src/packages/python/tasks/adk-workflows/
COPY packages/python/jsonata/pyproject.toml /src/packages/python/jsonata/
COPY packages/python/common/pyproject.toml /src/packages/python/common/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-install-local \
      --extra workflows --extra adk-workflows
COPY packages/python/enhanced-task-framework/ /src/packages/python/enhanced-task-framework/
COPY packages/python/jsonata/ /src/packages/python/jsonata/
COPY packages/python/common/ /src/packages/python/common/
COPY packages/python/tasks/ /src/packages/python/tasks/
COPY apps/forge-async-worker/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra workflows --extra adk-workflows
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
# Agent steps' and LLM nodes' model-provider YAMLs, where
# HYBRID_WORKFLOWS__MODEL_PROVIDER_CONFIG's and HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG's
# relative path (../../packages/...) finds them from /app, as the admin API's image does.
COPY packages/python/common/src/forge_common/model_provider/*.yaml \
    /packages/python/common/src/forge_common/model_provider/
WORKDIR /app
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
STOPSIGNAL SIGTERM
CMD ["forge-async-worker", "worker"]
