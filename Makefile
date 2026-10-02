DOCKER ?= docker
NPM ?= npm
UV ?= uv
# .env.common supplies the ${NAME} references in each app's .env; it adds no
# variables to a container by itself.
COMPOSE = $(DOCKER) compose --env-file .env.compose --env-file .env.common -f compose.yaml
SERVICES := web admin async-worker-adk-workflows
# Each app owns its dependencies: package.json and node_modules, or
# pyproject.toml, uv.lock and .venv.
WEB_DIR := apps/forge-web
ADMIN_DIR := apps/forge-admin-api
ASYNC_WORKER_DIR := apps/forge-async-worker
# The task packages the async worker bundles: checked, and tested, in its environment.
TASK_PACKAGES := $(addprefix packages/python/,task-sdk adk-workflows)
# Python packages the apps share, each with its own environment.
COMMON_DIR := packages/python/common
JSONATA_DIR := packages/python/jsonata
# The Forge UI design system: its demo and the shadcn registry apps install from.
FORGE_UI_DIR := packages/forge-ui
# The apps make up-all-local runs natively, each with its own make target.
LOCAL_APPS := web admin async-worker
.PHONY: help env install infrastructure start up up-all-local up-all-local-deps down logs logs-web logs-admin logs-async-worker status restart docker-build check
.PHONY: web web-install web-check web-test web-build web-lint web-token
.PHONY: admin admin-install admin-deps admin-migrate admin-check admin-test-mysql admin-fmt
.PHONY: async-worker async-worker-install async-worker-deps async-worker-check async-worker-fmt async-worker-test-redis
.PHONY: common-check common-fmt
.PHONY: shared-deps infrastructure-check
.PHONY: jsonata-check jsonata-fmt jsonata-test-re2
.PHONY: forge-ui forge-ui-install forge-ui-check forge-ui-registry

# Appends line $(1) to env file $(2) on a line of its own, even when the
# file's last line has no newline (echo >> alone would join the two).
append = { [ -z "$$(tail -c1 $(2))" ] || echo >> $(2); echo "$(1)" >> $(2); }

.DEFAULT_GOAL := help

help:
	@echo "make install            Install every app's, and Forge UI's, dependencies from its own lockfile"
	@echo "make infrastructure     Start admin MySQL, migrate (default roles) and seed you as the site admin user"
	@echo "make shared-deps        Start the shared MongoDB and Redis"
	@echo "make infrastructure-check Validate the Compose stack: shared services, connections and queues"
	@echo "make web-token          Sign the web console in as you: a bearer token in apps/forge-web/.env.local and .env.compose"
	@echo "make start / make up    Build and start web, admin, the async worker (the adk_workflows queue) and their databases (web on 18190, admin on 18201)"
	@echo "make up-all-local       Start the databases, then run web, admin and async-worker natively in this terminal; Ctrl-C stops them"
	@echo "make down               Stop the stack (MySQL, MongoDB and Redis data persist in their volumes)"
	@echo "make logs / logs-web / logs-admin / logs-async-worker  Follow logs"
	@echo "make status             Show service status"
	@echo "make restart            Rebuild and recreate the services after editing their settings"
	@echo "make docker-build       Build the service images without starting them"
	@echo "make check              Infrastructure, web, admin, async worker, shared package and Forge UI checks, and the web production build"
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
	@echo "make common-check       Shared Python package (forge-common): lint, format check, types and unit tests"
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
	@for dir in $(ADMIN_DIR) $(ASYNC_WORKER_DIR); do \
	  test -e $$dir/.env || (umask 077; cp $$dir/.env.example $$dir/.env); \
	done
	@# The key the admin API signs and checks bearer tokens with, once.
	@grep -Eq '^FORGE_ADMIN_JWT_SECRET=.{32,}' $(ADMIN_DIR)/.env || { \
	  secret=$$(openssl rand -hex 32); \
	  if grep -q '^FORGE_ADMIN_JWT_SECRET=' $(ADMIN_DIR)/.env; then \
	    perl -i -pe "s/^FORGE_ADMIN_JWT_SECRET=.*/FORGE_ADMIN_JWT_SECRET=$$secret/" $(ADMIN_DIR)/.env; \
	  else $(call append,FORGE_ADMIN_JWT_SECRET=$$secret,$(ADMIN_DIR)/.env); fi; \
	  echo "Generated FORGE_ADMIN_JWT_SECRET in $(ADMIN_DIR)/.env"; }

install: web-install admin-install async-worker-install forge-ui-install

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

up-all-local-deps: admin-deps async-worker-deps

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
	$(COMPOSE) logs -f --tail=100 async-worker-adk-workflows

status: env
	$(COMPOSE) ps -a

restart: env
	$(COMPOSE) up -d --build --force-recreate --wait $(SERVICES)

docker-build: env
	$(COMPOSE) build $(SERVICES)

check: infrastructure-check web-check web-test web-build admin-check async-worker-check common-check jsonata-check forge-ui-check

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

admin-install:
	cd $(ADMIN_DIR) && $(UV) sync --locked

shared-deps: env
	$(COMPOSE) up -d --wait mongo redis

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

async-worker-install:
	cd $(ASYNC_WORKER_DIR) && $(UV) sync --locked

# Redis for the SAQ queue, and the admin MySQL for the runs (the run store,
# which make admin-migrate creates) and their ADK sessions.
async-worker-deps: env
	$(COMPOSE) up -d --wait redis admin-mysql

# Natively against redis and admin-mysql (settings from
# apps/forge-async-worker/.env): sets up the ADK session tables and checks the
# run store's, then runs the adk_workflows queue's runs and upkeep.
async-worker: async-worker-deps
	cd $(ASYNC_WORKER_DIR) && $(UV) run forge-async-worker worker --ensure-schema

# The worker, then each task package in the worker's environment.
async-worker-check:
	cd $(ASYNC_WORKER_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src && $(UV) run pytest
	@for dir in $(TASK_PACKAGES); do \
	  echo "== $$dir"; \
	  (cd $$dir && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) ruff check . \
	    && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) ruff format --check . \
	    && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) mypy src \
	    && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) pytest) || exit 1; \
	done

async-worker-fmt:
	cd $(ASYNC_WORKER_DIR) && $(UV) run ruff check --fix . && $(UV) run ruff format .
	@for dir in $(TASK_PACKAGES); do \
	  (cd $$dir && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) ruff check --fix . \
	    && $(UV) run --project $(CURDIR)/$(ASYNC_WORKER_DIR) ruff format .) || exit 1; \
	done

# The SAQ worker on a real Redis, in redis database 15, which the
# tests clear.
async-worker-test-redis: env
	$(COMPOSE) up -d --wait redis
	@addr=$$($(COMPOSE) port redis 6379) && cd $(ASYNC_WORKER_DIR) && \
	  HYBRID_TEST_REDIS_URL=redis://$$addr/15 $(UV) run pytest -m redis

common-check:
	cd $(COMMON_DIR) && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src tests && $(UV) run pytest

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
	cd $(FORGE_UI_DIR) && npx tsc -b && $(NPM) test && $(NPM) run registry:check
