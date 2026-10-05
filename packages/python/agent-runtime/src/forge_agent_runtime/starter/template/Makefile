# @@NAME@@
.PHONY: @@PHONY@@

install:   ## @@INSTALL_WHAT@@
	uv sync
@@if ui@@
	cd web && npm install
@@end@@
@@if compose@@

services:  ## What it keeps things in (compose.yaml), for development
	docker compose up -d --wait
@@end@@

api:       ## The agent server on http://127.0.0.1:8000
	uv run main.py
@@if ui@@

web:       ## The UI with hot reload on http://localhost:5173 (sends /api to the server)
	cd web && npm run dev

build:     ## The UI, built into web/dist for the server to serve
	cd web && npm run build

run: build api
@@end@@
@@if !ui@@

run: api
@@end@@

docker:    ## One image: @@DOCKER_WHAT@@
	docker build -t @@SLUG@@ .
