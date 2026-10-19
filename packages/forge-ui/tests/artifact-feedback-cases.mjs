import assert from "node:assert/strict"
import { test } from "node:test"

export function testArtifactFeedback({ createArtifactFeedback }) {
  const defer = () => {
    let resolve
    const promise = new Promise((done) => {
      resolve = done
    })
    return { promise, resolve }
  }
  test("artifact list failures stay visible and retry updates saved files", async () => {
    let fail = true
    const feedback = createArtifactFeedback({
      sessionId: "session",
      api: {
        list: async () => {
          if (fail) throw new Error("HTTP 503")
          return ["file.txt", "file.txt"]
        },
        load: async () => ({ text: "file" }),
      },
      download() {},
    })
    await feedback.refresh()
    assert.equal(feedback.getSnapshot().loading, false)
    assert.match(feedback.getSnapshot().error.message, /couldn't be loaded/)
    fail = false
    await feedback.retry()
    assert.deepEqual(feedback.getSnapshot().names, ["file.txt"])
    assert.equal(feedback.getSnapshot().error, undefined)
  })
  test("artifact load and browser download failures are caught and retryable", async () => {
    let failLoad = true
    let failDownload = false
    let downloaded = 0
    const feedback = createArtifactFeedback({
      sessionId: "session",
      api: {
        list: async () => [],
        load: async () => {
          if (failLoad) throw new Error("HTTP 403")
          return { text: "file" }
        },
      },
      download() {
        if (failDownload) throw new Error("Invalid base64")
        downloaded++
      },
    })
    await feedback.open("file.txt")
    assert.equal(feedback.getSnapshot().error.action, "download")
    assert.equal(feedback.getSnapshot().pending, undefined)
    failLoad = false
    failDownload = true
    await feedback.retry()
    assert.match(feedback.getSnapshot().error.message, /file.txt/)
    failDownload = false
    await feedback.retry()
    assert.equal(downloaded, 1)
    assert.equal(feedback.getSnapshot().error, undefined)
  })
  test("custom artifact open callbacks surface both synchronous and asynchronous failures", async () => {
    for (const onOpenArtifact of [
      () => {
        throw new Error("sync")
      },
      async () => {
        throw new Error("async")
      },
    ]) {
      const feedback = createArtifactFeedback({ onOpenArtifact, download() {} })
      await feedback.open("file.txt")
      assert.equal(feedback.getSnapshot().error.action, "download")
    }
  })
  test("switching conversations prevents a late file load from downloading", async () => {
    const waiting = defer()
    let downloaded = false
    const feedback = createArtifactFeedback({
      sessionId: "old",
      api: { list: async () => [], load: () => waiting.promise },
      download() {
        downloaded = true
      },
    })
    const pending = feedback.open("file.txt")
    assert.equal(feedback.getSnapshot().pending, "file.txt")
    feedback.cancelPending()
    waiting.resolve({ text: "old file" })
    await pending
    assert.equal(downloaded, false)
  })
  test("duplicate clicks do not start parallel artifact requests", async () => {
    const waiting = defer()
    let calls = 0
    const feedback = createArtifactFeedback({
      sessionId: "session",
      api: {
        list: async () => [],
        load: () => {
          calls++
          return waiting.promise
        },
      },
      download() {},
    })
    const pending = feedback.open("file.txt")
    await feedback.open("file.txt")
    assert.equal(calls, 1)
    waiting.resolve({ text: "file" })
    await pending
    assert.equal(feedback.getSnapshot().pending, undefined)
  })
  test("malformed artifact lists show the same recoverable error", async () => {
    for (const names of [null, {}, [null], [""]]) {
      const feedback = createArtifactFeedback({
        sessionId: "session",
        api: { list: async () => names, load: async () => ({}) },
        download() {},
      })
      await feedback.refresh()
      assert.equal(feedback.getSnapshot().error.action, "list")
    }
  })
}
