// Writes the formats' JSON Schemas where the Python side checks documents
// against them: the workflow format's (src/features/adk-workflows/lib/schema.ts)
// to the admin API, and the chat agent format's (src/features/agents/lib/schema.ts)
// to the agent runtime package. `--check` only compares, and fails when a
// copy is out of date.
//
//   npm run generate:agent-schema
import { readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const TARGETS = [
  {
    module: "/src/features/adk-workflows/lib/schema.ts",
    name: "AGENT_JSON_SCHEMA",
    file: path.resolve(root, "../forge-admin-api/src/forge_admin/adk_workflows/agent.schema.json"),
  },
  {
    module: "/src/features/agents/lib/schema.ts",
    name: "CHAT_AGENT_JSON_SCHEMA",
    file: path.resolve(
      root,
      "../../packages/python/agent-runtime/src/forge_agent_runtime/chat_agent.schema.json"
    ),
  },
]

// Load the TypeScript module as the app sees it, without the app's plugins,
// in a cache of its own (a running dev server's is never touched).
const server = await createServer({
  configFile: false,
  root,
  cacheDir: path.resolve(root, "node_modules/.vite-agent-schema"),
  logLevel: "error",
  appType: "custom",
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false, watch: null },
  resolve: { alias: { "@": path.resolve(root, "src") } },
})
const schemas = []
try {
  for (const target of TARGETS) {
    schemas.push({ ...target, schema: (await server.ssrLoadModule(target.module))[target.name] })
  }
} finally {
  await server.close()
}

let stale = false
for (const { file, schema } of schemas) {
  const text = `${JSON.stringify(schema, null, 2)}\n`
  const shown = path.relative(process.cwd(), file)
  if (process.argv.includes("--check")) {
    const current = await readFile(file, "utf8").catch(() => "")
    if (current !== text) {
      console.error(`${shown} is out of date: run npm run generate:agent-schema`)
      stale = true
    }
  } else {
    await writeFile(file, text)
    console.log(`Wrote ${shown}`)
  }
}
if (stale) process.exit(1)
if (process.argv.includes("--check")) console.log("The JSON Schemas are up to date.")
