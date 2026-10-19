import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { desktopLinkOf, parseDesktopLink, serverOf, serverOfRuntime } =
  await server.ssrLoadModule("/src/features/agents/lib/desktop-link.ts")
const { parseChatLink } = await server.ssrLoadModule("/src/desktop/links.ts")
await server.close()

const TOKEN = "cp_1ax238t03pruc4o2lwx8jiov"
const API = "https://forge.example.com/api/v1"

test("a page's desktop link names its API and token, and reads back", () => {
  const link = desktopLinkOf({ server: API, token: TOKEN })
  assert.equal(
    link,
    `forge-agents://add?server=${encodeURIComponent(API)}&token=${TOKEN}`
  )
  assert.deepEqual(parseDesktopLink(link), { server: API, token: TOKEN })
  // As some systems hand it over: a path, not a host; any case of "add".
  assert.deepEqual(
    parseDesktopLink(`forge-agents:/ADD?server=${API}&token=${TOKEN}`),
    { server: API, token: TOKEN }
  )
})

test("a desktop link that isn't the app's, or not of the right shape, offers nothing", () => {
  const wrong = [
    // Another scheme or action.
    `https://forge.example.com/chat/${TOKEN}`,
    `forge-agent://add?server=${API}&token=${TOKEN}`,
    `forge-agents://remove?server=${API}&token=${TOKEN}`,
    // A token not as the API makes them.
    `forge-agents://add?server=${API}&token=cp_short`,
    `forge-agents://add?server=${API}&token=${TOKEN.toUpperCase()}`,
    `forge-agents://add?server=${API}&token=${TOKEN}x`,
    // A server that isn't http(s), is relative, or carries more than an address.
    `forge-agents://add?server=javascript:alert(1)&token=${TOKEN}`,
    `forge-agents://add?server=file:///etc&token=${TOKEN}`,
    `forge-agents://add?server=/api/v1&token=${TOKEN}`,
    `forge-agents://add?server=${encodeURIComponent("https://me:pw@evil.example/api")}&token=${TOKEN}`,
    `forge-agents://add?server=${encodeURIComponent(`${API}?x=1`)}&token=${TOKEN}`,
    `forge-agents://add?token=${TOKEN}`,
    "not a link",
  ]
  for (const link of wrong)
    assert.equal(parseDesktopLink(link), undefined, link)
})

test("a runtime's API is its address without /runtime, made absolute", () => {
  assert.equal(serverOfRuntime(`${API}/runtime`, "https://x.example"), API)
  // The console's relative /api/v1 (its dev server forwards it).
  assert.equal(
    serverOfRuntime("/api/v1/runtime", "http://localhost:5190"),
    "http://localhost:5190/api/v1"
  )
  assert.equal(serverOf(`${API}/`), API)
  assert.equal(serverOf("ftp://forge.example.com"), undefined)
})

test("the add dialog takes the app's own link, with its server", () => {
  assert.deepEqual(
    parseChatLink(desktopLinkOf({ server: API, token: TOKEN })),
    { server: API, token: TOKEN }
  )
  // A runtime link names its server; a console link doesn't.
  assert.deepEqual(parseChatLink(`${API}/runtime/pages/${TOKEN}`), {
    server: API,
    token: TOKEN,
  })
  assert.deepEqual(parseChatLink(`https://console.example/chat/${TOKEN}`), {
    token: TOKEN,
  })
  assert.equal(parseChatLink("https://console.example/chat/"), undefined)
})
