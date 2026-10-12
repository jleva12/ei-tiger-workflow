import type {
  AdkEvent,
  AdkMessage,
  AdkStreamCallback,
} from "@assistant-ui/react-google-adk"

import type { AdkArtifactsApi } from "@/components/forge/assistant"
import {
  FOLLOW_UPS_REQUEST_KEY,
  FOLLOW_UPS_STATE_KEY,
} from "@/components/forge/assistant/follow-ups"

/*
 * A stand-in for an ADK server, so the demo runs the real ADK runtime with
 * no backend. It streams events the way ADK's /run_sse does: partial text
 * chunks then the full final event, function calls and responses, and the
 * long-running `adk_request_*` calls for confirmations, sign-in and input.
 */

const COORDINATOR = "forge_assistant"
const REVIEWER = "review_agent"

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

type Run = {
  invocationId: string
  signal: AbortSignal
  /** The composer's model section, sent as ADK state (`stateDelta`). */
  model?: string
  thinkingLevel?: string
  /** What the conversation held when the run started, in tokens. */
  context: number
}

/**
 * The models' windows and reply budgets, as model_provider.yaml gives them:
 * what forge_common's `ContextWindowPlugin` measures against.
 */
const WINDOWS: Record<string, { window: number; reply: number }> = {
  "gemini-2.5-pro": { window: 1_048_576, reply: 65_536 },
  "gemini-2.5-flash": { window: 1_048_576, reply: 65_536 },
}

/**
 * The agent's context window after it answered `text`, as the plugin keeps
 * it in the session's state (`forge:context:<agent>`): the composer's
 * context meter reads it.
 */
function contextState(run: Run, author: string, text: string) {
  const model = run.model ?? "gemini-2.5-pro"
  const { window, reply } = WINDOWS[model] ?? WINDOWS["gemini-2.5-pro"]!
  const limit = window - reply
  return {
    [`forge:context:${author}`]: {
      tokens: run.context + Math.ceil(text.length / 3.5),
      limit,
      threshold: Math.floor(limit * 0.8),
      window,
      reply,
      share: 0.8,
      auto: true,
      counted: "provider",
      model,
      updated: Date.now() / 1000,
    },
  }
}

/** Streams `text` (and optional reasoning) as partial chunks, then the final event. */
async function* reply(
  run: Run,
  author: string,
  text: string,
  {
    thought: reasoning,
    actions,
  }: { thought?: string; actions?: AdkEvent["actions"] } = {}
): AsyncGenerator<AdkEvent> {
  const chunks = (source: string) => source.match(/\S+\s*/g) ?? [source]
  // Like a before_model_callback reading `thinking_level`: off skips reasoning.
  const thought = run.thinkingLevel === "off" ? undefined : reasoning
  if (thought) {
    for (const piece of chunks(thought)) {
      yield {
        id: nextId("evt"),
        invocationId: run.invocationId,
        author,
        partial: true,
        content: { role: "model", parts: [{ text: piece, thought: true }] },
      }
      await wait(18, run.signal)
    }
  }
  let buffered = ""
  for (const piece of chunks(text)) {
    buffered += piece
    if (buffered.length < 14) continue
    yield {
      id: nextId("evt"),
      invocationId: run.invocationId,
      author,
      partial: true,
      content: { role: "model", parts: [{ text: buffered }] },
    }
    buffered = ""
    await wait(28, run.signal)
  }
  if (buffered) {
    yield {
      id: nextId("evt"),
      invocationId: run.invocationId,
      author,
      partial: true,
      content: { role: "model", parts: [{ text: buffered }] },
    }
  }
  yield {
    id: nextId("evt"),
    invocationId: run.invocationId,
    author,
    turnComplete: true,
    content: {
      role: "model",
      parts: [...(thought ? [{ text: thought, thought: true }] : []), { text }],
    },
    actions: {
      ...actions,
      stateDelta: {
        ...actions?.stateDelta,
        ...contextState(run, author, text),
      },
    },
  }
}

const call = (
  run: Run,
  name: string,
  args: Record<string, unknown>,
  longRunning = false
): AdkEvent & { callId: string } => {
  const callId = nextId("call")
  return {
    callId,
    id: nextId("evt"),
    invocationId: run.invocationId,
    author: COORDINATOR,
    content: {
      role: "model",
      parts: [{ functionCall: { name, id: callId, args } }],
    },
    ...(longRunning && { longRunningToolIds: [callId] }),
  }
}

const respond = (
  run: Run,
  name: string,
  id: string,
  response: unknown
): AdkEvent => ({
  id: nextId("evt"),
  invocationId: run.invocationId,
  author: COORDINATOR,
  content: {
    role: "user",
    parts: [{ functionResponse: { name, id, response } }],
  },
})

const failingTasks = [
  {
    id: "FG-214",
    title: "Test worker reconnection",
    failure: "Timeout in reconnect loop",
  },
  {
    id: "FG-198",
    title: "Migrate billing webhooks",
    failure: "Contract test mismatch",
  },
  { id: "FG-176", title: "Paginate audit log", failure: "Flaky snapshot" },
]

export const artifactFiles: Record<string, string> = {
  "failing-tasks.md": [
    "# Failing tasks",
    "",
    ...failingTasks.map((t) => `- **${t.id}** ${t.title}: ${t.failure}`),
  ].join("\n"),
}

/**
 * Pending confirmation requests by call id. ADK clients send only the new
 * message, so like an ADK session the server remembers what it asked.
 */
const pendingConfirmations = new Map<string, { id: string; name: string }>()

async function* scenario(
  messages: AdkMessage[],
  run: Run
): AsyncGenerator<AdkEvent> {
  const last = messages.at(-1)

  // Replies to human-in-the-loop requests arrive as tool messages.
  if (last?.type === "tool") {
    const parsed = (() => {
      try {
        return JSON.parse(last.content) as Record<string, unknown>
      } catch {
        return {}
      }
    })()
    const original = pendingConfirmations.get(last.tool_call_id)
    pendingConfirmations.delete(last.tool_call_id)

    if (last.name === "adk_request_confirmation" && original) {
      if (parsed.confirmed === true) {
        yield respond(run, original.name, original.id, {
          status: "deleted",
          branch: "feat/legacy-sync",
        })
        yield* reply(
          run,
          COORDINATOR,
          "Done — `feat/legacy-sync` is deleted. It had no open pull requests, so nothing else changed."
        )
      } else {
        yield respond(run, original.name, original.id, {
          error: "The user declined the confirmation.",
        })
        yield* reply(
          run,
          COORDINATOR,
          "Okay, I left `feat/legacy-sync` in place."
        )
      }
      return
    }
    if (last.name === "adk_request_credential") {
      yield respond(run, "list_pull_requests", nextId("call"), { count: 4 })
      yield* reply(
        run,
        COORDINATOR,
        "Connected to GitHub. You have **4 open pull requests**; two are waiting on your review:\n\n1. `#482` Reconnect backoff for test workers\n2. `#479` Billing webhook contract update"
      )
      return
    }
    if (last.name === "adk_request_input") {
      // The form's answer, in the schema's types (or a plain answer).
      const answer =
        typeof parsed.result === "object" && parsed.result !== null
          ? (parsed.result as Record<string, unknown>)
          : { environment: parsed.result }
      const env = String(answer.environment ?? "staging")
      const replicas = Number(answer.replicas ?? 2)
      const regions = Array.isArray(answer.regions)
        ? ` in ${answer.regions.map((region) => String(region).toUpperCase()).join(" and ")}`
        : ""
      yield* reply(
        run,
        COORDINATOR,
        `Deploying \`main\` to **${env}** with ${replicas} ${replicas === 1 ? "replica" : "replicas"}${regions}.` +
          (answer.notify ? " I'll tell the team when it's out." : "") +
          " I'll post the rollout status here when it finishes.",
        { actions: { stateDelta: { "user:last_environment": env } } }
      )
      return
    }
  }

  const text = humanText(last)

  if (/fail|task/.test(text)) {
    const search = call(run, "search_tasks", { status: "failed", limit: 10 })
    yield search
    await wait(500, run.signal)
    yield respond(run, "search_tasks", search.callId, { tasks: failingTasks })
    yield* reply(
      run,
      COORDINATOR,
      [
        "Three tasks are failing:",
        "",
        "| Task | Title | Failure |",
        "| --- | --- | --- |",
        ...failingTasks.map((t) => `| ${t.id} | ${t.title} | ${t.failure} |`),
        "",
        "`FG-214` fails consistently, so I'd start there. I saved the list as `failing-tasks.md`.",
      ].join("\n"),
      {
        thought:
          "The user wants failing tasks. Search with status=failed, then summarise the causes.",
        actions: { artifactDelta: { "failing-tasks.md": 1 } },
      }
    )
    return
  }

  if (/delete|clean|branch/.test(text)) {
    const gated = call(run, "delete_branch", { branch: "feat/legacy-sync" })
    yield gated
    await wait(300, run.signal)
    const confirmation = call(
      run,
      "adk_request_confirmation",
      {
        originalFunctionCall: {
          id: gated.callId,
          name: "delete_branch",
          args: { branch: "feat/legacy-sync" },
        },
        toolConfirmation: {
          hint: "Deleting a branch can't be undone.",
          confirmed: false,
        },
      },
      true
    )
    pendingConfirmations.set(confirmation.callId, {
      id: gated.callId,
      name: "delete_branch",
    })
    yield confirmation
    return
  }

  if (/github|connect|pull/.test(text)) {
    yield* reply(run, COORDINATOR, "I need access to GitHub for that.")
    yield call(
      run,
      "adk_request_credential",
      {
        functionCallId: nextId("call"),
        authConfig: {
          authScheme: { type: "oauth2" },
          exchangedAuthCredential: {
            authType: "oauth2",
            oauth2: {
              authUri:
                "https://github.com/login/oauth/authorize?client_id=forge-demo",
            },
          },
        },
      },
      true
    )
    return
  }

  if (/deploy|release/.test(text)) {
    yield call(
      run,
      "adk_request_input",
      {
        message: "Before I deploy `main`, a few details.",
        response_schema: {
          type: "object",
          properties: {
            environment: {
              type: "string",
              title: "Which environment?",
              enum: ["staging", "production"],
            },
            replicas: {
              type: "integer",
              title: "How many replicas?",
              description: "Between 1 and 10.",
              minimum: 1,
              maximum: 10,
              default: 2,
            },
            regions: {
              type: "array",
              title: "Which regions?",
              items: { type: "string", enum: ["eu", "us", "apac"] },
            },
            notify: { type: "boolean", title: "Tell the team when it's out?" },
          },
          required: ["environment", "replicas"],
        },
      },
      true
    )
    return
  }

  if (/review|hand/.test(text)) {
    // ADK hands off through a transfer_to_agent call, then the sub-agent answers.
    const transfer = call(run, "transfer_to_agent", { agent_name: REVIEWER })
    yield transfer
    yield {
      ...respond(run, "transfer_to_agent", transfer.callId, null),
      actions: { transferToAgent: REVIEWER },
    }
    await wait(250, run.signal)
    yield* reply(
      run,
      REVIEWER,
      "I reviewed `#482`. The backoff logic is right, but the retry cap should come from config rather than a constant. I'd request that one change before merging.",
      {
        thought:
          "Read the diff for #482, check retry handling and configuration.",
      }
    )
    return
  }

  if (/summar/.test(text)) {
    yield* reply(
      run,
      COORDINATOR,
      "- Three tasks are failing; `FG-214` fails every run.\n- `feat/legacy-sync` is waiting to be cleaned up.\n- `#482` needs one change before it merges.",
      { thought: "Pull the key points from this conversation." }
    )
    return
  }

  if (/human|person|escalat/.test(text)) {
    yield* reply(
      run,
      COORDINATOR,
      "I've asked a teammate to take over. They'll see this conversation and reply here.",
      { actions: { escalate: true } }
    )
    return
  }

  const runningOn = run.model
    ? `\n\nThis turn ran on \`${run.model}\` with **${run.thinkingLevel}** thinking.`
    : ""
  yield* reply(
    run,
    COORDINATOR,
    `I can look into tasks and failures, clean up branches, check your GitHub pull requests, run a deployment, or hand a review to the review agent. What would you like to do?${runningOn}`,
    {
      thought: "No specific request yet; offer what this agent can do.",
    }
  )
}

/** What the person wrote, in lower case; nothing for a tool's answer. */
function humanText(message: AdkMessage | undefined) {
  if (message?.type !== "human") return ""
  return (
    typeof message.content === "string"
      ? message.content
      : message.content
          .map((part) => ("text" in part ? part.text : ""))
          .join(" ")
  ).toLowerCase()
}

/** What the demo's server suggests asking next, by what the turn was about. */
const FOLLOW_UPS: [RegExp, string[]][] = [
  [
    /deploy|replica/i,
    ["Show the deploy's logs", "Roll it back", "Deploy to staging too"],
  ],
  [/branch/i, ["Which other branches are stale?", "Protect main", "Undo that"]],
  [
    /review|hand/i,
    ["What did the reviewer flag?", "Merge it", "Ask for another review"],
  ],
  [
    /summar/i,
    ["Make it shorter", "What's still open?", "Share it with the team"],
  ],
  [
    /github|pull request/i,
    ["List my open pull requests", "Which need my review?"],
  ],
]
const SUGGESTED = [
  "What can you do?",
  "Deploy main",
  "Summarize this week's activity",
]

// Whether the session's state holds the last reply's follow-ups.
let suggested = false

/**
 * The run's events, then follow-up questions as forge_common's
 * `FollowUpsPlugin` adds them: a run that asks (`forge:suggest_follow_ups`)
 * and ends with a reply gets a few in the session's state
 * (`forge:follow_ups`); one that ends waiting on the person gets none; every
 * run clears the last reply's first.
 */
async function* withFollowUps(
  events: AsyncGenerator<AdkEvent>,
  run: Run,
  asked: unknown,
  said: string
): AsyncGenerator<AdkEvent> {
  if (suggested) {
    suggested = false
    yield {
      id: nextId("evt"),
      invocationId: run.invocationId,
      author: COORDINATOR,
      actions: { stateDelta: { [FOLLOW_UPS_STATE_KEY]: null } },
    }
  }
  let last: AdkEvent | undefined
  for await (const event of events) {
    if (!event.partial && event.content?.parts?.length) last = event
    yield event
  }
  const count =
    asked === true
      ? 3
      : typeof asked === "object" && asked !== null
        ? Number((asked as { count?: unknown }).count ?? 3)
        : 0
  const parts = last?.content?.parts ?? []
  const text = parts.map((part) => (part.thought ? "" : (part.text ?? "")))
  if (
    !last ||
    !count ||
    last.longRunningToolIds?.length ||
    parts.some((part) => part.functionCall) ||
    !text.join("").trim()
  ) {
    return
  }
  // The model writes them once the reply is done.
  await wait(500, run.signal)
  // What the person asked about; for an answer to a form, what the reply was.
  const about = said || text.join(" ")
  const topic = FOLLOW_UPS.find(([pattern]) => pattern.test(about))
  suggested = true
  yield {
    id: nextId("evt"),
    invocationId: run.invocationId,
    author: last.author ?? COORDINATOR,
    actions: {
      stateDelta: {
        [FOLLOW_UPS_STATE_KEY]: {
          suggestions: (topic?.[1] ?? SUGGESTED).slice(0, count),
        },
      },
    },
  }
}

/** An `AdkStreamCallback` backed by the scripted scenarios above. */
export const mockAdkStream: AdkStreamCallback = async (messages, config) => {
  await config.initialize()
  const state = config.stateDelta ?? {}
  // The instructions and tools, and the conversation so far.
  const said = messages.reduce(
    (chars, message) =>
      chars +
      (typeof message.content === "string"
        ? message.content.length
        : JSON.stringify(message.content ?? "").length),
    0
  )
  const run: Run = {
    invocationId: nextId("inv"),
    signal: config.abortSignal,
    context: 6_000 + Math.ceil(said / 3.5),
    model: typeof state.model === "string" ? state.model : undefined,
    thinkingLevel:
      typeof state.thinking_level === "string"
        ? state.thinking_level
        : undefined,
  }
  return withFollowUps(
    scenario(messages, run),
    run,
    state[FOLLOW_UPS_REQUEST_KEY],
    humanText(messages.at(-1))
  )
}

/** Artifact downloads for the demo, as the ADK session adapter would. */
export const mockArtifacts: AdkArtifactsApi = {
  list: async () => Object.keys(artifactFiles),
  load: async (_sessionId, name) => ({ text: artifactFiles[name] ?? "" }),
  listVersions: async () => [1],
  delete: async () => {},
}

/** Pretends to complete an OAuth round trip. */
export const mockSignIn = async () => {
  await new Promise((resolve) => setTimeout(resolve, 900))
  return {
    authType: "oauth2" as const,
    oauth2: {
      authResponseUri: "https://forge.example/oauth/callback?code=demo",
    },
  }
}
