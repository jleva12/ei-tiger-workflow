import assert from "node:assert/strict"
import { test } from "node:test"

/** Run the same approval contract against the app and distributable UI copies. */
export function testApprovals({
  approvalWords,
  shownArguments,
  pendingApprovals,
  confirmationReply,
  createApprovalBatch,
  CONFIRMATION_TOOL,
}) {
  const adkHint =
    "Please approve or reject the tool call test_agent() by responding with a " +
    "FunctionResponse with an expected ToolConfirmation payload."

  test("ADK's own hint is no hint: the card asks in general terms", () => {
    assert.deepEqual(approvalWords("test_agent", adkHint), {
      question: "Allow “Test agent”?",
      detail: undefined,
    })
    assert.deepEqual(approvalWords("deploy", undefined), {
      question: "Allow “Deploy”?",
      detail: undefined,
    })
  })

  test("a tool's question is the card's, and the rest says what it does", () => {
    assert.deepEqual(
      approvalWords(
        "test_agent",
        "Try “Support” with this message?\nIts draft answers as in a real chat."
      ),
      {
        question: "Try “Support” with this message?",
        detail: "Its draft answers as in a real chat.",
      }
    )
    // A hint that isn't a question explains the general one.
    assert.deepEqual(
      approvalWords("delete_file", "This removes it for good."),
      {
        question: "Allow “Delete file”?",
        detail: "This removes it for good.",
      }
    )
  })

  test("arguments show by name, without IDs, empty values or JSON", () => {
    const shown = shownArguments({
      organization_id: "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01",
      agent_id: "ca_k2x8d1q",
      agentId: "ca_k2x8d1q",
      messages: ["Where's my order 0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01?", ""],
      separately: true,
      scope: "org:0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01",
      workflow: "ag_test000001",
      input: { order_number: "A-1", customer_id: "c1", rush: false },
      wait_seconds: 90,
      state: null,
      note: "  ",
    })
    assert.deepEqual(shown, [
      {
        key: "messages",
        label: "Messages",
        // A message that mentions an ID is still the message.
        value: ["Where's my order 0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01?"],
      },
      { key: "separately", label: "Separately", value: "Yes" },
      {
        key: "input",
        label: "Input",
        value: ["Order number: A-1", "Rush: No"],
      },
      { key: "wait_seconds", label: "Wait seconds", value: "90" },
    ])
    assert.deepEqual(shownArguments(undefined), [])
  })

  test("a message's pending approvals are its unanswered confirmation calls", () => {
    const call = (toolCallId, extra = {}) => ({
      type: "tool-call",
      toolName: CONFIRMATION_TOOL,
      toolCallId,
      approval: { id: toolCallId },
      ...extra,
    })
    assert.deepEqual(
      pendingApprovals([
        { type: "text", text: "Two tests." },
        call("a"),
        { type: "tool-call", toolName: "test_agent", toolCallId: "g" },
        call("b", { approval: { id: "b", approved: true } }),
        call("c", { result: { confirmed: false } }),
        call("d"),
      ]),
      ["a", "d"]
    )
  })

  test("decisions wait until every approval of the turn is decided, then go once", () => {
    const batch = createApprovalBatch()
    let heard = 0
    const stop = batch.subscribe(() => {
      heard += 1
    })
    const turn = ["a", "b"]

    batch.decide("a", true)
    assert.equal(batch.decisionOf("a"), true)
    // The other is still to answer: nothing goes yet.
    assert.equal(batch.take(turn), undefined)
    assert.equal(batch.isSent("a"), false)

    // Taken back and made again.
    batch.undo("a")
    assert.equal(batch.decisionOf("a"), undefined)
    batch.decide("a", true)
    batch.decide("b", false)

    const replies = batch.take(turn)
    assert.deepEqual(
      replies.map(({ tool_call_id, name, content, type, status }) => ({
        tool_call_id,
        name,
        content: JSON.parse(content),
        type,
        status,
      })),
      [
        {
          tool_call_id: "a",
          name: CONFIRMATION_TOOL,
          content: { confirmed: true },
          type: "tool",
          status: "success",
        },
        {
          tool_call_id: "b",
          name: CONFIRMATION_TOOL,
          content: { confirmed: false },
          type: "tool",
          status: "success",
        },
      ]
    )
    assert.notEqual(replies[0].id, replies[1].id)
    // Only one card sends them, and a sent decision stays as it was.
    assert.equal(batch.take(turn), undefined)
    batch.undo("a")
    batch.decide("b", true)
    assert.equal(batch.decisionOf("a"), true)
    assert.equal(batch.decisionOf("b"), false)
    assert.ok(batch.isSent("a") && batch.isSent("b"))

    // Sending failed: they can be decided and sent again.
    batch.release(turn)
    assert.equal(batch.isSent("a"), false)
    assert.equal(batch.decisionOf("a"), undefined)

    stop()
    const before = heard
    batch.decide("a", true)
    assert.equal(heard, before)
    assert.ok(batch.version() > before)
  })

  test("a lone approval goes as soon as it's decided", () => {
    const batch = createApprovalBatch()
    assert.equal(batch.take([]), undefined)
    batch.decide("only", true)
    assert.equal(batch.take(["only"]).length, 1)
  })

  test("a reply is the shape ADK resumes a call on", () => {
    assert.deepEqual(confirmationReply("call-1", true, "reply-1"), {
      id: "reply-1",
      type: "tool",
      tool_call_id: "call-1",
      name: CONFIRMATION_TOOL,
      content: '{"confirmed":true}',
      status: "success",
    })
  })
}
