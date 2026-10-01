/**
 * Map of topic names to their data payload types.
 * Define this once per app to get full type safety across all handlers.
 *
 * @example
 * ```ts
 * interface MyEvents {
 *   'orders.created': { order_id: string; total: number };
 *   'orders.updated': { order_id: string; status: string };
 *   'payments.completed': { payment_id: string; amount: number };
 * }
 * ```
 */
export type EventMap = Record<string, Record<string, unknown>>;

/**
 * Information about an SSE event as received from the event bus.
 * Only fields actually present in the SSE stream are included.
 */
export interface EventInfo {
  /** Unique event ID (from the SSE `id:` field) */
  eventId: string;
  /** Topic name (from the SSE `event:` field, e.g. "orders.created") */
  topic: string;
}

/**
 * Handler function for a specific event topic.
 * The data parameter is typed based on the EventMap.
 */
export type EventHandler<TData> = (data: TData, info: EventInfo) => void;

/**
 * Connection state of the SSE stream.
 *
 * - `connecting` — initial connection attempt in progress
 * - `connected` — stream is open and receiving events
 * - `reconnecting` — lost connection, attempting to reconnect
 * - `disconnected` — not connected (either not started, manually closed, or max retries exceeded)
 */
export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

/**
 * A parsed SSE frame from the raw text stream.
 * @internal
 */
export interface SSEFrame {
  id: string;
  event: string;
  data: string;
}
