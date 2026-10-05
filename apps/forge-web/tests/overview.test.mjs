import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { summarize, bySubject, weeksBySubject, inScope } = await server.ssrLoadModule(
  "/src/features/overview/lib/overview.ts"
)
const { sharedTicks, usd, compact } = await server.ssrLoadModule("/src/features/overview/lib/format.ts")
await server.close()

const usage = (bucket, subject, model, input, output, cost, more = {}) => ({
  bucket,
  subject,
  model,
  calls: 1,
  failed: 0,
  input,
  cached: Math.floor(input / 2),
  output,
  thinking: 0,
  cost,
  unpriced: 0,
  ...more,
})
const activity = (bucket, subject, started, succeeded, failed, more = {}) => ({
  bucket,
  subject,
  started,
  succeeded,
  failed,
  abandoned: 0,
  open: started - succeeded - failed,
  retried: 0,
  duration_ms: 0,
  timed: 0,
  ...more,
})

const overview = {
  period: "7d",
  unit: "day",
  time_zone: "UTC",
  as_of: "2026-10-07T15:00:00+00:00",
  buckets: Array.from({ length: 3 }, (_, i) => ({
    start: `2026-10-0${5 + i}T00:00:00+00:00`,
    end: `2026-10-0${6 + i}T00:00:00+00:00`,
  })),
  previous: { start: "2026-10-02T00:00:00+00:00", end: "2026-10-05T00:00:00+00:00" },
  weeks: [{ start: "2026-09-28T00:00:00+00:00", end: "2026-10-05T00:00:00+00:00" }, { start: "2026-10-05T00:00:00+00:00", end: "2026-10-07T15:00:00+00:00" }],
  subjects: [],
  members: [],
  usage: [
    usage(0, "workflow:wf", "openai/gpt", 1000, 100, 0.5),
    usage(2, "workflow:wf", "openai/gpt", 1000, 100, 0.5),
    usage(2, "agent:ca", "anthropic/claude", 3000, 300, 2),
    usage(-1, "agent:ca", "anthropic/claude", 500, 50, 0.25),
  ],
  activity: [
    activity(0, "workflow:wf", 4, 3, 1, { duration_ms: 8000, timed: 4 }),
    activity(2, "agent:ca", 10, 10, 0),
    activity(-1, "workflow:wf", 2, 1, 1),
  ],
  weekly: [
    { subject: "workflow:wf", week: 1, tokens: 2200, runs: 4 },
    { subject: "agent:ca", week: 0, tokens: 550, runs: 0 },
  ],
  steps: [],
  tools: [],
  people: [],
  waiting: [],
  failures: [],
  open: { queued: 0, running: 0, paused: 0, waiting: 0 },
  recording_since: null,
  truncated: false,
}

test("the organization sums everything, bucket by bucket and kind by kind", () => {
  const { current, previous, buckets, models, successRate } = summarize(overview, { level: "organization" })
  assert.equal(current.tokens, 1100 + 1100 + 3300)
  assert.equal(current.cost, 3)
  assert.equal(previous.tokens, 550)
  assert.equal(buckets[2].byKind.agent.tokens, 3300)
  assert.equal(buckets[2].byKind.workflow.tokens, 1100)
  assert.equal(buckets[0].bySubject["workflow:wf"].started, 4)
  assert.deepEqual(
    models.map((m) => [m.model, m.tokens]),
    [
      ["anthropic/claude", 3300],
      ["openai/gpt", 2200],
    ]
  )
  assert.equal(successRate, 13 / 14)
})

test("a kind or one workflow sums only its own", () => {
  const workflows = summarize(overview, { level: "kind", kind: "workflow" })
  assert.equal(workflows.current.tokens, 2200)
  assert.equal(workflows.successRate, 3 / 4)
  assert.equal(workflows.previousSuccessRate, 1 / 2)
  assert.equal(workflows.averageDurationMs, 2000)
  const agent = summarize(overview, { level: "subject", key: "agent:ca" })
  assert.equal(agent.current.started, 10)
  assert.equal(agent.previous.tokens, 550)
  assert.equal(inScope({ level: "kind", kind: "agent" }, "workflow:wf"), false)
})

test("each subject's totals and weeks", () => {
  const totals = bySubject(overview)
  assert.equal(totals.get("workflow:wf").current.started, 4)
  assert.equal(totals.get("workflow:wf").previous.started, 2)
  const weeks = weeksBySubject(overview, "tokens")
  assert.deepEqual(weeks.get("workflow:wf"), [0, 2200])
  assert.deepEqual(weeks.get("agent:ca"), [550, 0])
})

test("figures and ticks", () => {
  assert.equal(usd(0.004), "<$0.01")
  assert.equal(usd(3.5), "$3.50")
  assert.equal(usd(1234), "$1,234")
  assert.equal(compact(18_200_000), "18.2M")
  const days = Array.from({ length: 7 }, (_, i) => Date.UTC(2026, 9, 1 + i))
  // Every other day, counting back from today.
  assert.deepEqual(sharedTicks(days, "day"), [days[0], days[2], days[4], days[6]])
})
