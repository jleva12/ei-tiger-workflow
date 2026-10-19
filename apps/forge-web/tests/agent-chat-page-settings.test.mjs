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
const {
  ACCENTS,
  PAGE_DEFAULTS,
  accentInk,
  changedSettings,
  cleanedPrompts,
  isAccent,
  pageSettingsOf,
  pageTitleOf,
  resolvedTheme,
} = await server.ssrLoadModule("/src/features/agents/lib/page-settings.ts")
await server.close()

test("a page without a setting (an older page or server) has its default", () => {
  assert.deepEqual(pageSettingsOf(null), PAGE_DEFAULTS)
  const older = pageSettingsOf({
    token: "cp_x",
    suggestions: ["Hi"],
    hide_history: true,
  })
  assert.deepEqual(older.suggestions, ["Hi"])
  assert.equal(older.hide_history, true)
  assert.equal(older.follow_ups, true)
  assert.equal(older.theme, "light")
  // Visitors can switch the theme until the page turns it off; the page
  // offers the desktop app only once it's turned on.
  assert.equal(older.theme_toggle, true)
  assert.equal(older.desktop_link, false)
  assert.equal(older.accent, null)
  // Only settings: not the page's link or who changed it.
  assert.equal("token" in older, false)
})

test("saving sends only what changed", () => {
  const saved = pageSettingsOf({ suggestions: ["Hi"], accent: "#2563eb" })
  assert.deepEqual(changedSettings(saved, saved), {})
  assert.deepEqual(
    changedSettings(saved, {
      ...saved,
      suggestions: ["Hi"],
      theme: "dark",
      accent: null,
      footer_note: "Answers can be wrong.",
    }),
    { theme: "dark", accent: null, footer_note: "Answers can be wrong." }
  )
  assert.deepEqual(
    changedSettings(saved, { ...saved, suggestions: ["Hi", "More"] }),
    { suggestions: ["Hi", "More"] }
  )
})

test("prompts are saved trimmed, without blanks, each once", () => {
  assert.deepEqual(cleanedPrompts([" Hi ", "", "Track", "Hi", "  "]), [
    "Hi",
    "Track",
  ])
})

test("text on an accent is whichever of white and ink reads better", () => {
  assert.equal(accentInk("#2563eb"), "#ffffff")
  assert.equal(accentInk("#7c3aed"), "#ffffff")
  assert.equal(accentInk("#fde047"), "#15171c")
  assert.equal(accentInk("#ffffff"), "#15171c")
  assert.ok(ACCENTS.filter((a) => a.value).every((a) => isAccent(a.value)))
  assert.equal(isAccent("#abc"), false)
  assert.equal(isAccent("blue"), false)
})

test("the page's name and theme", () => {
  assert.equal(
    pageTitleOf({ title: "  " }, { name: "Order desk" }),
    "Order desk"
  )
  assert.equal(pageTitleOf({ title: "Acme" }, { name: "Order desk" }), "Acme")
  assert.equal(pageTitleOf({ title: "" }, { name: "" }), "Agent")
  assert.equal(resolvedTheme("system", "dark"), "dark")
  assert.equal(resolvedTheme("light", "dark"), "light")
})
