// Writes the workflow format's JSON Schema (src/lib/workflows/schema.ts)
// to the admin API, which checks every workflow it saves against it, and
// the step catalog (src/lib/workflows/model.ts, scope.ts, fields.ts), from
// which the assistant drafts workflows: each kind's settings and defaults,
// which settings are expressions or templates, its ways out, and what it
// hands on. `--check` only compares, and fails when either copy is out of
// date.
//
//   npm run generate:workflow-schema
import { readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const admin = path.resolve(root, "../forge-admin-api/src/forge_admin")
const targets = {
  schema: path.join(admin, "workflow.schema.json"),
  catalog: path.join(admin, "workflow.catalog.json"),
}

// What a kind hands on when that depends on how it's set up.
const HANDS_ON = {
  entry: "The run's input, as its input_schema declares.",
  agent: 'Its answer as text; with output "json", the object its output_schema declares.',
  transform: "What its expression makes; with an output_schema, that shape.",
  subworkflow: "With wait, the other workflow's result; without, {run_id}.",
  merge: "An object keyed by the IDs of the steps that came into it, each holding what that step handed on.",
  if: "{branch}; later steps' previous is what came into it.",
  switch: "{branch}; later steps' previous is what came into it.",
  match: "{branch}; later steps' previous is what came into it.",
  end: "Nothing: it ends the run, whose result is its result expression.",
}

// Load the TypeScript modules as the app sees them, without the app's
// plugins. Their own cache, and no dependency pre-bundling: a running dev
// server's cache (node_modules/.vite) is never touched.
const server = await createServer({
  configFile: false,
  root,
  cacheDir: path.resolve(root, "node_modules/.vite-workflow-schema"),
  logLevel: "error",
  appType: "custom",
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false, watch: null },
  resolve: { alias: { "@": path.resolve(root, "src") } },
})
let schema
let catalog
try {
  schema = (await server.ssrLoadModule("/src/lib/workflows/schema.ts")).WORKFLOW_JSON_SCHEMA
  catalog = stepCatalog(
    await server.ssrLoadModule("/src/lib/workflows/model.ts"),
    await server.ssrLoadModule("/src/lib/workflows/scope.ts"),
    await server.ssrLoadModule("/src/lib/workflows/fields.ts"),
    await server.ssrLoadModule("/src/lib/workflows/types.ts"),
    schema
  )
} finally {
  await server.close()
}

/** A data type as a compact shape: fields to their type and description. */
function shape(type, label, depth = 0) {
  const note = type.description ? ` — ${type.description}` : ""
  switch (type.kind) {
    case "object":
      if (depth > 4 || !Object.keys(type.properties).length) return `object${note}`
      return Object.fromEntries(
        Object.entries(type.properties).map(([name, field]) => [name, shape(field, label, depth + 1)])
      )
    case "array":
      return [shape(type.items, label, depth + 1)]
    default:
      return `${label(type)}${note}`
  }
}

function stepCatalog(model, scope, fields, types, workflowSchema) {
  const kinds = {}
  for (const kind of model.STEP_KIND_LIST) {
    const info = model.STEP_KINDS[kind]
    const step = model.newStep(kind)
    // A switch's cases and a match's rules are made with random IDs: none here.
    const defaults = { ...step.config }
    if (kind === "switch") defaults.cases = []
    if (kind === "match") defaults.arms = []
    const properties = workflowSchema.$defs[`config_${kind}`].properties
    const settings = Object.fromEntries(
      Object.entries(properties).map(([name, spec]) => [name, spec.description ?? ""])
    )
    // Which settings read the run's data, and how they're written; a list's
    // items by their field.
    const reads = {}
    for (const setting of fields.expressionSettings(step)) {
      // A list item's key carries its random ID: the list's entry below says it.
      if (!setting.key.includes(".")) reads[setting.key] = setting.mode
    }
    if (kind === "http") reads["headers[].value"] = "template"
    if (kind === "match") reads["arms[].condition"] = "expression"
    const outputs =
      kind === "switch"
        ? { each: "cases[].id, one way out per case", then: ["default"] }
        : kind === "match"
          ? { each: "arms[].id, one way out per rule", then: ["otherwise"] }
          : model.outputsOf(step).map((output) => output.id)
    const handsOn = scope.outputTypeOf(step, {
      input: types.t.unknown("The run's input."),
      incoming: new Map(),
    })
    kinds[kind] = {
      label: info.label,
      summary: info.summary,
      id_prefix: info.idPrefix,
      settings,
      defaults,
      reads,
      outputs,
      hands_on: HANDS_ON[kind] ?? shape(handsOn, types.typeLabel),
    }
  }
  return {
    $comment:
      "GENERATED by apps/forge-web/scripts/generate-workflow-schema.mjs from src/lib/workflows: don't edit it by hand.",
    format: "forge.workflow/v1",
    kinds,
  }
}

const texts = {
  schema: `${JSON.stringify(schema, null, 2)}\n`,
  catalog: `${JSON.stringify(catalog, null, 2)}\n`,
}
if (process.argv.includes("--check")) {
  let stale = false
  for (const [name, target] of Object.entries(targets)) {
    const current = await readFile(target, "utf8").catch(() => "")
    if (current !== texts[name]) {
      console.error(
        `${path.relative(process.cwd(), target)} is out of date: run npm run generate:workflow-schema`
      )
      stale = true
    }
  }
  if (stale) process.exit(1)
  console.log("The admin API's workflow schema and step catalog are up to date.")
} else {
  for (const [name, target] of Object.entries(targets)) {
    await writeFile(target, texts[name])
    console.log(`Wrote ${path.relative(process.cwd(), target)}`)
  }
}
