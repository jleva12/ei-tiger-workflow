import assert from "node:assert/strict"
import { test } from "node:test"
import {
  FOLLOW_UPS_REQUEST_KEY,
  FOLLOW_UPS_STATE_KEY,
  followUpsOf,
  followUpsRequest,
} from "../src/components/forge/assistant/follow-ups.ts"

test("a run asks for follow-ups only when they're on, with a count it may ask for", () => {
  assert.equal(followUpsRequest(undefined), undefined)
  assert.equal(followUpsRequest(false), undefined)
  assert.deepEqual(followUpsRequest(true), { [FOLLOW_UPS_REQUEST_KEY]: true })
  assert.deepEqual(followUpsRequest({}), { [FOLLOW_UPS_REQUEST_KEY]: true })
  assert.deepEqual(followUpsRequest({ count: 2 }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 2 },
  })
  assert.deepEqual(followUpsRequest({ count: 12 }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 5 },
  })
  assert.deepEqual(followUpsRequest({ count: 0 }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 1 },
  })
  assert.deepEqual(followUpsRequest({ count: Number.NaN }), {
    [FOLLOW_UPS_REQUEST_KEY]: { count: 1 },
  })
})

test("the last reply's follow-ups are read from the session's state", () => {
  assert.deepEqual(
    followUpsOf({
      [FOLLOW_UPS_STATE_KEY]: { suggestions: ["Roll it back", "", " ", 3] },
    }),
    ["Roll it back"]
  )
  // Cleared when a turn starts, or never written.
  assert.deepEqual(followUpsOf({ [FOLLOW_UPS_STATE_KEY]: null }), [])
  assert.deepEqual(followUpsOf({}), [])
  assert.deepEqual(followUpsOf({ [FOLLOW_UPS_STATE_KEY]: "nonsense" }), [])
})
