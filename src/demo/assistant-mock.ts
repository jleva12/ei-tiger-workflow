import type {
  AdkEvent,
  AdkMessage,
  AdkStreamCallback,
} from "@assistant-ui/react-google-adk"

import type { AdkArtifactsApi } from "@/components/forge/assistant"

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
    actions,
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
      const env = String(parsed.result ?? "staging")
      yield* reply(
        run,
        COORDINATOR,
        `Deploying \`main\` to **${env}**. I'll post the rollout status here when it finishes.`,
        { actions: { stateDelta: { "user:last_environment": env } } }
      )
      return
    }
  }

  const text =
    last?.type === "human"
      ? (typeof last.content === "string"
          ? last.content
          : last.content
              .map((part) => ("text" in part ? part.text : ""))
              .join(" ")
        ).toLowerCase()
      : ""

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
        message: "Which environment should I deploy `main` to?",
        response_schema: { type: "string", enum: ["staging", "production"] },
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

/** An `AdkStreamCallback` backed by the scripted scenarios above. */
export const mockAdkStream: AdkStreamCallback = async (messages, config) => {
  await config.initialize()
  const state = config.stateDelta ?? {}
  const run: Run = {
    invocationId: nextId("inv"),
    signal: config.abortSignal,
    model: typeof state.model === "string" ? state.model : undefined,
    thinkingLevel:
      typeof state.thinking_level === "string"
        ? state.thinking_level
        : undefined,
  }
  return scenario(messages, run)
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
    oauth2: { authResponseUri: "https://forge.example/oauth/callback?code=demo" },
  }
}
