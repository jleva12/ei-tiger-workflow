import assert from "node:assert/strict"
import { test } from "node:test"

export function testHistorySnapshot({
  snapshotOfAdkEvents,
  createAdkHistoryLoader,
  convertAdkMessage,
  convertExternalMessages,
}) {
  const key = "forge:turn:client"
  const reply = {
    id: "reply",
    invocationId: "turn1",
    author: "agent",
    content: { role: "model", parts: [{ text: "Retained answer" }] },
  }
  const error = {
    ...reply,
    errorCode: "MAX_TOKENS",
    errorMessage: "Output limit reached.",
  }
  const terminal = (status, extra = {}) => ({
    id: `terminal-${status}`,
    invocationId: "turn1",
    customMetadata: {
      "forge:turn": { version: 1, runId: "a".repeat(32), status, ...extra },
    },
  })
  const text = (snapshot) =>
    convertExternalMessages(snapshot.messages, convertAdkMessage, false, {})
      .filter((message) => message.role === "assistant")
      .flatMap((message) =>
        message.content
          .filter((part) => part.type === "text")
          .map((part) => part.text)
      )
      .join("\n")

  test("history preserves failed final content and the latest failure outcome", () => {
    const snapshot = snapshotOfAdkEvents([error])
    assert.match(text(snapshot), /Retained answer/)
    assert.match(text(snapshot), /Output limit reached/)
    assert.equal(snapshot.stateDelta[key].status, "failed")
    assert.equal(snapshot.stateDelta[key].errorCode, "MAX_TOKENS")
  })

  test("replaying final content plus error replaces earlier partial text exactly once", () => {
    const snapshot = snapshotOfAdkEvents([
      { ...reply, id: "partial", partial: true },
      error,
    ])
    assert.equal(text(snapshot).split("Retained answer").length - 1, 1)
    assert.equal(snapshot.stateDelta[key].status, "failed")
  })

  test("snake case and legacy errors normalize consistently with live replies", () => {
    const snapshot = snapshotOfAdkEvents([
      {
        ...reply,
        id: 12,
        invocationId: undefined,
        invocation_id: "turn1",
        error_code: "BLOCKED",
        error_message: "Blocked.",
      },
    ])
    assert.match(text(snapshot), /Retained answer/)
    assert.equal(snapshot.stateDelta[key].errorCode, "BLOCKED")
    assert.equal(
      snapshotOfAdkEvents([{ id: "legacy", error: "Legacy failure" }])
        .stateDelta[key].status,
      "failed"
    )
  })

  test("contentless length and filtering stops remain visible after reload", () => {
    for (const finishReason of ["MAX_TOKENS", "SAFETY"]) {
      const snapshot = snapshotOfAdkEvents([{ id: "error", finishReason }])
      assert.equal(snapshot.stateDelta[key].status, "failed")
      assert.match(text(snapshot), /limit|filters/)
    }
  })

  test("older failures do not poison a newer successful invocation or legacy user turn", () => {
    for (const events of [
      [error, { ...reply, id: "new", invocationId: "turn2" }],
      [
        { ...error, invocationId: undefined },
        {
          id: "user",
          author: "user",
          content: { parts: [{ text: "Try again" }] },
        },
        { ...reply, id: "new", invocationId: undefined },
      ],
    ])
      assert.equal(
        snapshotOfAdkEvents(events).stateDelta[key].status,
        "succeeded"
      )
  })

  test("an older failed turn remains marked after a later successful reply and SDK message merging", () => {
    const snapshot = snapshotOfAdkEvents([
      {
        ...reply,
        id: "child",
        author: "child",
        content: { parts: [{ text: "Child result" }] },
      },
      error,
      {
        id: "user2",
        author: "user",
        invocationId: "turn2",
        content: { parts: [{ text: "Try again" }] },
      },
      { ...reply, id: "success", invocationId: "turn2" },
    ])
    const converted = convertExternalMessages(
      snapshot.messages,
      convertAdkMessage,
      false,
      {}
    )
    assert.equal(converted[0].status.type, "incomplete")
    assert.equal(converted[0].status.reason, "error")
    assert.equal(converted[0].status.error, "Output limit reached.")
    assert.equal(converted[2].status.type, "complete")
    assert.equal(snapshot.stateDelta[key].status, "succeeded")
  })

  test("an error stays failed through later state or tool-result events in that invocation", () => {
    const snapshot = snapshotOfAdkEvents([
      error,
      {
        id: "state",
        invocationId: "turn1",
        actions: { stateDelta: { keep: true } },
      },
      {
        id: "tool",
        invocationId: "turn1",
        author: "agent",
        content: {
          role: "user",
          parts: [
            {
              functionResponse: {
                id: "call",
                name: "lookup",
                response: { result: true },
              },
            },
          ],
        },
      },
    ])
    assert.equal(snapshot.stateDelta[key].status, "failed")
    assert.equal(snapshot.stateDelta.keep, true)
  })

  test("snapshots retain state, artifacts, interruption and tool requests", () => {
    const snapshot = snapshotOfAdkEvents([
      {
        id: "tool",
        author: "agent",
        invocationId: "turn1",
        longRunningToolIds: ["call"],
        actions: {
          stateDelta: { keep: 3, [key]: { status: "failed" } },
          artifactDelta: { "report.txt": 2 },
        },
        content: {
          parts: [{ functionCall: { id: "call", name: "ask", args: {} } }],
        },
      },
      { id: "stopped", invocationId: "turn1", interrupted: true },
    ])
    assert.equal(snapshot.stateDelta.keep, 3)
    assert.deepEqual(snapshot.artifactDelta, { "report.txt": 2 })
    assert.deepEqual(snapshot.longRunningToolIds, ["call"])
    assert.equal(snapshot.stateDelta[key].status, "cancelled")
  })

  test("persisted transport-only failures retain the answer and remain failed", () => {
    const snapshot = snapshotOfAdkEvents([
      reply,
      terminal("failed", {
        errorCode: "STREAM_INTERRUPTED",
        errorMessage: "Connection ended early.",
      }),
    ])
    assert.match(text(snapshot), /Retained answer/)
    assert.equal(text(snapshot).split("Connection ended early.").length - 1, 1)
    assert.equal(snapshot.stateDelta[key].status, "failed")
    const converted = convertExternalMessages(
      snapshot.messages,
      convertAdkMessage,
      false,
      {}
    )
    assert.equal(converted[0].status.reason, "error")
  })

  test("persisted terminal failures do not duplicate preceding native error text", () => {
    const snapshot = snapshotOfAdkEvents([
      error,
      terminal("failed", {
        errorCode: error.errorCode,
        errorMessage: error.errorMessage,
      }),
    ])
    assert.equal(text(snapshot).split(error.errorMessage).length - 1, 1)
    assert.equal(text(snapshot).split("Retained answer").length - 1, 1)
    assert.equal(snapshot.stateDelta[key].status, "failed")
  })

  test("persisted cancellation retains interrupted final text once and explains the stop once", () => {
    const snapshot = snapshotOfAdkEvents([
      { ...reply, id: "partial", partial: true },
      { ...reply, id: "interrupted", interrupted: true },
      terminal("cancelled"),
    ])
    assert.equal(text(snapshot).split("Retained answer").length - 1, 1)
    assert.equal(text(snapshot).split("The reply was stopped").length - 1, 1)
    assert.equal(snapshot.stateDelta[key].status, "cancelled")
    const converted = convertExternalMessages(
      snapshot.messages,
      convertAdkMessage,
      false,
      {}
    )
    assert.equal(converted[0].status.type, "incomplete")
    assert.equal(converted[0].status.reason, "cancelled")
  })

  test("older persisted terminal failures and cancellations remain marked without poisoning the next success", () => {
    for (const status of ["failed", "cancelled"]) {
      const snapshot = snapshotOfAdkEvents([
        { ...reply, partial: true },
        terminal(status),
        {
          id: "user2",
          author: "user",
          invocationId: "turn2",
          content: { parts: [{ text: "Try again" }] },
        },
        { ...reply, id: "success", invocationId: "turn2" },
      ])
      const converted = convertExternalMessages(
        snapshot.messages,
        convertAdkMessage,
        false,
        {}
      )
      assert.equal(converted[0].status.type, "incomplete")
      assert.equal(
        converted[0].status.reason,
        status === "failed" ? "error" : "cancelled"
      )
      assert.match(
        converted[0].content.at(-1).text,
        /couldn't finish|was stopped/
      )
      assert.equal(converted[2].status.type, "complete")
      assert.equal(snapshot.stateDelta[key].status, "succeeded")
    }
  })

  test("delayed old terminal outcomes attach to their invocation without cancelling a newer success", () => {
    for (const status of ["failed", "cancelled"]) {
      const snapshot = snapshotOfAdkEvents([
        { ...reply, partial: true },
        {
          id: "user2",
          author: "user",
          invocationId: "turn2",
          content: { parts: [{ text: "Try again" }] },
        },
        {
          ...reply,
          id: "success",
          invocationId: "turn2",
          content: { parts: [{ text: "New successful answer" }] },
        },
        terminal(status),
      ])
      const converted = convertExternalMessages(
        snapshot.messages,
        convertAdkMessage,
        false,
        {}
      )
      assert.equal(
        converted[0].status.reason,
        status === "failed" ? "error" : "cancelled"
      )
      assert.match(
        converted[0].content.at(-1).text,
        /couldn't finish|was stopped/
      )
      assert.equal(converted[2].status.type, "complete")
      assert.deepEqual(
        converted[2].content.map((part) => part.text),
        ["New successful answer"]
      )
      assert.equal(snapshot.stateDelta[key].status, "succeeded")
    }
  })

  test("delayed terminal after a native error does not duplicate that old error or poison the new turn", () => {
    const snapshot = snapshotOfAdkEvents([
      error,
      {
        id: "user2",
        author: "user",
        invocationId: "turn2",
        content: { parts: [{ text: "Try again" }] },
      },
      { ...reply, id: "success", invocationId: "turn2" },
      terminal("failed", {
        errorCode: error.errorCode,
        errorMessage: error.errorMessage,
      }),
    ])
    assert.equal(text(snapshot).split(error.errorMessage).length - 1, 1)
    assert.equal(snapshot.stateDelta[key].status, "succeeded")
  })

  test("terminal placement never moves native content or state events with turn metadata", () => {
    const snapshot = snapshotOfAdkEvents([
      reply,
      {
        id: "user2",
        author: "user",
        invocationId: "turn2",
        content: { parts: [{ text: "Try again" }] },
      },
      {
        ...reply,
        id: "success",
        invocationId: "turn2",
        content: { parts: [{ text: "New answer" }] },
        actions: { stateDelta: { version: 1 } },
      },
      {
        ...terminal("failed"),
        id: "native-content",
        content: { parts: [{ text: "Late native content" }] },
        actions: { stateDelta: { version: 2 } },
      },
    ])
    assert.ok(
      text(snapshot).indexOf("Late native content") >
        text(snapshot).indexOf("New answer")
    )
    assert.equal(snapshot.stateDelta.version, 2)
  })

  test("contentless cancellation produces a visible stopped reply", () => {
    const snapshot = snapshotOfAdkEvents([
      terminal("cancelled", { errorMessage: "This reply was cancelled." }),
    ])
    assert.equal(snapshot.stateDelta[key].status, "cancelled")
    assert.equal(text(snapshot), "This reply was cancelled.")
  })

  test("a native error carrying interruption cannot lose its content or failure status", () => {
    const snapshot = snapshotOfAdkEvents([
      { ...error, interrupted: true },
      terminal("cancelled"),
    ])
    assert.match(text(snapshot), /Retained answer/)
    assert.match(text(snapshot), /Output limit reached/)
    assert.equal(snapshot.stateDelta[key].status, "failed")
    assert.doesNotMatch(text(snapshot), /reply was stopped/)
  })

  test("malformed history rejects instead of presenting a plausible partial transcript", () => {
    for (const bad of [
      null,
      {},
      [],
      { content: 5 },
      { id: {} },
      { content: { parts: [null] } },
      { content: { parts: [{ text: 2 }] } },
      { actions: { stateDelta: [] } },
      { partial: "yes" },
    ])
      assert.throws(
        () => snapshotOfAdkEvents([reply, bad]),
        /Invalid ADK session event at index 1/
      )
    assert.throws(() => snapshotOfAdkEvents(null), /expected an array/)
    assert.deepEqual(snapshotOfAdkEvents([]).messages, [])
  })

  test("history loader fetches the requested session with fresh headers and abort signal", async (t) => {
    const calls = []
    t.mock.method(globalThis, "fetch", async (url, init) => {
      calls.push({ url, init })
      return Response.json({ id: "session /", events: [error] })
    })
    let token = "one"
    const load = createAdkHistoryLoader({
      url: "https://example.invalid///",
      appName: "agent/draft",
      userId: "person +",
      headers: () => ({ Authorization: token }),
    })
    const controller = new AbortController()
    assert.equal(
      (await load("session /", { signal: controller.signal })).stateDelta[key]
        .status,
      "failed"
    )
    token = "two"
    await load("session /")
    assert.equal(
      calls[0].url,
      "https://example.invalid/apps/agent%2Fdraft/users/person%20%2B/sessions/session%20%2F"
    )
    assert.equal(calls[0].init.signal, controller.signal)
    assert.equal(calls[0].init.headers.Authorization, "one")
    assert.equal(calls[1].init.headers.Authorization, "two")
  })

  test("history HTTP errors and malformed response envelopes reject for the shared error UI", async (t) => {
    let response
    t.mock.method(globalThis, "fetch", async () => response)
    const load = createAdkHistoryLoader({
      url: "https://example.invalid",
      appName: "agent",
      userId: "user",
    })
    response = new Response("unavailable", { status: 503 })
    await assert.rejects(load("session"), /503/)
    for (const body of [
      null,
      [],
      {},
      { id: "other" },
      { id: "session", events: {} },
      { id: "session", events: [null] },
    ]) {
      response = Response.json(body)
      await assert.rejects(load("session"), /Invalid ADK session/)
    }
  })

  test("history cancellation stops before fetch even while asynchronous headers are pending", async (t) => {
    let fetched = false
    t.mock.method(globalThis, "fetch", async () => {
      fetched = true
      return Response.json({ id: "session" })
    })
    const controller = new AbortController()
    const load = createAdkHistoryLoader({
      url: "https://example.invalid",
      appName: "agent",
      userId: "user",
      headers: () => new Promise(() => {}),
    })
    const loading = load("session", { signal: controller.signal })
    controller.abort()
    await assert.rejects(loading, { name: "AbortError" })
    assert.equal(fetched, false)
    await assert.rejects(load("session", { signal: controller.signal }), {
      name: "AbortError",
    })
  })
}
