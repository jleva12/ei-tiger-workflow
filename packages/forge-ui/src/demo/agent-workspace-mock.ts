import type {
  AdkEvent,
  AdkStreamCallback,
} from "@assistant-ui/react-google-adk"

import { knowledgeBaseSources } from "@/components/forge/assistant"
import type { Plan } from "@/components/forge/agent-workspace"

/*
 * A scripted agent for the agent workspace demo, speaking ADK's event
 * protocol like the chat API's agent: it keeps a plan and notes in session
 * state (`set_plan`, `update_plan_step`, `add_note`), uses the calculator,
 * and cites the passages a Forge knowledge base search returns. No backend,
 * no model.
 */

const AGENT = "assistant"

let counter = 0
const nextId = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer)
        reject(new DOMException("Aborted", "AbortError"))
      },
      { once: true }
    )
  })

type Run = { invocationId: string; signal: AbortSignal; thinking: boolean }

/** Streams reasoning, then the answer in chunks, then the final event. */
async function* reply(
  run: Run,
  text: string,
  thought?: string
): AsyncGenerator<AdkEvent> {
  const pieces = (source: string) => source.match(/\S+\s*/g) ?? [source]
  const reasoning = run.thinking ? thought : undefined
  if (reasoning) {
    for (const piece of pieces(reasoning)) {
      yield {
        id: nextId("evt"),
        invocationId: run.invocationId,
        author: AGENT,
        partial: true,
        content: { role: "model", parts: [{ text: piece, thought: true }] },
      }
      await wait(22, run.signal)
    }
  }
  let buffered = ""
  for (const piece of pieces(text)) {
    buffered += piece
    if (buffered.length < 18) continue
    yield {
      id: nextId("evt"),
      invocationId: run.invocationId,
      author: AGENT,
      partial: true,
      content: { role: "model", parts: [{ text: buffered }] },
    }
    buffered = ""
    await wait(24, run.signal)
  }
  yield {
    id: nextId("evt"),
    invocationId: run.invocationId,
    author: AGENT,
    turnComplete: true,
    content: {
      role: "model",
      parts: [
        ...(reasoning ? [{ text: reasoning, thought: true }] : []),
        { text },
      ],
    },
  }
}

/** A tool call, then its result (with any session state it changed). */
async function* tool(
  run: Run,
  name: string,
  args: Record<string, unknown>,
  response: Record<string, unknown>,
  stateDelta?: Record<string, unknown>,
  ms = 500
): AsyncGenerator<AdkEvent> {
  const id = nextId("call")
  yield {
    id: nextId("evt"),
    invocationId: run.invocationId,
    author: AGENT,
    content: { role: "model", parts: [{ functionCall: { name, id, args } }] },
  }
  await wait(ms, run.signal)
  yield {
    id: nextId("evt"),
    invocationId: run.invocationId,
    author: AGENT,
    content: {
      role: "user",
      parts: [{ functionResponse: { name, id, response } }],
    },
    ...(stateDelta && { actions: { stateDelta } }),
  }
}

/* -------------------------------------------------------------------------- */
/* Scenarios                                                                  */
/* -------------------------------------------------------------------------- */

const LAUNCH_STEPS = [
  "Confirm pricing and plan names with finance",
  "Size the migration batch for existing customers",
  "Draft the announcement and in-app notice",
  "Run the staged rollout",
  "Review support tickets after a week",
]

const now = () => Math.floor(Date.now() / 1000)

async function* planLaunch(run: Run): AsyncGenerator<AdkEvent> {
  const plan: Plan = {
    id: nextId("plan"),
    title: "Launch the billing redesign",
    steps: LAUNCH_STEPS.map((text) => ({ text, status: "pending" })),
  }
  const snapshot = () => structuredClone(plan)
  yield* tool(
    run,
    "set_plan",
    { title: plan.title, steps: LAUNCH_STEPS },
    { plan: snapshot() },
    { plan: snapshot() }
  )

  for (const index of [0, 1, 2]) {
    const step = plan.steps[index]!
    step.status = "in_progress"
    step.started_at = now()
    yield* tool(
      run,
      "update_plan_step",
      { step: index + 1, status: "in_progress" },
      {
        step: index + 1,
        text: step.text,
        status: "in_progress",
        done: index,
        total: plan.steps.length,
      },
      { plan: snapshot() },
      300
    )
    await wait(900, run.signal)
    if (index === 1) {
      yield* tool(
        run,
        "calculate",
        { expression: "1840 * 0.15" },
        { expression: "1840 * 0.15", result: 276 }
      )
    }
    step.status = "done"
    step.finished_at = now()
    yield* tool(
      run,
      "update_plan_step",
      { step: index + 1, status: "done" },
      {
        step: index + 1,
        text: step.text,
        status: "done",
        done: index + 1,
        total: plan.steps.length,
      },
      { plan: snapshot() },
      300
    )
  }

  const note = {
    id: nextId("note"),
    text: "Launch on Oct 14 — brief support first",
  }
  yield* tool(
    run,
    "add_note",
    { text: note.text },
    { added: note },
    { notes: [note] }
  )

  yield* reply(
    run,
    [
      "The first three steps are done; rollout and the support review are next.",
      "",
      "| Batch | Customers | Share |",
      "| --- | ---: | ---: |",
      "| Pilot | 276 | 15% |",
      "| Remainder | 1,564 | 85% |",
      "",
      "The pilot is $$0.15 \\times 1840 = 276$$ accounts, so a bad migration reaches at most",
      "",
      "$$\\frac{276}{1840} = 15\\%$$",
      "",
      "of customers. The rollout:",
      "",
      "```mermaid",
      "flowchart LR",
      "  A[Pilot 15%] --> B{Errors < 1%?}",
      "  B -- yes --> C[Remaining 85%]",
      "  B -- no --> D[Pause and fix]",
      "```",
      "",
      "And the flag the rollout flips:",
      "",
      "```ts",
      'export const billingRedesign = flag("billing-redesign", {',
      "  rollout: 0.15,",
      "})",
      "```",
    ].join("\n"),
    "They want a launch plan. I'll lay out the steps, work through the ones I can do now, size the pilot batch and note the launch date."
  )
}

/**
 * What a Forge knowledge base tool answers (forge_agent_runtime's
 * KnowledgeBaseTool): the knowledge bases it searched, and the passages it
 * found, which the answer cites by ref.
 */
const SEARCH = {
  searched: ["Support policies", "Finance FAQ"],
  passages: [
    {
      ref: "RFD0030",
      knowledge_base: "Support policies",
      knowledge_base_id: "kb-support",
      document: "Refund policy.pdf",
      document_id: "doc-refunds",
      section: "Refunds > Eligibility",
      location: "page 2",
      text: "Customers can request a full refund within 30 days of purchase.",
      score: 0.71,
    },
    {
      ref: "ANP0412",
      knowledge_base: "Support policies",
      knowledge_base_id: "kb-support",
      document: "Refund policy.pdf",
      document_id: "doc-refunds",
      section: "Refunds > Annual plans",
      location: "page 3",
      text: "Annual plans are refunded pro rata after the first 30 days.",
      score: 0.64,
    },
    {
      ref: "BFQ0510",
      knowledge_base: "Finance FAQ",
      knowledge_base_id: "kb-finance",
      document: "Billing FAQ.docx",
      document_id: "doc-billing",
      section: "Processing",
      location: "",
      text: "Refunds reach the original payment method in 5–10 business days.",
      score: 0.58,
    },
  ],
}

async function* refundPolicy(run: Run): AsyncGenerator<AdkEvent> {
  yield* tool(
    run,
    "search_policies",
    { query: "refund policy" },
    // In the envelope every Forge tool answers in.
    { status: "success", payload: SEARCH },
    undefined,
    700
  )
  yield* reply(
    run,
    "Any purchase can be refunded in full within 30 days [RFD0030]. After that, annual plans are refunded for the unused months [ANP0412]. Either way the money reaches the original payment method in 5–10 business days [BFQ0510].",
    "Search the policy documents, then answer only from what they say, citing each passage."
  )
}

async function* fallback(run: Run): AsyncGenerator<AdkEvent> {
  yield* reply(
    run,
    "I'm a scripted agent for this demo. Ask me to **plan the billing launch** to see a plan, progress and notes, or ask **what our refund policy is** to see cited sources.",
    "No scenario matches; say what this demo can show."
  )
}

/** An `AdkStreamCallback` running the scenarios above. */
export const workspaceMockStream: AdkStreamCallback = async (
  messages,
  config
) => {
  const last = messages.at(-1)
  const text =
    last?.type === "human"
      ? (typeof last.content === "string"
          ? last.content
          : last.content
              .map((part) => ("text" in part ? part.text : ""))
              .join(" ")
        ).toLowerCase()
      : ""
  const run: Run = {
    invocationId: nextId("inv"),
    signal: config.abortSignal,
    thinking: config.stateDelta?.thinking_level !== "off",
  }
  if (/launch|plan/.test(text)) return planLaunch(run)
  if (/refund|policy|cite|source/.test(text)) return refundPolicy(run)
  return fallback(run)
}

/** How answers cite the demo's knowledge base search, as Forge's do. */
export const workspaceSources = knowledgeBaseSources()
