DOCKER ?= docker
NPM ?= npm
UV ?= uv
GO ?= GOTOOLCHAIN=auto go
# SPANNER=cloud puts the code graph worker on managed Cloud Spanner
# (WORKER_CLOUD_SPANNER_DATABASE in .env.compose) instead of the emulator:
# natively for make worker, and in Compose for up, restart, down, logs and
# status, which then add compose.cloud.yaml.
SPANNER ?= emulator
# .env.common supplies the ${NAME} references in each app's .env; it adds no
# variables to a container by itself.
COMPOSE = $(DOCKER) compose --env-file .env.compose --env-file .env.common -f compose.yaml $(if $(filter cloud,$(SPANNER)),-f compose.cloud.yaml)
CLOUD_SPANNER_DATABASE = $(shell sed -n 's/^WORKER_CLOUD_SPANNER_DATABASE=//p' .env.compose 2>/dev/null | tr -d "\"'" | head -1)
SERVICES := web admin async-worker-adk-workflows async-worker-documents worker codegraph-mcp
# The web apps each own package.json and node_modules; the Python apps and
# packages are one uv workspace, with one uv.lock and .venv at the root
# (pyproject.toml lists its members).
WEB_DIR := apps/forge-web
ADMIN_DIR := apps/forge-admin-api
ASYNC_WORKER_DIR := apps/forge-async-worker
# The code graph ingestion worker: a Go module, with the Go modules it links
# under packages/go/code-graph (the root go.work joins them).
WORKER_DIR := apps/forge-codegraph-worker
# One ./... pattern cannot span several modules of the go.work, so each is
# listed.
WORKER_GO_PACKAGES := ./$(WORKER_DIR)/... $(foreach m,agentquery authorization domain serviceconfig storage tree-sitter-java,./packages/go/code-graph/$(m)/...)
# The code graph MCP server: a Python app of the uv workspace that reads the
# graph the worker writes.
CODEGRAPH_MCP_DIR := apps/forge-codegraph-mcp
# Every app embeds with the one model and vector length in .env.common
# (FORGE_EMBEDDING_*): each app's embedding settings, as file:setting:shared
# name, which make env points at .env.common's by reference.
EMBEDDING_REFERENCES := \
  $(ADMIN_DIR)/.env:FORGE_ADMIN_KNOWLEDGE_EMBEDDING__DOCUMENT_MODEL:FORGE_EMBEDDING_MODEL \
  $(ADMIN_DIR)/.env:FORGE_ADMIN_KNOWLEDGE_EMBEDDING__DIMENSIONS:FORGE_EMBEDDING_DIMENSIONS \
  $(ADMIN_DIR)/.env:FORGE_ADMIN_KNOWLEDGE_EMBEDDING__BASE_URL:FORGE_EMBEDDING_BASE_URL \
  $(ASYNC_WORKER_DIR)/.env:HYBRID_EMBEDDING__DOCUMENT_MODEL:FORGE_EMBEDDING_MODEL \
  $(ASYNC_WORKER_DIR)/.env:HYBRID_EMBEDDING__DIMENSIONS:FORGE_EMBEDDING_DIMENSIONS \
  $(ASYNC_WORKER_DIR)/.env:HYBRID_EMBEDDING__BASE_URL:FORGE_EMBEDDING_BASE_URL \
  $(WORKER_DIR)/.env:CODEGRAPH_EMBEDDING_MODEL:FORGE_EMBEDDING_MODEL \
  $(WORKER_DIR)/.env:CODEGRAPH_EMBEDDING_DIMENSIONS:FORGE_EMBEDDING_DIMENSIONS \
  $(WORKER_DIR)/.env:CODEGRAPH_EMBEDDING_BASE_URL:FORGE_EMBEDDING_BASE_URL \
  $(CODEGRAPH_MCP_DIR)/.env:CODEGRAPH_EMBEDDING__MODEL:FORGE_EMBEDDING_MODEL \
  $(CODEGRAPH_MCP_DIR)/.env:CODEGRAPH_EMBEDDING__DIMENSIONS:FORGE_EMBEDDING_DIMENSIONS \
  $(CODEGRAPH_MCP_DIR)/.env:CODEGRAPH_EMBEDDING__BASE_URL:FORGE_EMBEDDING_BASE_URL
# The task packages the async worker bundles: checked and tested with it.
TASK_PACKAGES := $(addprefix packages/python/,task-sdk adk-workflows documents embeddings)
# Python packages the apps share.
COMMON_DIR := packages/python/common
AGENT_RUNTIME_DIR := packages/python/agent-runtime
MCP_SERVERS_DIR := packages/python/mcp-servers
CODEGRAPH_DIR := packages/python/codegraph
JSONATA_DIR := packages/python/jsonata
# The Forge UI design system: its demo and the shadcn registry apps install from.
FORGE_UI_DIR := packages/forge-ui
# The apps make up-all-local runs natively, each with its own make target.
LOCAL_APPS := web admin async-worker worker codegraph-mcp
.PHONY: help env install python-install infrastructure start up up-all-local up-all-local-deps down logs logs-web logs-admin logs-async-worker logs-worker logs-codegraph-mcp status restart docker-build check
.PHONY: web web-install web-check web-test web-build web-lint web-token
.PHONY: admin admin-install admin-deps admin-migrate admin-check admin-test-mysql admin-fmt
.PHONY: async-worker async-worker-install async-worker-deps async-worker-check async-worker-fmt async-worker-test-redis
.PHONY: worker worker-install worker-deps worker-check worker-fmt worker-test-python worker-test-typescript worker-test-spanner worker-test-mysql worker-test-java
.PHONY: worker-spanner-cloud worker-spanner-cloud-update worker-spanner-cloud-reset
.PHONY: codegraph-mcp codegraph-mcp-install codegraph-mcp-check codegraph-mcp-fmt
.PHONY: common-check common-fmt
.PHONY: agent-runtime-check agent-runtime-fmt starter-wheels mcp-servers-check codegraph-check
.PHONY: shared-deps infrastructure-check
.PHONY: jsonata-check jsonata-fmt jsonata-test-re2
.PHONY: forge-ui forge-ui-install forge-ui-check forge-ui-registry

# Appends line $(1) to env file $(2) on a line of its own, even when the
# file's last line has no newline (echo >> alone would join the two).
append = { [ -z "$$(tail -c1 $(2))" ] || echo >> $(2); echo "$(1)" >> $(2); }

.DEFAULT_GOAL := help

help:
	@echo "make install            Install the web apps' and Forge UI's npm dependencies, the Python workspace into .venv, and the code graph worker's analyzers and Go modules"
	@echo "make infrastructure     Start admin MySQL, migrate (default roles) and seed you as the site admin user"
	@echo "make shared-deps        Start the shared MongoDB, Redis and S3 (RustFS, for knowledge base documents)"
	@echo "make infrastructure-check Validate the Compose stack: shared services, connections and queues"
	@echo "make web-token          Sign the web console in as you: a bearer token in apps/forge-web/.env.local and .env.compose"
	@echo "make start / make up    Build and start web, admin, the async workers (the adk_workflows and documents queues), the code graph worker and MCP server and their databases (web on 18190, admin on 18201, worker on 18090, MCP on 18203)"
	@echo "make up SPANNER=cloud   The same with the code graph worker on managed Cloud Spanner; no emulator"
	@echo "make up-all-local       Start the databases, then run web, admin, async-worker, worker and codegraph-mcp natively in this terminal; Ctrl-C stops them"
	@echo "make down               Stop the stack (MySQL, MongoDB and Redis data persist in their volumes; the Spanner emulator keeps nothing)"
	@echo "make logs / logs-web / logs-admin / logs-async-worker / logs-worker / logs-codegraph-mcp  Follow logs"
	@echo "make status             Show service status"
	@echo "make restart            Rebuild and recreate the services after editing their settings"
	@echo "make docker-build       Build the service images without starting them"
	@echo "make check              Infrastructure, web, admin, async worker, code graph worker and MCP server, shared package and Forge UI checks, and the web production build"
	@echo "make web                Run the React dev server on http://localhost:5190"
	@echo "make web-check / web-test / web-build / web-lint  Run one web step"
	@echo "make admin-deps         Start admin MySQL plus the shared MongoDB and Redis for native runs"
	@echo "make admin              Run the admin API natively with reload on http://localhost:8101"
	@echo "make admin-migrate      Apply admin database migrations natively"
	@echo "make admin-check        Admin lint, format check and unit tests"
	@echo "make admin-test-mysql   Start admin MySQL and run the admin integration tests"
	@echo "make admin-fmt          Format and autofix the admin sources"
	@echo "make async-worker-deps  Start the shared Redis and the admin MySQL for the async worker"
	@echo "make async-worker       Run the async worker natively on the adk_workflows queue: ADK workflow runs, kept in the admin MySQL"
	@echo "make async-worker-check Async worker and task packages: lint, format check, types and unit tests"
	@echo "make async-worker-fmt   Format and autofix the async worker and task package sources"
	@echo "make async-worker-test-redis  Start the shared Redis and run the SAQ worker integration tests"
	@echo "make worker-install     Build the code graph worker's Python and TypeScript analyzers and download its Go modules"
	@echo "make worker-deps        Start the Spanner emulator (127.0.0.1:19030) and the admin MySQL (its job queue) for native worker runs"
	@echo "make worker             Run the code graph ingestion worker natively against the emulator (health and admission API on http://localhost:8090)"
	@echo "make worker SPANNER=cloud  The same against managed Cloud Spanner (WORKER_CLOUD_SPANNER_DATABASE), as your gcloud user"
	@echo "make worker-spanner-cloud  Create the managed database with the codegraph schema (gcloud; the instance must exist)"
	@echo "make worker-spanner-cloud-update  Add the tables and indexes a newer schema has to it, after showing them (FORCE=1 skips asking)"
	@echo "make worker-spanner-cloud-reset  Drop and recreate it, after typing its name (FORCE=1 skips asking)"
	@echo "make worker-check       Code graph worker and its Go modules: vet and unit tests"
	@echo "make worker-fmt         Format the code graph worker and its Go modules"
	@echo "make worker-test-python Build the Python analyzer and run its tests and the Go Python tests"
	@echo "make worker-test-typescript  Build the TypeScript compiler bridge and run its tests and the Go TypeScript tests"
	@echo "make worker-test-spanner  Start the emulator and run the Spanner integration tests"
	@echo "make worker-test-mysql  Start and migrate the admin MySQL and run the job queue integration tests"
	@echo "make worker-test-java   Java resolver and Maven tests (CODEGRAPH_TEST_JAVA_HOME, CODEGRAPH_TEST_MAVEN)"
	@echo "make codegraph-mcp      Run the code graph MCP server natively on http://localhost:8103/mcp, reading the worker's graph in the emulator (run make worker too)"
	@echo "make codegraph-mcp SPANNER=cloud  The same against managed Cloud Spanner (WORKER_CLOUD_SPANNER_DATABASE), as your gcloud user"
	@echo "make codegraph-mcp-check Code graph MCP server: lint, format check, types and unit tests"
	@echo "make codegraph-mcp-fmt  Format and autofix the code graph MCP server"
	@echo "make common-check       Shared Python package (forge-common): lint, format check, types and unit tests"
	@echo "make agent-runtime-check The chat agent runtime (forge-agent-runtime): lint, format check, types, tests and a package build"
	@echo "make starter-wheels     The runtime's wheels standalone agent projects carry (into the runtime's dist/)"
	@echo "make mcp-servers-check  Organizations' MCP servers (forge-mcp-servers, admin and worker): lint, format check, types and tests"
	@echo "make codegraph-check    The code graph worker's client (forge-codegraph, admin and worker): lint, format check, types and tests"
	@echo "make common-fmt         Format and autofix the shared Python package sources"
	@echo "make jsonata-check      Local JSONata engine: lint, format and full upstream compatibility suite"
	@echo "make jsonata-fmt        Format and autofix the local JSONata engine"
	@echo "make jsonata-test-re2   Test the optional RE2 regex engine integration"
	@echo "make forge-ui           Run the Forge UI design system demo on http://localhost:5185"
	@echo "make forge-ui-registry  Rebuild the Forge UI shadcn registry (registry.json, public/r); commit it with the change"
	@echo "make forge-ui-check     Forge UI typecheck, tests, and the registry rebuilt and committed (as CI runs it)"

env:
	@test -e .env.compose || (umask 077; cp .env.compose.example .env.compose)
	@test -e .env.common || (umask 077; cp .env.common.example .env.common)
	@for dir in $(ADMIN_DIR) $(ASYNC_WORKER_DIR) $(WORKER_DIR) $(CODEGRAPH_MCP_DIR); do \
	  test -e $$dir/.env || (umask 077; cp $$dir/.env.example $$dir/.env); \
	done
	@# The code graph's shared settings: a .env.common from before them takes
	@# the template's lines, then the admission token and the cursor signing
	@# key are generated, once.
	@grep -q '^FORGE_CODEGRAPH_ADMISSION_TOKEN=' .env.common || { \
	  { [ -z "$$(tail -c1 .env.common)" ] || echo; echo; \
	    sed -n '/^# The code graph:/,/^FORGE_GITHUB_TOKEN=/p' .env.common.example; } >> .env.common; \
	  echo "Added the code graph settings to .env.common"; }
	@for name in FORGE_CODEGRAPH_ADMISSION_TOKEN FORGE_CODEGRAPH_CURSOR_SIGNING_KEY; do \
	  grep -Eq "^$$name=.{32}" .env.common || { \
	    perl -i -pe "s/^$$name=.*/$$name=$$(openssl rand -hex 32)/" .env.common; \
	    echo "Generated $$name in .env.common"; }; \
	done
	@# The code graph worker claims its jobs from the admin API's MySQL: a
	@# worker .env from before the queue moved there takes the template's line.
	@grep -q '^CODEGRAPH_JOBS_MYSQL_DSN=' $(WORKER_DIR)/.env || { \
	  { [ -z "$$(tail -c1 $(WORKER_DIR)/.env)" ] || echo; \
	    grep '^CODEGRAPH_JOBS_MYSQL_DSN=' $(WORKER_DIR)/.env.example; } >> $(WORKER_DIR)/.env; \
	  echo "Added CODEGRAPH_JOBS_MYSQL_DSN to $(WORKER_DIR)/.env"; }
	@# Graph knowledge bases: the admin API and the workflows worker call the
	@# code graph worker's API; a .env from before them takes the template's
	@# lines.
	@for entry in $(ADMIN_DIR):FORGE_ADMIN_CODEGRAPH $(ASYNC_WORKER_DIR):HYBRID_ADK_WORKFLOWS__CODEGRAPH; do \
	  dir=$${entry%%:*}; prefix=$${entry#*:}; \
	  for setting in $${prefix}_URL $${prefix}_TOKEN; do \
	    grep -q "^$$setting=" $$dir/.env || { \
	      { [ -z "$$(tail -c1 $$dir/.env)" ] || echo; grep "^$$setting=" $$dir/.env.example; } >> $$dir/.env; \
	      echo "Added $$setting to $$dir/.env"; }; \
	  done; \
	done
	@# The embedding model every app shares: a .env.common from before it
	@# takes the template's lines, keeping the code graph's model and
	@# dimensions (its database was made for them) in place of the old names.
	@grep -q '^FORGE_EMBEDDING_MODEL=' .env.common || { \
	  model=$$(sed -n 's/^FORGE_CODEGRAPH_EMBEDDING_MODEL=//p' .env.common | tail -1); \
	  dimensions=$$(sed -n 's/^FORGE_CODEGRAPH_EMBEDDING_DIMENSIONS=//p' .env.common | tail -1); \
	  { [ -z "$$(tail -c1 .env.common)" ] || echo; echo; \
	    sed -n '/^# Embeddings:/,/^FORGE_EMBEDDING_BASE_URL=/p' .env.common.example; } >> .env.common; \
	  [ -z "$$model" ] || MODEL="$$model" perl -i -pe 's/^FORGE_EMBEDDING_MODEL=.*/FORGE_EMBEDDING_MODEL=$$ENV{MODEL}/' .env.common; \
	  [ -z "$$dimensions" ] || DIMENSIONS="$$dimensions" perl -i -pe 's/^FORGE_EMBEDDING_DIMENSIONS=.*/FORGE_EMBEDDING_DIMENSIONS=$$ENV{DIMENSIONS}/' .env.common; \
	  echo "Added the shared embedding model (FORGE_EMBEDDING_*) to .env.common"; }
	@! grep -q '^FORGE_CODEGRAPH_EMBEDDING_' .env.common || { \
	  perl -i -ne 'print unless /^FORGE_CODEGRAPH_EMBEDDING_(MODEL|DIMENSIONS)=/' .env.common; \
	  echo "Removed FORGE_CODEGRAPH_EMBEDDING_* from .env.common: FORGE_EMBEDDING_* replaced them"; }
	@for entry in $(EMBEDDING_REFERENCES); do \
	  file=$${entry%%:*}; rest=$${entry#*:}; setting=$${rest%%:*}; reference="\$${$${rest#*:}}"; \
	  old=$$(sed -n "s/^$$setting=//p" $$file | tail -1); \
	  [ "$$old" = "$$reference" ] && continue; \
	  if grep -q "^$$setting=" $$file; then \
	    SETTING=$$setting REFERENCE=$$reference perl -i -pe 's/^\Q$$ENV{SETTING}\E=.*/$$ENV{SETTING}=$$ENV{REFERENCE}/' $$file; \
	    case "$$old" in \
	      ""|'$${FORGE_CODEGRAPH_EMBEDDING_'*) echo "Pointed $$setting in $$file at $$reference";; \
	      *) echo "Pointed $$setting in $$file at $$reference instead of its own value: what it embedded with another model or length needs embedding again";; \
	    esac; \
	  else $(call append,$$setting=$$reference,$$file); echo "Added $$setting=$$reference to $$file"; fi; \
	done
	@# The key the admin API signs and checks bearer tokens with, once.
	@grep -Eq '^FORGE_ADMIN_JWT_SECRET=.{32,}' $(ADMIN_DIR)/.env || { \
	  secret=$$(openssl rand -hex 32); \
	  if grep -q '^FORGE_ADMIN_JWT_SECRET=' $(ADMIN_DIR)/.env; then \
	    perl -i -pe "s/^FORGE_ADMIN_JWT_SECRET=.*/FORGE_ADMIN_JWT_SECRET=$$secret/" $(ADMIN_DIR)/.env; \
	  else $(call append,FORGE_ADMIN_JWT_SECRET=$$secret,$(ADMIN_DIR)/.env); fi; \
	  echo "Generated FORGE_ADMIN_JWT_SECRET in $(ADMIN_DIR)/.env"; }
	@# The key the admin API encrypts MCP servers' credentials with, once.
	@grep -Eq '^FORGE_ADMIN_SECRETS_KEY=.{32,}' $(ADMIN_DIR)/.env || { \
	  secret=$$(openssl rand -hex 32); \
	  if grep -q '^FORGE_ADMIN_SECRETS_KEY=' $(ADMIN_DIR)/.env; then \
	    perl -i -pe "s/^FORGE_ADMIN_SECRETS_KEY=.*/FORGE_ADMIN_SECRETS_KEY=$$secret/" $(ADMIN_DIR)/.env; \
	  else $(call append,FORGE_ADMIN_SECRETS_KEY=$$secret,$(ADMIN_DIR)/.env); fi; \
	  echo "Generated FORGE_ADMIN_SECRETS_KEY in $(ADMIN_DIR)/.env"; }
	@# The worker connects to organizations' MCP servers with the same key.
	@key=$$(sed -n 's/^FORGE_ADMIN_SECRETS_KEY=//p' $(ADMIN_DIR)/.env | tail -1); \
	grep -Eq "^HYBRID_ADK_WORKFLOWS__SECRETS_KEY=$$key$$" $(ASYNC_WORKER_DIR)/.env || { \
	  if grep -q '^HYBRID_ADK_WORKFLOWS__SECRETS_KEY=' $(ASYNC_WORKER_DIR)/.env; then \
	    perl -i -pe "s/^HYBRID_ADK_WORKFLOWS__SECRETS_KEY=.*/HYBRID_ADK_WORKFLOWS__SECRETS_KEY=$$key/" $(ASYNC_WORKER_DIR)/.env; \
	  else $(call append,HYBRID_ADK_WORKFLOWS__SECRETS_KEY=$$key,$(ASYNC_WORKER_DIR)/.env); fi; \
	  echo "Copied FORGE_ADMIN_SECRETS_KEY into $(ASYNC_WORKER_DIR)/.env"; }
	@# The code graph MCP server has the admin API check its callers'
	@# credentials, sending the admin's deployment key (if it has one) with
	@# each question; a .env from when it checked JWTs itself loses those
	@# settings, the admin API's signing key among them.
	@! grep -Eq '^CODEGRAPH_MCP__AUTH__(MODE|ALGORITHM|ISSUER|AUDIENCE|PUBLIC_KEY)=' $(CODEGRAPH_MCP_DIR)/.env || { \
	  perl -i -ne 'print unless /^CODEGRAPH_MCP__AUTH__(MODE|ALGORITHM|ISSUER|AUDIENCE|PUBLIC_KEY)=/' $(CODEGRAPH_MCP_DIR)/.env; \
	  echo "Removed the JWT settings from $(CODEGRAPH_MCP_DIR)/.env: the admin API checks its callers now"; }
	@key=$$(sed -n 's/^FORGE_ADMIN_API_KEY=//p' $(ADMIN_DIR)/.env | tail -1); \
	grep -Fqx "CODEGRAPH_MCP__AUTH__ADMIN_API_KEY=$$key" $(CODEGRAPH_MCP_DIR)/.env || { \
	  if grep -q '^CODEGRAPH_MCP__AUTH__ADMIN_API_KEY=' $(CODEGRAPH_MCP_DIR)/.env; then \
	    KEY="$$key" perl -i -pe 's/^CODEGRAPH_MCP__AUTH__ADMIN_API_KEY=.*/CODEGRAPH_MCP__AUTH__ADMIN_API_KEY=$$ENV{KEY}/' $(CODEGRAPH_MCP_DIR)/.env; \
	  else $(call append,CODEGRAPH_MCP__AUTH__ADMIN_API_KEY=$$key,$(CODEGRAPH_MCP_DIR)/.env); fi; \
	  echo "Copied FORGE_ADMIN_API_KEY into $(CODEGRAPH_MCP_DIR)/.env"; }

install: web-install python-install forge-ui-install worker-install

# Every Python app and package, and the tools their checks run, into the
# workspace's .venv at the root.
python-install:
	$(UV) sync --locked --all-packages

# MySQL, the schema and you as the site administrator (the
# FORGE_ADMIN_SITE_ADMIN_* settings in apps/forge-admin-api/.env), for development and
# testing through the UI and the API. Safe to rerun: the seed only adds.
infrastructure: env
	$(COMPOSE) up -d --wait admin-mysql
	$(COMPOSE) run --rm --build admin-seed
	@echo "Next: make web-token, so the web console signs in as you"

up: env
	$(COMPOSE) up -d --build --wait $(SERVICES)

start: up

# Every app natively with reload, in one terminal: the databases first (in
# Compose, once), then the apps' own targets in parallel, each output line
# prefixed with its app. An app that exits says so and leaves the others
# running; Ctrl-C stops them all, and the databases keep running until make
# down. Use it instead of make up: the native worker must not also run in
# Compose on the same queue.
up-all-local: env
	@$(MAKE) --no-print-directory up-all-local-deps
	@$(MAKE) --no-print-directory -j$(words $(LOCAL_APPS)) $(addprefix local-,$(LOCAL_APPS))

up-all-local-deps: admin-deps async-worker-deps $(if $(filter cloud,$(SPANNER)),,worker-deps)

# One app of up-all-local. The prefixer ignores Ctrl-C, so it relays each
# app's shutdown until the app closes its output.
local-%:
	@{ $(MAKE) --no-print-directory $*; echo "exited with status $$?"; } 2>&1 | (trap '' INT; awk '{ print "[$*] " $$0; fflush() }')

down: env
	$(COMPOSE) down

logs: env
	$(COMPOSE) logs -f --tail=100

logs-web: env
	$(COMPOSE) logs -f --tail=100 web

logs-admin: env
	$(COMPOSE) logs -f --tail=100 admin

logs-async-worker: env
	$(COMPOSE) logs -f --tail=100 async-worker-adk-workflows async-worker-documents

logs-worker: env
	$(COMPOSE) logs -f --tail=100 worker

logs-codegraph-mcp: env
	$(COMPOSE) logs -f --tail=100 codegraph-mcp

status: env
	$(COMPOSE) ps -a

restart: env
	$(COMPOSE) up -d --build --force-recreate --wait $(SERVICES)

docker-build: env
	$(COMPOSE) build $(SERVICES)

check: infrastructure-check web-check web-test web-build admin-check async-worker-check worker-check codegraph-mcp-check common-check agent-runtime-check mcp-servers-check codegraph-check jsonata-check forge-ui-check

web-install:
	cd $(WEB_DIR) && $(NPM) ci

web:
	cd $(WEB_DIR) && $(NPM) run dev

# Local development: mint a bearer token for the site administrator and set it
# as VITE_API_TOKEN for make web (apps/forge-web/.env.local) and make up
# (.env.compose). Needs the seeded user; rerun when it expires (90 days).
web-token: env
	@token=$$(cd $(ADMIN_DIR) && $(UV) run --quiet forge-admin-token) || exit 1; \
	for file in $(WEB_DIR)/.env.local .env.compose; do \
	  (umask 077; touch $$file); \
	  if grep -q '^VITE_API_TOKEN=' $$file; then \
	    perl -i -pe "s/^VITE_API_TOKEN=.*/VITE_API_TOKEN=$$token/" $$file; \
	  else \
	    printf '\n# Local development only: the admin API bearer token (make web-token).\nVITE_API_TOKEN=%s\n' "$$token" >> $$file; \
	  fi; \
	done; \
	echo "Set VITE_API_TOKEN in $(WEB_DIR)/.env.local and .env.compose; restart make web, or make restart"

web-check:
	cd $(WEB_DIR) && $(NPM) run check

# The web console's tests (node --test): the builders' steps, the ADK
# workflow and chat agent builders, and the timestamp helpers.
web-test:
	cd $(WEB_DIR) && $(NPM) run test:steps && $(NPM) run test:agents && $(NPM) run test:timestamps

web-build:
	cd $(WEB_DIR) && $(NPM) run build

web-lint:
	cd $(WEB_DIR) && $(NPM) run lint

admin-install: python-install

shared-deps: env
	$(COMPOSE) up -d --wait mongo redis knowledge-s3

infrastructure-check:
	python3 -m unittest discover -s tests/infrastructure -v

admin-deps: shared-deps
	$(COMPOSE) up -d --wait admin-mysql

admin: env
	cd $(ADMIN_DIR) && FORGE_ADMIN_RELOAD=true $(UV) run forge-admin

admin-migrate: env
	cd $(ADMIN_DIR) && $(UV) run alembic upgrade head

admin-check:
	cd $(ADMIN_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run pytest

admin-test-mysql: admin-deps
	cd $(ADMIN_DIR) && FORGE_ADMIN_TEST_MYSQL=1 $(UV) run pytest -m mysql

admin-fmt:
	cd $(ADMIN_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .

async-worker-install: python-install

# Redis for the SAQ queue, and the admin MySQL for the runs (the run store,
# which make admin-migrate creates) and their ADK sessions.
async-worker-deps: env
	$(COMPOSE) up -d --wait redis admin-mysql mongo knowledge-s3

# Natively against redis, admin-mysql, mongo and knowledge-s3 (settings from
# apps/forge-async-worker/.env): sets up the ADK session tables and checks the
# run store's, and the knowledge bases' Atlas Search and Vector Search
# indexes, then runs the adk_workflows queue's runs and upkeep and the
# documents queue's ingests.
async-worker: async-worker-deps
	cd $(ASYNC_WORKER_DIR) && $(UV) run forge-async-worker worker --ensure-schema

# The worker, then each task package it bundles.
async-worker-check:
	cd $(ASYNC_WORKER_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src && $(UV) run pytest
	@for dir in $(TASK_PACKAGES); do \
	  echo "== $$dir"; \
	  (cd $$dir && $(UV) run ruff check . && $(UV) run ruff format --check . \
	    && $(UV) run mypy src && $(UV) run pytest) || exit 1; \
	done

async-worker-fmt:
	cd $(ASYNC_WORKER_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .
	@for dir in $(TASK_PACKAGES); do \
	  (cd $$dir && $(UV) run ruff check --fix . && $(UV) run ruff format .) || exit 1; \
	done

# The SAQ worker on a real Redis, in redis database 15, which the
# tests clear.
async-worker-test-redis: env
	$(COMPOSE) up -d --wait redis
	@addr=$$($(COMPOSE) port redis 6379) && cd $(ASYNC_WORKER_DIR) && \
	  HYBRID_TEST_REDIS_URL=redis://$$addr/15 $(UV) run pytest -m redis

# The code graph worker's Pyright and TypeScript compiler bridges (each into
# its dist/, which the worker runs under Node), then its Go modules.
worker-install:
	cd $(WORKER_DIR)/python-analyzer && $(NPM) ci --ignore-scripts && $(NPM) run build
	cd $(WORKER_DIR)/typescript-analyzer && $(NPM) ci --ignore-scripts && $(NPM) run build
	$(GO) -C $(WORKER_DIR) mod download

# The Spanner emulator, and the admin MySQL whose code_ingestion_jobs the
# worker claims from (make admin-migrate creates it).
worker-deps: env
	$(COMPOSE) up -d --wait worker-spanner admin-mysql

# Natively with settings from apps/forge-codegraph-worker/.env: against the
# emulator, which it provisions (instance, database, schema) on start, or
# with SPANNER=cloud against the managed database, as your gcloud user.
ifeq ($(SPANNER),cloud)
worker: env
	@test -n "$(CLOUD_SPANNER_DATABASE)" || { echo "Set WORKER_CLOUD_SPANNER_DATABASE in .env.compose"; exit 1; }
	env -u SPANNER_EMULATOR_HOST CODEGRAPH_SPANNER_DATABASE="$(CLOUD_SPANNER_DATABASE)" \
	  CODEGRAPH_SPANNER_AUTO_PROVISION=false SPANNER_DISABLE_BUILTIN_METRICS=true \
	  $(GO) -C $(WORKER_DIR) run ./cmd/codegraph-worker
else
worker: worker-deps
	SPANNER_EMULATOR_HOST=$$($(COMPOSE) port worker-spanner 9010) CODEGRAPH_SPANNER_AUTO_PROVISION=true \
	  $(GO) -C $(WORKER_DIR) run ./cmd/codegraph-worker
endif

worker-spanner-cloud: env
	GO="$(GO)" $(WORKER_DIR)/scripts/spanner-cloud.sh create

worker-spanner-cloud-update: env
	GO="$(GO)" FORCE=$(FORCE) $(WORKER_DIR)/scripts/spanner-cloud.sh update

worker-spanner-cloud-reset: env
	GO="$(GO)" FORCE=$(FORCE) $(WORKER_DIR)/scripts/spanner-cloud.sh reset

worker-check:
	$(GO) vet $(WORKER_GO_PACKAGES)
	$(GO) test $(WORKER_GO_PACKAGES)

worker-fmt:
	$(GO) fmt $(WORKER_GO_PACKAGES)

codegraph-mcp-install: python-install

# Natively with reload and settings from apps/forge-codegraph-mcp/.env: against
# the emulator, reading the database make worker provisions (startup waits up
# to CODEGRAPH_SPANNER__STARTUP_TIMEOUT for it), or with SPANNER=cloud against
# the managed database, as your gcloud user.
ifeq ($(SPANNER),cloud)
codegraph-mcp: env
	@test -n "$(CLOUD_SPANNER_DATABASE)" || { echo "Set WORKER_CLOUD_SPANNER_DATABASE in .env.compose"; exit 1; }
	cd $(CODEGRAPH_MCP_DIR) && env -u SPANNER_EMULATOR_HOST CODEGRAPH_SPANNER__DATABASE="$(CLOUD_SPANNER_DATABASE)" \
	  CODEGRAPH_SERVER__RELOAD=true SPANNER_DISABLE_BUILTIN_METRICS=true $(UV) run forge-codegraph-mcp
else
codegraph-mcp: worker-deps
	host=$$($(COMPOSE) port worker-spanner 9010) && cd $(CODEGRAPH_MCP_DIR) && \
	  SPANNER_EMULATOR_HOST=$$host CODEGRAPH_SERVER__RELOAD=true $(UV) run forge-codegraph-mcp
endif

codegraph-mcp-check:
	cd $(CODEGRAPH_MCP_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src tests && $(UV) run pytest

codegraph-mcp-fmt:
	cd $(CODEGRAPH_MCP_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .

worker-test-python:
	cd $(WORKER_DIR)/python-analyzer && $(NPM) ci --ignore-scripts && $(NPM) run build && $(NPM) test
	CODEGRAPH_REQUIRE_PYTHON_TESTS=1 $(GO) test ./$(WORKER_DIR)/internal/parser/python ./$(WORKER_DIR)/internal/resolve/python ./$(WORKER_DIR)/internal/languages/python/...

worker-test-typescript:
	cd $(WORKER_DIR)/typescript-analyzer && $(NPM) ci --ignore-scripts && $(NPM) run build && $(NPM) test
	CODEGRAPH_REQUIRE_TS_COMPILER=1 $(GO) test ./$(WORKER_DIR)/internal/parser/typescript ./$(WORKER_DIR)/internal/resolve/typescript ./$(WORKER_DIR)/internal/languages/typescript/...

# The Spanner store and the ingestion pipeline on the emulator.
worker-test-spanner: env
	$(COMPOSE) up -d --wait worker-spanner
	SPANNER_EMULATOR_HOST=$$($(COMPOSE) port worker-spanner 9010) \
	  $(GO) -C packages/go/code-graph/storage test -race -tags=integration ./spanner -count=1
	SPANNER_EMULATOR_HOST=$$($(COMPOSE) port worker-spanner 9010) \
	  $(GO) -C $(WORKER_DIR) test -race -tags=integration ./internal/ingestion -count=1

# The MySQL job queue on the admin MySQL, migrated, in organizations and a
# queue of the tests' own, which they remove.
worker-test-mysql: env
	$(COMPOSE) up -d --wait admin-mysql
	@$(MAKE) --no-print-directory admin-migrate
	@user=$$(sed -n 's/^FORGE_MYSQL_USER=//p' .env.common | tail -1); \
	password=$$(sed -n 's/^FORGE_MYSQL_PASSWORD=//p' .env.common | tail -1); \
	port=$$(sed -n 's/^FORGE_MYSQL_PORT=//p' .env.common | tail -1); \
	database=$$(sed -n 's/^FORGE_MYSQL_DATABASE=//p' .env.common | tail -1); \
	CODEGRAPH_TEST_MYSQL_DSN="$$user:$$password@tcp(127.0.0.1:$$port)/$$database" \
	  $(GO) -C $(WORKER_DIR) test -race -tags=integration ./internal/jobqueue -count=1

worker-test-java:
	$(GO) -C $(WORKER_DIR) test ./internal/resolve/... ./internal/languages/... -count=1

common-check:
	cd $(COMMON_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src tests && $(UV) run pytest

agent-runtime-check:
	cd $(AGENT_RUNTIME_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src tests && $(UV) run pytest
	$(UV) build --package forge-agent-runtime -o $(AGENT_RUNTIME_DIR)/dist

mcp-servers-check:
	cd $(MCP_SERVERS_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src && $(UV) run pytest

codegraph-check:
	cd $(CODEGRAPH_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src tests && $(UV) run pytest

starter-wheels:
	for package in forge-agent-runtime forge-common forge-jsonata; do \
		$(UV) build --package $$package --wheel -o $(AGENT_RUNTIME_DIR)/dist || exit 1; \
	done

agent-runtime-fmt:
	cd $(AGENT_RUNTIME_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .

common-fmt:
	cd $(COMMON_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .

jsonata-check:
	cd $(JSONATA_DIR) && $(UV) run --locked ruff check . && $(UV) run --locked ruff format --check . && $(UV) run --locked pytest

jsonata-fmt:
	cd $(JSONATA_DIR) && $(UV) run --locked ruff check --fix . && $(UV) run --locked ruff format .

jsonata-test-re2:
	cd $(JSONATA_DIR) && $(UV) run --locked --group re2 pytest tests/upstream/re2_engine_test.py

forge-ui-install:
	cd $(FORGE_UI_DIR) && $(NPM) ci

forge-ui:
	cd $(FORGE_UI_DIR) && $(NPM) run dev -- --port 5185 --strictPort

forge-ui-registry:
	cd $(FORGE_UI_DIR) && $(NPM) run registry:build

# registry:check rebuilds the registry and fails when registry.json or
# public/r differ from what's committed: apps install the committed files.
forge-ui-check:
	cd $(FORGE_UI_DIR) && npx tsc -b && $(NPM) test && $(NPM) run registry:check && $(NPM) run starter:build -- --check
