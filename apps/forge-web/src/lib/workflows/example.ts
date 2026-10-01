import { newWorkflowId, toDocument, type WorkflowDocument } from "./document"
import { newStep, type StepConfigs, type StepData, type StepKind } from "./model"

/*
 * The example an organization can start from: an agent assesses a purchase
 * request against the vendor's record; large or risky purchases go to an
 * organization admin to approve, small ones wait for the next ordering
 * window, and approved ones are ordered. It uses most kinds of step. It
 * carries no layout: the builder tidies it the first time it opens.
 */

function step<K extends StepKind>(
  id: string,
  kind: K,
  name: string,
  config: Partial<StepConfigs[K]> = {}
) {
  const base = newStep(kind, name)
  return {
    id,
    data: { ...base, config: { ...base.config, ...config } } as StepData,
    position: { x: 0, y: 0 },
  }
}

export function exampleWorkflow(organizationId: string): WorkflowDocument {
  const now = new Date().toISOString()
  const doc = toDocument(
    {
      id: newWorkflowId(),
      name: "Review purchase requests",
      description:
        "Example: an agent assesses a purchase request against the vendor's record; large or risky purchases go to an organization admin to approve, small ones wait for the next ordering window, and approved ones are ordered. Start it with a request, or link it to an event type on Events.",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        // Started by hand with the request, so its input is declared and every
        // step reading it is checked; link it to an event type to run it on events.
        step("start", "entry", "Start", {
          trigger: "manual",
          input_schema: {
            type: "object",
            required: ["request"],
            properties: {
              request: {
                type: "object",
                required: ["id", "item", "amount", "vendor"],
                properties: {
                  id: { type: "string", description: "The request's reference, e.g. PR-1042." },
                  requester: { type: "string", description: "Who asks for it." },
                  item: { type: "string", description: "What they want to buy." },
                  amount: { type: "number", description: "What it costs, in dollars." },
                  vendor: { type: "string", description: "The vendor's ID." },
                  justification: { type: "string", description: "Why it's needed." },
                },
              },
            },
          },
        }),
        step("vendor", "http", "Look up the vendor", {
          method: "GET",
          url: "https://api.example.com/vendors/{{ input.request.vendor }}",
        }),
        step("assess", "agent", "Assess the request", {
          instructions:
            "Read the purchase request and the vendor's record. Say what the spend is for, whether it fits the justification, and how risky it is.",
          output: "json",
          output_schema: JSON.stringify(
            {
              type: "object",
              required: ["category", "risk", "summary"],
              properties: {
                category: { type: "string" },
                risk: { enum: ["low", "high"] },
                summary: { type: "string" },
              },
            },
            null,
            2
          ),
        }),
        step("route", "match", "Route by size and risk", {
          arms: [
            {
              id: "large",
              label: "Large purchase",
              condition: "input.request.amount > 10000",
            },
            {
              id: "risky",
              label: "Risky",
              condition: 'steps.assess.output.risk = "high"',
            },
          ],
        }),
        step("window", "delay", "Wait for the ordering window", {
          amount: 2,
          unit: "hours",
        }),
        step("review", "approval", "An admin approves the purchase", {
          message:
            "{{ input.request.requester }} asks to buy {{ input.request.item }} for {{ input.request.amount }} dollars from {{ input.request.vendor }}. {{ steps.assess.output.summary }} Approve the purchase?",
          approvers: "org:admin",
          timeout_hours: 48,
        }),
        step("place_order", "http", "Place the order", {
          method: "POST",
          url: "https://api.example.com/purchase-orders",
          headers: [{ id: "h_ct", name: "Content-Type", value: "application/json" }],
          body: '{\n  "request": input.request.id,\n  "vendor": input.request.vendor,\n  "item": input.request.item,\n  "amount": input.request.amount\n}',
        }),
        step("confirm", "transform", "Sum up the order", {
          expression:
            '{\n  "order": steps.place_order.output.body.id,\n  "category": steps.assess.output.category\n}',
        }),
        step("ordered", "end", "Ordered", {
          outcome: "succeeded",
          result: "steps.confirm.output",
        }),
        step("tell_requester", "http", "Tell the requester", {
          method: "POST",
          url: "https://hooks.example.com/procurement/declined",
          headers: [{ id: "h_ct", name: "Content-Type", value: "application/json" }],
          body: '{\n  "request": input.request.id,\n  "comment": steps.review.output.comment\n}',
        }),
        step("declined", "end", "Declined", {
          outcome: "failed",
          result: "steps.review.output",
        }),
        step("unknown_vendor", "end", "Unknown vendor", {
          outcome: "failed",
          result: '"The vendor isn\'t on file: " & input.request.vendor',
        }),
      ],
      connections: [
        { source: "start", output: "next", target: "vendor" },
        { source: "vendor", output: "success", target: "assess" },
        { source: "vendor", output: "error", target: "unknown_vendor" },
        { source: "assess", output: "next", target: "route" },
        { source: "route", output: "large", target: "review" },
        { source: "route", output: "risky", target: "review" },
        { source: "route", output: "otherwise", target: "window" },
        { source: "window", output: "next", target: "place_order" },
        { source: "review", output: "approved", target: "place_order" },
        { source: "review", output: "rejected", target: "tell_requester" },
        { source: "tell_requester", output: "success", target: "declined" },
        { source: "place_order", output: "success", target: "confirm" },
        { source: "confirm", output: "next", target: "ordered" },
      ],
    }
  )
  return { ...doc, layout: {} }
}
