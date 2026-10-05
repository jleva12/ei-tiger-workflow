@@if ui@@
# @@NAME@@: the UI built with Node, then served with the agent by Python.
@@end@@
@@if !ui@@
# @@NAME@@: the agent's API, served by Python.
@@end@@
#   docker build -t @@SLUG@@ .
#   docker run --env-file .env -p 8000:8000 @@SLUG@@
@@if ui@@

FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build
@@end@@

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock* ./
@@if wheels@@
COPY vendor/ vendor/
@@end@@
RUN uv sync --no-dev
COPY main.py model_provider.yaml ./
COPY agent/ agent/
@@if ui@@
COPY --from=web /web/dist web/dist
@@end@@
RUN useradd --create-home --uid 10001 agent
@@if data@@
# SQLite and the artifacts folder are in data/: mount a volume there to keep them.
RUN mkdir -p data && chown agent data
VOLUME /app/data
@@end@@
USER agent
ENV FORGE_AGENT_HOST=0.0.0.0 FORGE_AGENT_PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["/app/.venv/bin/python", "main.py"]
