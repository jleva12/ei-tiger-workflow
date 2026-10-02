import assert from "node:assert/strict"
import { test } from "node:test"
import type { ThreadMessage } from "@assistant-ui/react"
import { activityForTurn } from "../src/components/forge/agent-workspace/lib/activity.ts"

const user = (id: string) =>
  ({ id, role: "user", content: [{ type: "text", text: id }] }) as ThreadMessage
const reply = (
  id: string,
  content: unknown[],
  status = { type: "complete", reason: "stop" }
) => ({ id, role: "assistant", content, status }) as ThreadMessage
const tool = (result?: unknown, name = "calculate") => ({
  type: "tool-call",
  toolName: name,
  toolCallId: "call",
  args: {},
  argsText: "{}",
  ...(result === undefined ? {} : { result }),
})

test("a cancelled stream never implies that a pending tool completed", () => {
  const result = activityForTurn(
    [
      user("one"),
      reply("a", [tool()], { type: "incomplete", reason: "cancelled" }),
    ],
    false
  )
  assert.equal(result.state, "stopped")
  assert.equal(result.items[0].state, "stopped")
})
test("structured tool errors are visible even when the message completed", () => {
  const result = activityForTurn(
    [
      user("one"),
      reply("a", [
        tool(JSON.stringify({ result: { error: "Invalid expression" } })),
      ]),
    ],
    false
  )
  assert.equal(result.state, "error")
  assert.equal(result.items[0].state, "error")
})
test("approval requests remain waiting and do not show a success mark", () => {
  const result = activityForTurn(
    [user("one"), reply("a", [{ ...tool(), approval: { id: "gate" } }])],
    true
  )
  assert.equal(result.state, "waiting")
})
test("a new turn does not inherit old tools or completed plans", () => {
  const result = activityForTurn(
    [
      user("one"),
      reply("a", [tool({ plan: {} }, "set_plan")]),
      user("two"),
      reply("b", []),
    ],
    true
  )
  assert.equal(result.hasPlanActivity, false)
  assert.deepEqual(result.items, [])
  assert.equal(result.state, "running")
})
test("interim results remain running until the tool settles", () => {
  const result = activityForTurn(
    [
      user("one"),
      reply("a", [{ ...tool({ progress: 25 }), isPreliminary: true }]),
    ],
    true
  )
  assert.equal(result.items[0].state, "running")
})
