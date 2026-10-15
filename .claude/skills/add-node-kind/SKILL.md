---
name: add-node-kind
description: How to add a new kind of node to Forge's builders — a workflow node or step (an action, logic step, agent or person step on the workflow canvas) or a tool kind on the Agents canvas (which workflow LLM agents may use too). The kinds are served from backend node catalogs (node_kinds.json) and wired into the ADK runtime and the web console's forms. Use this skill whenever someone wants to add, rename, regroup, reorder or remove a workflow node kind, a step type, an Agents canvas node or an LLM tool kind, change what a new node starts with or its ways out, or asks how node kinds are wired between backend and frontend — even if they only say "add a Slack step", "new node", "new tool type" or "new block".
---

# Adding a node kind

Node kinds are described once, in a **node catalog** the admin API serves,
then implemented on each side: the backend runs them, the web console sets
them up. There are two catalogs:

| Builder | Catalog (the one source) | Served at | Backend builds it in |
| --- | --- | --- | --- |
| Workflows | `packages/python/adk-workflows/src/forge_task_adk_workflows/graph/node_kinds.json` (`graph/catalog.py`) | `GET /api/v1/workflow-node-kinds` | `graph/factories/` (`FACTORIES`) |
| Agents canvas + LLM tools | `packages/python/agent-runtime/src/forge_agent_runtime/node_kinds.json` (`catalog.py`) | `GET /api/v1/chat-agent-node-kinds` | `forge_agent_runtime/build.py` (`_Builder.tool`) |

Both routes are in `apps/forge-admin-api/src/forge_admin/api/routes/node_kinds.py`
and serve the JSON as it is: adding a kind needs no route change.

**What the catalog owns, and nothing else does:** the label, summary,
keywords, library group, library order (the file's order), icon name, ID
prefix, ways out and the settings a new node starts with (plus, for the
Agents canvas, `handed_to` and `workflow_tool`). Never copy these into
TypeScript or Python constants: the web console reads them through
`kindInfo()` / `chatKindInfo()`, and the backend derives its kind sets from
the catalog (`graph.outputs_of`, `document.KINDS` / `AGENT_LIKE` /
`HANDED_TO` / `DEFAULTS`, both `TOOL_KINDS`, the instruction writer's
`ToolKind`).

**What each side still owns:** the backend has the settings model and the
code that runs the node. The web console has the TypeScript settings type,
the settings form, validation, autocomplete typing, the card text and the
icon component. The console only offers a kind that is both served **and**
in its `SET_UP` list, so do both sides in one change. The tests below fail
until they agree.

## A workflow node kind

Work in this order. Optional steps are marked; skip what doesn't apply.

### 1. The catalog entry

Add an object to `graph/node_kinds.json` where it should sit in the library:

```json
{
  "kind": "slack_message",
  "label": "Slack message",
  "group": "actions",
  "icon": "slack",
  "summary": "Posts a message to a Slack channel.",
  "keywords": "slack chat notify post message channel",
  "id_prefix": "slack",
  "outputs": [
    { "id": "success", "label": "Success", "branch": true },
    { "id": "error", "label": "Error", "branch": true, "fallback": true }
  ],
  "defaults": { "channel": "", "text": "", "credential": "" }
}
```

- `group` is one of the `groups` at the top of the file: `start`, `agents`, `people`, `actions`, `github`, `logic` or `finish`. A new group goes in that list, in library order. Give it a glyph tint in `kindTones` in `adk-workflows/components/agent-ui.ts`, and add it to the tests that list the groups (`tests/agent-catalog.test.mjs`, `apps/forge-admin-api/tests/api/test_node_kinds.py`).
- `icon` is a Hugeicons name (`robot-01`, `globe-02`); step 3.3 maps it.
- `outputs` are its ways out, in canvas order. A node that just goes on has `[{ "id": "next", "label": "Next" }]`, and an ending has `[]`. `branch` marks a named way, `fallback` the way for everything else, and `body` a loop's body way.
- `rows` is for per-row ways out before the fixed ones, as Switch and Match have: `{ "setting": "cases", "label": "value", "empty": "Empty value" }`. `{n}` in `empty` is the row's number. Default rows need fixed IDs (`case_1`), which only have to be unique within the node.
  - With `"ways": [{ "id": "accepted", … }, { "id": "rejected", … }]`, each row has several ways out, `<row id>:<way id>` (a Decision board's choices). One ADK node can only take its ways once, so a kind whose rows go on separately is built as hidden nodes: see `board_parts` in `factories/people.py` and `BOARD` in `graph.py` / `steps.py`.
- Optional flags: `terminal` (drawn as a start or finish), `person` (a person decides here) and `orb` (agent orb 0–4).
- `defaults` must fit the backend settings model. The catalog test checks this.

### 2. Backend (`packages/python/adk-workflows`)

1. **Settings model:** add a class to `support/step_settings.py`, e.g. `SlackMessageConfig(_Config)`. Its field defaults say what a *missing* key means at run time; the catalog's `defaults` say what a *new* node starts with.
2. **Factory:** add `def slack_message(node, ctx: BuildContext) -> BaseNode` in the fitting `graph/factories/` module (`actions.py`, `logic.py`, `people.py`, `agents.py`).
   - Read its settings once with `settings_of(node, SlackMessageConfig)`.
   - A branching node emits the route of the way it takes, using the IDs in its catalog `outputs`.
   - Copy the pattern of a neighbour such as `http` or `bash`.
3. **Register it:** add it to `FACTORIES` in `graph/factories/__init__.py`, **in the same position as in the JSON**. The test compares the two lists in order.
4. **Optional, in `graph/graph.py` / `graph/names.py` / `config.py`:**
   - It can pause a run: add it to `PAUSING`.
   - It runs a loop body: add it to `BODY_KINDS`.
   - It works in the run's folder: add it to `FOLDER_KINDS`.
   - It needs a worker opt-in, like Bash or the Pi agent: add a setting to `config.py`, and refuse at run time when it's off.
5. **Tests:**
   - Add `"slack_message": SlackMessageConfig` to `MODELS` in `tests/test_catalog.py`.
   - Add the kind to the list in `test_every_kind_has_a_factory` in `tests/test_graph.py`.
   - Add behaviour tests: in `tests/test_graph.py`, or a file of their own for a family of kinds (`tests/test_github_steps.py` runs real git against a local bare repository).

### 3. Frontend (`apps/forge-web/src/features`)

1. **Types:** add the settings type to `StepConfigs` in `steps/lib/model.ts`, and the kind to `FORGE_KINDS` in `adk-workflows/lib/model.ts`. An agent-style kind (like `pi_agent`) goes straight into `AgentConfigs` there instead.
2. **Set it up:** add `slack_message: true` to `SET_UP` in `adk-workflows/lib/catalog.ts`. Without it, the served kind is left out of the library.
3. **Icon:** if the catalog's `icon` name isn't in `ICONS` in `builder/lib/catalog.ts`, import it from `@hugeicons/core-free-icons` and add it there. Otherwise the node shows a plain square.
4. **Settings form:** write a `SlackMessageFields` component and add its `case` to `StepFields` in `adk-workflows/components/step-fields.tsx`. An agent-style kind goes in `kindFields` in `agent-fields.tsx`. Follow the forge-ui skill for the form controls.
5. **Validation:** add a `settingsIssues` case in `steps/lib/validate.ts` (or `adk-workflows/lib/validate.ts` for an agent-style kind) for what must be filled in. Add each issue code's setting to that file's `FIELDS` map, so the form highlights the field. A kind with a `credential` also goes through `credentialIssues` in `settingsIssues` in `adk-workflows/lib/validate.ts`, as HTTP and the GitHub steps do (a deleted credential, nobody signed in, a URL it isn't sent to).
6. **Templated settings:** for `{{ }}` templates and JSONata expressions, add a case in `steps/lib/fields.ts`. That gives them autocomplete and checks.
7. **What it hands on:** add a case to `outputTypeOf` in `steps/lib/scope.ts`. That lets later nodes autocomplete `steps.<id>.output` and `previous`.
8. **Card text:** add its detail and summary lines in `adk-workflows/components/step-lines.ts`.
9. **Optional, edge colours:** if it branches into good and bad ways out, add it to `toneOf` in `steps/lib/connections.ts`.
10. **Optional, a folder of the run's:** if it works in the run's folder, add it to `FOLDER_KINDS` in `adk-workflows/lib/validate.ts` (as well as the backend's), and to `FOLDER_SETTINGS` in `adk-workflows/lib/folders.ts` (its folder setting, and the output it hands the folder on as). Then put `<FolderPlace>` (`adk-workflows/components/folder-place.tsx`) under its folder field. That shows where it works, from the run's folder, and offers earlier steps' folders a click each.
11. **JSON Schema:** add its settings schema to `CONFIGS` in `steps/lib/schema.ts`, and its "what it hands on" sentence to `HANDS_ON` in `adk-workflows/lib/schema.ts`. Then run `npm run generate:agent-schema` in `apps/forge-web`. The admin API checks every saved workflow against the generated `agent.schema.json`, so a stale file makes saves fail.

## An Agents canvas tool kind

The same shape, against the agent runtime's catalog:

1. **Catalog:** add an entry to `forge_agent_runtime/node_kinds.json` with `"group": "tools"` and `"outputs": []`.
   - Add `"workflow_tool": true` if a workflow's LLM agents may have it too. It then appears in their "Add a tool" menu.
   - Add `"handed_to": true` only for agents. `outputs` with `tools` / `agents` ways make a kind agent-like.
2. **Backend:**
   - Add a `case` in `_Builder.tool()` in `forge_agent_runtime/build.py`.
   - If workflows need a check before building it, add one to `_tools()` in `adk-workflows/.../graph/factories/agents.py`.
   - Describe it for the instruction writer with a `case` in `_tool_lines` in `apps/forge-admin-api/src/forge_admin/instruction_writer/brief.py`.
   - Extend the runtime's `tests/test_catalog.py` expectations if they list kinds.
3. **Frontend:**
   - **Types and setup:** add the settings type to `ChatConfigs` in `agents/lib/model.ts`, and the kind to `SET_UP` in `agents/lib/catalog.ts`.
   - **Icon:** add it to `ICONS` as for workflows.
   - **Form:** add its settings to `agents/components/tool-fields.tsx`. Both the canvas and workflow LLM agents use it.
   - **Validation:** add it to `agents/lib/validate.ts`, and to `adk-workflows/lib/validate.ts` if workflow tools need checks.
   - **Card lines:** add them in `agents/components/chat-agent-ui.ts`, and in `toolLine` in `adk-workflows/components/agent-lines.ts` for workflow tool rows.
   - **Schema:** add it to `CONFIGS` in `agents/lib/schema.ts`.
   - **Workflow tools:** if `workflow_tool` is true, also add it to `LLM_TOOL_KINDS` in `adk-workflows/lib/model.ts`.
   - Then run `npm run generate:agent-schema`.

## Checks that catch a missed step

| Check | Catches |
| --- | --- |
| `uv run pytest tests/test_catalog.py` in `packages/python/adk-workflows` | the JSON and `FACTORIES` differ (in order), or `defaults` don't fit the settings model |
| `uv run pytest tests/test_catalog.py` in `packages/python/agent-runtime` | the runtime's kind sets, ways out or tool kinds drift |
| `node --test tests/agent-catalog.test.mjs` in `apps/forge-web` | served kinds ≠ `SET_UP` / `KNOWN_*`, or workflow tools ≠ `LLM_TOOL_KINDS` |
| `npx tsc -b` in `apps/forge-web` | a `Record<AgentKind, …>` table without the new kind (`SET_UP`, `HANDS_ON`, `CONFIGS`) |
| `npm run check` in `apps/forge-web` | a stale `agent.schema.json` / `chat_agent.schema.json`, lint, types |
| `make async-worker-check`, `make agent-runtime-check`, `make admin-check` | each Python package's lint, types and tests |

Web tests run without a server: `tests/node-catalog.mjs` installs both
catalogs from the JSON files. In the app, pages that read kinds wait for
them through `NodeCatalogGate` / `useNodeCatalogs()` in
`adk-workflows/lib/api.ts`. A new page that reads kinds must sit under the
gate, or it throws "the … node catalog isn't loaded".

## Changing or removing a kind

- **Rename, regroup, reorder or re-describe:** edit the JSON only, then run `npm run generate:agent-schema`, since the kinds' summaries appear in the generated schemas.
- **Change what a new node starts with:** edit `defaults` in the JSON. Change the settings model's defaults only if a missing key should mean something else at run time.
- **Remove:** delete the JSON entry and its factory or `case` together.
  - The console drops the kind's nodes when it reads an existing workflow, with a note. Check whether saved workflows still use it before removing.
  - Keep `SET_UP` and the TypeScript type in step with the catalog, or `agent-catalog.test.mjs` fails.
