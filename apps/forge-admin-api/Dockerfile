# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-admin-api/Dockerfile .
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# The workspace, at its repository paths. uv checks the lockfile against every
# member's pyproject, so they all come; only this app and what it depends on
# are installed (--package).
WORKDIR /src
# Third-party dependencies first, so source changes reuse this layer.
COPY pyproject.toml uv.lock ./
COPY apps/forge-admin-api/pyproject.toml apps/forge-admin-api/
COPY apps/forge-async-worker/pyproject.toml apps/forge-async-worker/
COPY packages/python/adk-workflows/pyproject.toml packages/python/adk-workflows/
COPY packages/python/agent-runtime/pyproject.toml packages/python/agent-runtime/
COPY packages/python/common/pyproject.toml packages/python/common/
COPY packages/python/documents/pyproject.toml packages/python/documents/
COPY packages/python/embeddings/pyproject.toml packages/python/embeddings/
COPY packages/python/jsonata/pyproject.toml packages/python/jsonata/
COPY packages/python/task-sdk/pyproject.toml packages/python/task-sdk/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package forge-admin --no-install-workspace
COPY packages/python/adk-workflows/ packages/python/adk-workflows/
COPY packages/python/agent-runtime/ packages/python/agent-runtime/
COPY packages/python/common/ packages/python/common/
COPY packages/python/documents/ packages/python/documents/
COPY packages/python/embeddings/ packages/python/embeddings/
COPY packages/python/jsonata/ packages/python/jsonata/
COPY packages/python/task-sdk/ packages/python/task-sdk/
COPY apps/forge-admin-api/ apps/forge-admin-api/
# Installs the app itself (with its migrations) and the local packages into
# the environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package forge-admin --no-editable
# The wheels a standalone agent's project carries in vendor/ (the starter;
# FORGE_ADMIN_STARTER_WHEELS), until the runtime is on PyPI.
RUN for package in forge-agent-runtime forge-common forge-jsonata; do \
        uv build --package "$package" --wheel --out-dir /app/starter-wheels || exit 1; \
    done

FROM python:3.13-slim-bookworm AS runtime
RUN groupadd --gid 10001 forge \
    && useradd --uid 10001 --gid forge --no-create-home forge
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/starter-wheels /app/starter-wheels
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
    FORGE_ADMIN_PORT=8091 \
    FORGE_ADMIN_STARTER_WHEELS=/app/starter-wheels
EXPOSE 8091
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health/ready', timeout=2)"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["forge-admin"]
