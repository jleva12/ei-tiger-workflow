# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-codegraph-mcp/Dockerfile .
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
COPY apps/forge-codegraph-mcp/pyproject.toml apps/forge-codegraph-mcp/
COPY packages/python/adk-workflows/pyproject.toml packages/python/adk-workflows/
COPY packages/python/agent-runtime/pyproject.toml packages/python/agent-runtime/
COPY packages/python/codegraph/pyproject.toml packages/python/codegraph/
COPY packages/python/common/pyproject.toml packages/python/common/
COPY packages/python/documents/pyproject.toml packages/python/documents/
COPY packages/python/embeddings/pyproject.toml packages/python/embeddings/
COPY packages/python/jsonata/pyproject.toml packages/python/jsonata/
COPY packages/python/mcp-servers/pyproject.toml packages/python/mcp-servers/
COPY packages/python/task-sdk/pyproject.toml packages/python/task-sdk/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package forge-codegraph-mcp --no-install-workspace
COPY packages/python/common/ packages/python/common/
COPY apps/forge-codegraph-mcp/ apps/forge-codegraph-mcp/
# Installs the app itself and forge-common into the environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package forge-codegraph-mcp --no-editable

FROM python:3.13-slim-bookworm AS runtime
RUN groupadd --gid 10001 forge \
    && useradd --uid 10001 --gid forge --no-create-home forge
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CODEGRAPH_SERVER__HOST=0.0.0.0 \
    CODEGRAPH_SERVER__PORT=8103
EXPOSE 8103
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8103/health/ready', timeout=2)"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["forge-codegraph-mcp"]
