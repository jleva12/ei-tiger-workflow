import assert from "node:assert/strict"
import { test } from "node:test"

/** Run the same transport contract against the app and distributable UI copies. */
export function testTurnLifecycle({
  withTurnLifecycle,
  CLIENT_TURN_STATE_KEY,
  TURN_METADATA_KEY,
  AdkEventAccumulator,
  convertAdkMessage,
  convertExternalMessages,
  createAdkStream,
}) {
  const runId = "a".repeat(32)
  const envelope = (status, extra = {}) => ({
    id: `turn-${status}`,
    customMetadata: {
      [TURN_METADATA_KEY]: { version: 1, runId, status, ...extra },
    },
  })
  const partial = {
    id: "part",
    author: "agent",
    partial: true,
    content: { role: "model", parts: [{ text: "A partial reply" }] },
  }
  const final = { ...partial, id: "final", partial: false }
  const config = (extra = {}) => ({
    abortSignal: new AbortController().signal,
    initialize: async () => ({ externalId: "session" }),
    ...extra,
  })
  const source = (events) =>
    async function* () {
      yield* events
    }
  async function collect(
    stream,
    options = { requireTurnOutcome: true },
    cfg = config()
  ) {
    const events = []
    for await (const event of withTurnLifecycle(stream, options)([], cfg))
      events.push(event)
    const accumulator = new AdkEventAccumulator([
      { id: "user", type: "human", content: "Hi" },
    ])
    for (const event of events) accumulator.processEvent(event)
    return {
      events,
      state: accumulator.getStateDelta()[CLIENT_TURN_STATE_KEY],
      messages: convertExternalMessages(
        accumulator.getMessages(),
        convertAdkMessage,
        false,
        {}
      ),
    }
  }
  const textOf = (result) =>
    result.messages
      .flatMap((m) =>
        m.role === "assistant"
          ? m.content.filter((p) => p.type === "text").map((p) => p.text)
          : []
      )
      .join("\n")

  test("a Forge turn succeeds only after its terminal envelope", async () => {
    const iterator = withTurnLifecycle(
      source([envelope("running"), partial, final, envelope("succeeded")]),
      { requireTurnOutcome: true }
    )([], config())
    assert.equal(
      (await iterator.next()).value.actions.stateDelta[CLIENT_TURN_STATE_KEY]
        .status,
      "running"
    )
    assert.deepEqual((await iterator.next()).value, partial)
    assert.deepEqual((await iterator.next()).value, final)
    assert.equal(
      (await iterator.next()).value.actions.stateDelta[CLIENT_TURN_STATE_KEY]
        .status,
      "succeeded"
    )
    assert.equal((await iterator.next()).done, true)
  })

  test("graceful EOF cannot silently drop an empty or partially delivered Forge turn", async () => {
    for (const events of [
      [],
      [envelope("running")],
      [envelope("running"), partial],
    ]) {
      const result = await collect(source(events))
      assert.equal(result.state.status, "failed")
      assert.equal(result.state.errorCode, "STREAM_INTERRUPTED")
      assert.match(textOf(result), /connection ended before/i)
      if (events.includes(partial))
        assert.match(textOf(result), /A partial reply/)
    }
  })

  test("plain ADK/custom transports remain compatible without the Forge protocol", async () => {
    assert.equal((await collect(source([final]), {})).state.status, "succeeded")
    // Once a server advertises the protocol, an absent terminal is still a failure.
    assert.equal(
      (await collect(source([envelope("running"), partial]), {})).state.status,
      "failed"
    )
  })

  test("a failure survives the installed SDK merging it with partial text", async () => {
    const result = await collect(
      source([
        envelope("running"),
        partial,
        {
          id: "error",
          errorCode: "MODEL_UNAVAILABLE",
          errorMessage: "The model is unavailable.",
        },
        envelope("failed", {
          errorCode: "MODEL_UNAVAILABLE",
          errorMessage: "The model is unavailable.",
        }),
      ])
    )
    assert.match(textOf(result), /A partial reply/)
    assert.equal(result.state.status, "failed")
    assert.equal(result.state.errorMessage, "The model is unavailable.")
    // This SDK currently loses the merged error status. The separate outcome
    // drives the visible failure UI instead of relying on that status.
    assert.ok(result.messages.some((m) => m.role === "assistant"))
  })

  test("content plus error retains final full text without duplicating streamed text", async () => {
    const result = await collect(
      source([
        envelope("running"),
        partial,
        {
          ...final,
          finishReason: "MAX_TOKENS",
          errorCode: "MAX_TOKENS",
          errorMessage: "Output limit reached.",
        },
        envelope("failed", {
          errorCode: "MAX_TOKENS",
          errorMessage: "Output limit reached.",
        }),
      ])
    )
    assert.equal(textOf(result).split("A partial reply").length - 1, 1)
    assert.equal(result.state.status, "failed")
    // Also preserve a non-streaming provider's first and only text response.
    const once = await collect(
      source([
        envelope("running"),
        {
          ...final,
          error_code: "MAX_TOKENS",
          error_message: "Output limit reached.",
        },
        envelope("failed"),
      ])
    )
    assert.match(textOf(once), /A partial reply/)
  })

  test("length and filtering failures are explained even without content", async () => {
    for (const finishReason of [
      "MAX_TOKENS",
      "SAFETY",
      "RECITATION",
      "MALFORMED_FUNCTION_CALL",
    ])
      for (const content of [undefined, final.content]) {
        const result = await collect(
          source([{ id: "end", finishReason, content }]),
          {}
        )
        assert.equal(result.state.status, "failed")
        assert.equal(result.state.errorCode, finishReason)
        assert.ok(result.state.errorMessage)
        assert.match(textOf(result), /limit|filters|tool call/)
      }
  })

  test("a failed or cancelled terminal works without an ordinary ADK error", async () => {
    const failed = await collect(
      source([
        envelope("running"),
        partial,
        envelope("failed", { errorMessage: "Run timed out." }),
      ])
    )
    assert.equal(failed.state.status, "failed")
    assert.match(textOf(failed), /Run timed out/)
    const cancelled = await collect(
      source([envelope("running"), partial, envelope("cancelled")])
    )
    assert.equal(cancelled.state.status, "cancelled")
    assert.match(cancelled.state.errorMessage, /stopped/)
    assert.equal(textOf(cancelled), "A partial reply")
  })

  test("interrupted final content is retained before a server cancellation", async () => {
    const result = await collect(
      source([
        envelope("running"),
        partial,
        { ...final, interrupted: true },
        envelope("cancelled"),
      ])
    )
    assert.equal(textOf(result).split("A partial reply").length - 1, 1)
    assert.equal(result.state.status, "cancelled")
  })

  test("invalid, mismatched and missing-start envelopes fail visibly", async () => {
    for (const events of [
      [envelope("succeeded")],
      [envelope("running"), envelope("running")],
      [envelope("running"), envelope("succeeded", { runId: "b".repeat(32) })],
      [envelope("running"), envelope("succeeded", { version: 2 })],
    ]) {
      const result = await collect(source(events))
      assert.equal(result.state.status, "failed")
      assert.equal(result.state.errorCode, "INVALID_TURN_OUTCOME")
    }
  })

  test("success cannot erase an observed model failure", async () => {
    const result = await collect(
      source([
        envelope("running"),
        { id: "error", errorCode: "ERROR", errorMessage: "Failed." },
        envelope("succeeded"),
      ])
    )
    assert.equal(result.state.status, "failed")
  })

  test("local response state is reset, cannot be spoofed, and never sent to agents", async () => {
    let sent
    const result = await collect(
      async function* (_, cfg) {
        sent = cfg.stateDelta
        yield envelope("running")
        yield {
          id: "state",
          actions: {
            state_delta: {
              [CLIENT_TURN_STATE_KEY]: { status: "succeeded" },
              business: 42,
            },
          },
        }
        yield envelope("failed")
      },
      undefined,
      config({
        stateDelta: {
          [CLIENT_TURN_STATE_KEY]: { status: "failed" },
          business: 1,
        },
      })
    )
    assert.deepEqual(sent, { business: 1 })
    assert.equal(
      result.events[0].actions.stateDelta[CLIENT_TURN_STATE_KEY].status,
      "running"
    )
    assert.equal(result.state.status, "failed")
    assert.equal(result.events[1].actions.stateDelta.business, 42)
    assert.equal(
      Object.hasOwn(result.events[1].actions.stateDelta, CLIENT_TURN_STATE_KEY),
      false
    )
  })

  test("explicit Stop does not manufacture a failed reply", async () => {
    const controller = new AbortController()
    const result = await collect(
      async function* () {
        yield envelope("running")
        yield partial
        controller.abort()
        throw new DOMException("Stopped", "AbortError")
      },
      undefined,
      config({ abortSignal: controller.signal })
    )
    assert.equal(
      result.events.some((event) => event.errorCode),
      false
    )
    assert.equal(textOf(result), "A partial reply")
  })

  test("transport and session-creation rejections produce a visible failed outcome", async () => {
    const rejected = await collect(async function* () {
      throw new Error("Network lost")
    })
    assert.equal(rejected.state.status, "failed")
    assert.match(textOf(rejected), /Network lost/)
    const initialized = await collect(
      createAdkStream({
        api: "https://example.invalid",
        appName: "app",
        userId: "user",
      }),
      undefined,
      config({
        initialize: async () => {
          throw new Error("Session unavailable")
        },
      })
    )
    assert.equal(initialized.state.status, "failed")
    assert.match(textOf(initialized), /Session unavailable/)
  })

  test("the actual SSE parser's normal partial EOF is caught by the lifecycle", async (t) => {
    t.mock.method(
      globalThis,
      "fetch",
      async () =>
        new Response(
          [envelope("running"), partial]
            .map((event) => `data: ${JSON.stringify(event)}\n\n`)
            .join(""),
          { headers: { "Content-Type": "text/event-stream" } }
        )
    )
    const result = await collect(
      createAdkStream({ api: "https://example.invalid" })
    )
    assert.equal(result.state.errorCode, "STREAM_INTERRUPTED")
    assert.match(textOf(result), /A partial reply/)
  })

  test("a received terminal remains authoritative if the connection fails during close", async () => {
    const result = await collect(async function* () {
      yield envelope("running")
      yield final
      yield envelope("succeeded")
      throw new TypeError("connection reset")
    })
    assert.equal(result.state.status, "succeeded")
  })

  test(
    "terminal outcome completes without reading a hanging socket and releases only the child request",
    { timeout: 1000 },
    async () => {
      const caller = new AbortController()
      let cleaned = false
      let requestSignal
      let reads = 0
      const events = [envelope("running"), partial, envelope("succeeded")]
      const result = await collect(
        (_, cfg) => {
          requestSignal = cfg.abortSignal
          return {
            [Symbol.asyncIterator]() {
              return this
            },
            next() {
              const value = events[reads++]
              return value
                ? Promise.resolve({ value, done: false })
                : new Promise(() => {})
            },
            return() {
              cleaned = true
              return Promise.resolve({ value: undefined, done: true })
            },
          }
        },
        undefined,
        config({ abortSignal: caller.signal })
      )
      assert.equal(result.state.status, "succeeded")
      assert.equal(reads, 3)
      assert.equal(cleaned, true)
      assert.equal(requestSignal.aborted, true)
      assert.equal(caller.signal.aborted, false)
    }
  )

  test("reader rejection preserves partial text and reports failure", async (t) => {
    let read = false
    t.mock.method(
      globalThis,
      "fetch",
      async () =>
        new Response(
          new ReadableStream({
            pull(controller) {
              if (read) controller.error(new TypeError("Connection reset"))
              else {
                read = true
                controller.enqueue(
                  new TextEncoder().encode(
                    [envelope("running"), partial]
                      .map((event) => `data: ${JSON.stringify(event)}\n\n`)
                      .join("")
                  )
                )
              }
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } }
        )
    )
    const result = await collect(
      createAdkStream({ api: "https://example.invalid" })
    )
    assert.equal(result.state.status, "failed")
    assert.match(textOf(result), /A partial reply/)
    assert.match(result.state.errorMessage, /Connection reset/)
  })
}
