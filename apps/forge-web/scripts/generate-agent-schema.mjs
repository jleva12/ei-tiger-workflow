// Writes the agent format's JSON Schema (src/lib/agents/schema.ts) to the
// admin API, which checks every agent it saves against it. `--check` only
// compares, and fails when the admin API's copy is out of date.
//
//   npm run generate:agent-schema
import { readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const target = path.resolve(root, "../forge-admin-api/src/forge_admin/agent.schema.json")

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
let schema
try {
  schema = (await server.ssrLoadModule("/src/lib/agents/schema.ts")).AGENT_JSON_SCHEMA
} finally {
  await server.close()
}

const text = `${JSON.stringify(schema, null, 2)}\n`
if (process.argv.includes("--check")) {
  const current = await readFile(target, "utf8").catch(() => "")
  if (current !== text) {
    console.error(`${path.relative(process.cwd(), target)} is out of date: run npm run generate:agent-schema`)
    process.exit(1)
  }
  console.log("The admin API's agent schema is up to date.")
} else {
  await writeFile(target, text)
  console.log(`Wrote ${path.relative(process.cwd(), target)}`)
}
