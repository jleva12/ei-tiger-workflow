import { newAgentId, toDocument, type AgentDocument } from "./document"
import {
  newNode,
  subAgentDefaults,
  type AgentConfigs,
  type AgentKind,
  type AgentStep,
  type SubAgent,
  type SubAgentConfigs,
  type SubAgentKind,
} from "./model"

/*
 * The example an organization can start from: a support desk, mixing
 * Google ADK agents with Forge's workflow steps. It looks the customer up
 * over HTTP (an unknown customer ends it); two agents read their message
 * at once (what it's about, and how they feel) and a merge waits for
 * both; a switch sends bugs to a team that reproduces the problem and
 * drafts a reply, which a person checks before it's emailed to each of
 * the customer's contacts; billing to an agent whose refunds need an
 * approval within 24 hours, with a loop agent polishing the reply; and
 * anything else to two researchers working at once, whose findings a
 * transform sums up. It carries no layout: the builder tidies it the
 * first time it opens.
 */

function node<K extends AgentKind>(
  id: string,
  kind: K,
  name: string,
  config: Partial<AgentConfigs[K]> = {}
) {
  const base = newNode(kind, [], name)
  return {
    id,
    data: { ...base, config: { ...base.config, ...config } } as AgentStep,
    position: { x: 0, y: 0 },
  }
}

function sub<K extends SubAgentKind>(
  id: string,
  kind: K,
  name: string,
  config: Partial<SubAgentConfigs[K]> = {}
): SubAgent {
  return {
    id,
    kind,
    name,
    config: { ...subAgentDefaults(kind), ...config },
  } as SubAgent
}

export function exampleAgent(organizationId: string): AgentDocument {
  const now = new Date().toISOString()
  const doc = toDocument(
    {
      id: newAgentId(),
      name: "Support desk",
      description:
        "Example: looks the customer up, works out what their message is about and how they feel, and routes it: bugs to a team that drafts a fix a person checks before it's emailed to each contact, billing to an agent whose refunds need an approval within 24 hours, and anything else to two researchers.",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        node("start", "start", "Start", {
          input_schema: {
            type: "object",
            required: ["message", "customer_id"],
            properties: {
              message: {
                type: "string",
                description: "What the customer wrote.",
              },
              customer_id: { type: "string", description: "Who they are." },
            },
          },
        }),
        node("customer", "http", "Look up the customer", {
          method: "GET",
          url: "https://api.example.com/customers/{{ input.customer_id }}",
          headers: [
            { id: "h_accept", name: "Accept", value: "application/json" },
          ],
          output_schema: {
            type: "object",
            required: ["name", "plan", "contacts"],
            properties: {
              name: { type: "string" },
              plan: { type: "string", enum: ["free", "pro", "enterprise"] },
              contacts: {
                type: "array",
                description: "Where replies go.",
                items: { type: "string", format: "email" },
              },
            },
          },
        }),
        node("triage", "llm", "Triage", {
          description: "Works out what the customer's message is about.",
          instruction:
            "The customer wrote: {{ input.message }}\n\nSay whether it's about a BUG, BILLING or something ELSE, and sum it up in a sentence.",
          output_schema: {
            type: "object",
            required: ["category", "summary"],
            properties: {
              category: { type: "string", enum: ["BUG", "BILLING", "ELSE"] },
              summary: { type: "string" },
            },
          },
        }),
        node("sentiment", "llm", "Sentiment", {
          description: "Reads how the customer feels.",
          instruction:
            "The customer wrote: {{ input.message }}\n\nHow do they feel: calm, annoyed or angry?",
          output_schema: {
            type: "object",
            required: ["mood"],
            properties: {
              mood: { type: "string", enum: ["calm", "annoyed", "angry"] },
            },
          },
        }),
        node("read", "merge", "Read the message", { mode: "all" }),
        node("route", "switch", "Route by category", {
          value: "previous.triage.category",
          cases: [
            { id: "bug", value: "BUG" },
            { id: "billing", value: "BILLING" },
          ],
        }),
        node("bug_team", "sequential", "Bug team", {
          description: "Reproduces the bug, then drafts a fix and a reply.",
          sub_agents: [
            sub("reproduce", "llm", "Reproducer", {
              description: "Works out how to reproduce the bug.",
              instruction:
                "The bug: {{ steps.triage.output.summary }}\n\nList the steps that reproduce it.",
            }),
            sub("draft_fix", "llm", "Fix drafter", {
              description: "Drafts a fix and a reply to the customer.",
              instruction:
                "Using these steps to reproduce it: {{ state.reproduce }}\n\nDraft a fix, and a short reply to the customer.",
            }),
          ],
        }),
        node("check_fix", "human_input", "Check the reply", {
          message:
            "Send this reply to {{ steps.customer.output.body.name }}? {{ previous }}",
          response_schema: {
            type: "object",
            required: ["send"],
            properties: {
              send: { type: "boolean", description: "Whether to send it." },
              changes: { type: "string", description: "What to change first." },
            },
          },
        }),
        node("send_it", "if", "Send it?", { condition: "previous.send" }),
        node("notify", "loop", "Email each contact", {
          items: "steps.customer.output.body.contacts",
          item_name: "contact",
          max_iterations: 20,
          concurrency: 3,
        }),
        node("email", "http", "Email the reply", {
          method: "POST",
          url: "https://hooks.example.com/support/replies",
          headers: [
            { id: "h_ct", name: "Content-Type", value: "application/json" },
          ],
          body: '{\n  "to": contact,\n  "reply": steps.bug_team.output\n}',
        }),
        node("sent", "end", "Sent", {
          outcome: "succeeded",
          result: "steps.notify.output",
        }),
        node("held", "end", "Held back", {
          outcome: "succeeded",
          result: "steps.check_fix.output",
        }),
        node("billing", "llm", "Billing", {
          description: "Answers billing questions and works out any refund.",
          instruction:
            "Answer the customer's billing question: {{ input.message }}\n\nThey feel {{ steps.sentiment.output.mood }}. Say how much they're owed back, 0 if nothing.",
          output_schema: {
            type: "object",
            required: ["answer", "refund"],
            properties: {
              answer: { type: "string" },
              refund: {
                type: "number",
                minimum: 0,
                description: "Dollars owed back.",
              },
            },
          },
        }),
        node("owed", "if", "Refund owed?", {
          condition: "previous.refund > 0",
        }),
        node("approve_refund", "approval", "Approve the refund", {
          message:
            "Refund {{ steps.billing.output.refund }} dollars to {{ steps.customer.output.body.name }}? {{ steps.billing.output.answer }}",
          approvers: "org:admin",
          timeout_hours: 24,
        }),
        node("polish", "loop_agent", "Polish the reply", {
          description: "Drafts the reply and critiques it until it's right.",
          max_iterations: 3,
          sub_agents: [
            sub("writer", "llm", "Writer", {
              description: "Writes the reply.",
              instruction:
                "Write the reply to the customer from this answer: {{ steps.billing.output.answer }}\n\nTake in any critique: {{ state.critic }}",
            }),
            sub("critic", "llm", "Critic", {
              description: "Critiques the reply.",
              instruction:
                "Critique this reply: {{ state.writer }}\n\nIs it clear, kind and right?",
            }),
          ],
        }),
        node("replied", "end", "Replied", {
          outcome: "succeeded",
          result: "state.writer",
        }),
        node("declined", "end", "Refund declined", {
          outcome: "succeeded",
          result:
            '{\n  "refund": 0,\n  "reason": steps.approve_refund.output.comment\n}',
        }),
        node("research", "parallel", "Research", {
          description: "Looks in the docs and the forum at once.",
          sub_agents: [
            sub("docs", "llm", "Docs researcher", {
              description: "Looks in the product's docs.",
              instruction:
                "Find what the docs say about: {{ steps.triage.output.summary }}",
            }),
            sub("forum", "llm", "Forum researcher", {
              description: "Looks in the community forum.",
              instruction:
                "Find what the forum says about: {{ steps.triage.output.summary }}",
            }),
          ],
        }),
        node("sum_up", "transform", "Sum up", {
          expression: '{\n  "docs": state.docs,\n  "forum": state.forum\n}',
          output_schema: {
            type: "object",
            required: ["docs", "forum"],
            properties: { docs: { type: "string" }, forum: { type: "string" } },
          },
        }),
        node("answered", "end", "Answered", { outcome: "succeeded" }),
        node("unknown_customer", "end", "Unknown customer", {
          outcome: "failed",
          result: '"No customer on file: " & input.customer_id',
        }),
      ],
      connections: [
        { source: "start", output: "next", target: "customer" },
        { source: "customer", output: "success", target: "triage" },
        { source: "customer", output: "success", target: "sentiment" },
        { source: "customer", output: "error", target: "unknown_customer" },
        { source: "triage", output: "next", target: "read" },
        { source: "sentiment", output: "next", target: "read" },
        { source: "read", output: "next", target: "route" },
        { source: "route", output: "bug", target: "bug_team" },
        { source: "route", output: "billing", target: "billing" },
        { source: "route", output: "default", target: "research" },
        { source: "bug_team", output: "next", target: "check_fix" },
        { source: "check_fix", output: "next", target: "send_it" },
        { source: "send_it", output: "true", target: "notify" },
        { source: "send_it", output: "false", target: "held" },
        { source: "notify", output: "each", target: "email" },
        { source: "email", output: "success", target: "notify" },
        { source: "notify", output: "done", target: "sent" },
        { source: "billing", output: "next", target: "owed" },
        { source: "owed", output: "true", target: "approve_refund" },
        { source: "owed", output: "false", target: "polish" },
        { source: "approve_refund", output: "approved", target: "polish" },
        { source: "approve_refund", output: "rejected", target: "declined" },
        { source: "polish", output: "next", target: "replied" },
        { source: "research", output: "next", target: "sum_up" },
        { source: "sum_up", output: "next", target: "answered" },
      ],
    }
  )
  return { ...doc, layout: {} }
}
