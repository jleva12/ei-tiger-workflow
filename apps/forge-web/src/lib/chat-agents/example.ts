import { newChatAgentId, toDocument, type ChatAgentDocument } from "./document"
import {
  HANDS_OFF,
  newNode,
  TOOLS,
  type ChatConfigs,
  type ChatKind,
  type ChatStep,
} from "./model"

/*
 * The example an organization can start from: a support assistant. It
 * remembers the person's earlier conversations, looks orders up over HTTP
 * and reads the help center's MCP server; billing
 * questions go to a billing specialist, which checks refunds against the
 * billing API's OpenAPI spec. It carries no layout: the builder tidies it
 * the first time it opens.
 */

function node<K extends ChatKind>(
  id: string,
  kind: K,
  name: string,
  config: Partial<ChatConfigs[K]> = {}
) {
  const base = newNode(kind, [], name)
  return {
    id,
    data: { ...base, config: { ...base.config, ...config } } as ChatStep,
    position: { x: 0, y: 0 },
  }
}

export function exampleChatAgent(organizationId: string): ChatAgentDocument {
  const now = new Date().toISOString()
  const doc = toDocument(
    {
      id: newChatAgentId(),
      name: "Support assistant",
      description:
        "Example: answers customers' questions with their past conversations, their orders and the help center, and hands billing to a specialist.",
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    {
      steps: [
        node("agent", "agent", "Support assistant", {
          description:
            "Answers customers' questions about their orders and the product.",
          instruction:
            "You're the support assistant. Be brief and kind. Look orders up before answering questions about them, check the help center before saying something can't be done, and hand billing questions (invoices, charges, refunds) to the billing specialist.",
        }),
        node("memory", "memory", "Memory", { mode: "every_turn" }),
        node("orders", "http_tool", "Look up order", {
          description:
            "Finds an order by its number: its items, status and delivery date.",
          url: "https://api.example.com/orders/{order_id}",
          headers: [
            { id: "h_accept", name: "Accept", value: "application/json" },
          ],
          parameters: {
            type: "object",
            required: ["order_id"],
            properties: {
              order_id: {
                type: "string",
                description: "The order's number, e.g. A-10442.",
              },
            },
          },
        }),
        node("help", "mcp", "Help center", {
          url: "https://mcp.example.com/help-center",
        }),
        node("billing", "sub_agent", "Billing specialist", {
          description: "Handles billing: invoices, charges and refunds.",
          instruction:
            "You handle billing questions. Check what a refund would be before promising one, and say plainly when something can't be refunded.",
          mode: "chat",
        }),
        node("billing_api", "openapi", "Billing API", {
          url: "https://api.example.com/billing/openapi.json",
          operations: "getInvoice, listCharges, previewRefund",
          confirm: true,
        }),
      ],
      connections: [
        { source: "agent", output: TOOLS, target: "memory" },
        { source: "agent", output: TOOLS, target: "orders" },
        { source: "agent", output: TOOLS, target: "help" },
        { source: "agent", output: HANDS_OFF, target: "billing" },
        { source: "billing", output: TOOLS, target: "billing_api" },
      ],
    }
  )
  return { ...doc, layout: {} }
}
