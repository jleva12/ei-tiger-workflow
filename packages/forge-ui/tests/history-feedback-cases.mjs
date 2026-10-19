import assert from "node:assert/strict"
import { test } from "node:test"

export function historyFeedbackTests({ createHistoryFeedback, historyErrorMessage }) {
  const deferred = () => {
    let resolve, reject
    const promise = new Promise((ok, no) => { resolve = ok; reject = no })
    return { promise, resolve, reject }
  }
  test("failed initial history is visible, never replaced by an empty snapshot, and blocks sends until retry succeeds", async () => {
    const feedback = createHistoryFeedback()
    let fail = true
    const snapshot = { messages: [{ id: "kept" }] }
    const load = feedback.wrapLoad(async () => { if (fail) throw new Error("Failed to load conversation: 503"); return snapshot })
    await assert.rejects(load("s1"), /503/)
    assert.match(feedback.getSnapshot().entries.get("s1").error, /couldn't be loaded/)
    assert.throws(() => feedback.assertReady("s1"), /Retry loading/)
    feedback.assertReady("s2")
    fail = false
    assert.equal(await load("s1"), snapshot)
    assert.deepEqual(feedback.getSnapshot().entries.get("s1"), { loading: false, loaded: true })
    feedback.assertReady("s1")
  })
  test("a failed refresh preserves usable history and a retryable error", async () => {
    const feedback = createHistoryFeedback()
    await feedback.wrapLoad(async () => ({ messages: [{ id: "kept" }] }))("s1")
    await assert.rejects(feedback.wrapLoad(async () => { throw new Error("offline") })("s1"))
    assert.equal(feedback.getSnapshot().entries.get("s1").loaded, true)
    assert.match(feedback.getSnapshot().entries.get("s1").error, /couldn't be loaded/)
    feedback.assertReady("s1")
  })
  test("switching conversations aborts without a false failure and stale completions cannot replace newer results", async () => {
    const feedback = createHistoryFeedback()
    const first = deferred(), second = deferred()
    const controller = new AbortController()
    const old = feedback.wrapLoad(() => first.promise)("s1", { signal: controller.signal })
    const latest = feedback.wrapLoad(() => second.promise)("s1")
    second.resolve({ messages: [] })
    await latest
    controller.abort()
    first.reject(new Error("aborted"))
    await assert.rejects(old)
    assert.deepEqual(feedback.getSnapshot().entries.get("s1"), { loading: false, loaded: true })
  })
  test("aborting an initial load restores no error and preserves an earlier refresh result", async () => {
    for (const loaded of [false, true]) {
      const feedback = createHistoryFeedback()
      if (loaded) await feedback.wrapLoad(async () => ({ messages: [] }))("s1")
      const controller = new AbortController()
      const pending = deferred()
      const work = feedback.wrapLoad(() => pending.promise)("s1", { signal: controller.signal })
      controller.abort()
      pending.reject(new Error("Aborted"))
      await assert.rejects(work)
      const entry = feedback.getSnapshot().entries.get("s1")
      assert.equal(entry?.error, undefined)
      assert.equal(entry?.loading ?? false, false)
      feedback.assertReady("s1")
    }
  })
  test("failed navigation has feedback, retries clear it, and failures from abandoned navigation are ignored", async () => {
    const feedback = createHistoryFeedback()
    feedback.select("s1")
    let fail = true
    const adapter = feedback.wrapAdapter({ fetch: async (id) => { if (fail) throw new Error("Session not found: 404"); return { remoteId: id } } })
    await assert.rejects(adapter.fetch("s2"))
    assert.match(feedback.getSnapshot().navigation.error, /no longer available/)
    fail = false
    await adapter.fetch("s2")
    assert.equal(feedback.getSnapshot().navigation, undefined)
    const pending = deferred()
    const work = feedback.wrapAdapter({ fetch: () => pending.promise }).fetch("s3")
    feedback.select("s4")
    pending.reject(new Error("old failure"))
    await assert.rejects(work)
    assert.equal(feedback.getSnapshot().navigation, undefined)
  })
  test("history feedback distinguishes authentication and access failures without exposing raw response details", () => {
    assert.match(historyErrorMessage(new Error("Failed to load: 401"), "load"), /Sign in again/)
    assert.match(historyErrorMessage({ status: 403 }, "load"), /permission/)
    const message = historyErrorMessage(new Error("postgresql://secret:password@internal/db"), "list")
    assert.match(message, /conversations couldn't be loaded/)
    assert.doesNotMatch(message, /secret|postgresql/)
  })
}
