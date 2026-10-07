# syntax=docker/dockerfile:1
# Build from the repository root so the shared packages can be copied:
#   docker build -f apps/forge-codegraph-worker/Dockerfile .
FROM maven:3.9.9-eclipse-temurin-21 AS java-tools
RUN cp -aL /opt/java/openjdk /opt/java/codegraph-jdk
FROM node:22-bookworm-slim AS python-analyzer
WORKDIR /analyzer
COPY apps/forge-codegraph-worker/python-analyzer/package.json apps/forge-codegraph-worker/python-analyzer/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/forge-codegraph-worker/python-analyzer/build.mjs ./
COPY apps/forge-codegraph-worker/python-analyzer/src ./src
RUN npm run build
FROM node:22-bookworm-slim AS typescript-analyzer
WORKDIR /analyzer
COPY apps/forge-codegraph-worker/typescript-analyzer/package.json apps/forge-codegraph-worker/typescript-analyzer/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/forge-codegraph-worker/typescript-analyzer/build.mjs ./
COPY apps/forge-codegraph-worker/typescript-analyzer/src ./src
RUN npm run build
FROM golang:1.25.5-bookworm AS build

WORKDIR /src/apps/forge-codegraph-worker
ENV CGO_ENABLED=1 GOTOOLCHAIN=local GOWORK=off
COPY apps/forge-codegraph-worker/go.mod apps/forge-codegraph-worker/go.sum ./
COPY packages/go/code-graph/domain/go.mod packages/go/code-graph/domain/go.sum /src/packages/go/code-graph/domain/
COPY packages/go/code-graph/authorization/go.mod packages/go/code-graph/authorization/go.sum /src/packages/go/code-graph/authorization/
COPY packages/go/code-graph/storage/go.mod packages/go/code-graph/storage/go.sum /src/packages/go/code-graph/storage/
COPY packages/go/code-graph/serviceconfig/go.mod packages/go/code-graph/serviceconfig/go.sum /src/packages/go/code-graph/serviceconfig/
COPY packages/go/code-graph/agentquery/go.mod packages/go/code-graph/agentquery/go.sum /src/packages/go/code-graph/agentquery/
# go.mod replaces the Java grammar with this audited local module.
COPY packages/go/code-graph/tree-sitter-java /src/packages/go/code-graph/tree-sitter-java
RUN go mod download
COPY apps/forge-codegraph-worker/ ./
COPY packages/go/code-graph/domain/ /src/packages/go/code-graph/domain/
COPY packages/go/code-graph/storage/ /src/packages/go/code-graph/storage/
COPY packages/go/code-graph/authorization/ /src/packages/go/code-graph/authorization/
COPY packages/go/code-graph/serviceconfig/ /src/packages/go/code-graph/serviceconfig/
COPY packages/go/code-graph/agentquery/ /src/packages/go/code-graph/agentquery/
RUN go build -trimpath -ldflags="-s -w" -o /out/codegraph-worker ./cmd/codegraph-worker

FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM debian:bookworm-slim AS runtime
# python3 is the interpreter uv reads target markers from when it installs
# the wheels of an ingested checkout's packages; no package code runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gosu libstdc++6 python3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 codegraph \
    && useradd --uid 10001 --gid codegraph --create-home codegraph \
    && mkdir -p /var/lib/codegraph /etc/codegraph \
    && chown codegraph:codegraph /var/lib/codegraph

COPY --from=build /out/codegraph-worker /usr/local/bin/codegraph-worker
COPY --from=java-tools /opt/java/codegraph-jdk /opt/java/codegraph-jdk
COPY --from=java-tools /usr/share/maven /opt/maven
COPY --from=python-analyzer /usr/local/bin/node /usr/local/bin/node
# npm installs the npm projects of ingested checkouts for the TypeScript
# compiler, with every lifecycle script off.
COPY --from=python-analyzer /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY --from=python-analyzer /analyzer/dist /opt/codegraph/python
COPY --from=typescript-analyzer /analyzer/dist /opt/codegraph/typescript
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /home/codegraph
ENV CODEGRAPH_WORK_DIR=/var/lib/codegraph
ENV JAVA_HOME=/opt/java/codegraph-jdk
ENV CODEGRAPH_PYTHON_ANALYZER=/opt/codegraph/python/bridge.cjs
ENV CODEGRAPH_TYPESCRIPT_ANALYZER=/opt/codegraph/typescript/bridge.cjs
ENV PATH=/opt/java/codegraph-jdk/bin:/opt/maven/bin:$PATH
COPY --chmod=755 apps/forge-codegraph-worker/scripts/entrypoint.sh /usr/local/bin/codegraph-entrypoint
USER root
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/codegraph-entrypoint"]
CMD ["/usr/local/bin/codegraph-worker"]
