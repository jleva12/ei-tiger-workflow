# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-admin-api/Dockerfile .
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# The app and the local packages it depends on, at their repository paths (the
# app's pyproject finds them by relative path).
WORKDIR /src/apps/forge-admin-api
# Third-party dependencies first, so source changes reuse this layer. uv checks
# the lockfile against every local package's pyproject, so those come too.
COPY apps/forge-admin-api/pyproject.toml apps/forge-admin-api/uv.lock ./
COPY packages/python/common/pyproject.toml /src/packages/python/common/
COPY packages/python/tasks/adk-workflows/pyproject.toml /src/packages/python/tasks/adk-workflows/
COPY packages/python/tasks/task-sdk/pyproject.toml /src/packages/python/tasks/task-sdk/
COPY packages/python/jsonata/pyproject.toml /src/packages/python/jsonata/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-install-local
COPY packages/python/common/ /src/packages/python/common/
COPY packages/python/tasks/adk-workflows/ /src/packages/python/tasks/adk-workflows/
COPY packages/python/tasks/task-sdk/ /src/packages/python/tasks/task-sdk/
COPY packages/python/jsonata/ /src/packages/python/jsonata/
COPY apps/forge-admin-api/ ./
# Installs the package itself (with its migrations) and the local packages into
# the environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime
RUN groupadd --gid 10001 forge \
    && useradd --uid 10001 --gid forge --no-create-home forge
COPY --from=build /app/.venv /app/.venv
# The shared model-provider YAML, which ADK workflows' LLM nodes read too, at its
# repository path relative to /app: FORGE_ADMIN_MODEL_PROVIDER_CONFIG=../../packages/...
# names it here as it does natively from apps/forge-admin-api.
COPY packages/python/common/src/forge_common/model_provider/*.yaml \
    /packages/python/common/src/forge_common/model_provider/
WORKDIR /app
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FORGE_ADMIN_HOST=0.0.0.0 \
    FORGE_ADMIN_PORT=8091
EXPOSE 8091
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health/ready', timeout=2)"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["forge-admin"]
