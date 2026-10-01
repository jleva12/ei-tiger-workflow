import { useCallback, useEffect, useRef, useState } from "react";

import { connectSSE } from "./sse-parser";
import type {
  ConnectionStatus,
  EventHandler,
  EventInfo,
  EventMap,
} from "./types";

// ---------------------------------------------------------------------------
// Options & Return types
// ---------------------------------------------------------------------------

export interface UseEventBusOptions<TMap extends EventMap> {
  /**
   * SSE endpoint URL.
   * Topics are appended as `?topics=orders.*,payments.**`.
   *
   * @example "/events/sse"
   * @example "https://api.example.com/events/sse"
   */
  url: string;

  /**
   * Topic patterns to subscribe to.
   * Supports `*` (single segment) and `**` (multi-segment) wildcards.
   *
   * @example ["orders.*", "payments.**"]
   * @example ["order.user_abc123"]
   */
  topics: string[];

  /**
   * Typed per-topic handlers.  Keys must be topics from your EventMap.
   * Each handler receives the payload typed to that topic's data shape.
   *
   * @example
   * ```ts
   * handlers: {
   *   'orders.created': (data) => console.log(data.order_id),
   *   'orders.updated': (data) => console.log(data.status),
   * }
   * ```
   */
  handlers?: { [K in keyof TMap]?: EventHandler<TMap[K]> };

  /**
   * Catch-all handler invoked for every event regardless of topic.
   * Runs after any matching typed handler.  Data is untyped (`unknown`).
   */
  onEvent?: (topic: string, data: unknown, info: EventInfo) => void;

  /** Called when the SSE connection opens. */
  onConnect?: () => void;

  /** Called when the SSE connection is lost (before reconnect starts). */
  onDisconnect?: () => void;

  /** Called on connection or parse errors. */
  onError?: (error: Error) => void;

  /**
   * Enable/disable the connection.  Set to `false` to disconnect
   * without unmounting.  Defaults to `true`.
   */
  enabled?: boolean;

  /** Send cookies with the SSE request (`credentials: 'include'`). */
  withCredentials?: boolean;

  /**
   * Custom HTTP headers sent with the SSE request.
   * Useful for Authorization tokens.
   *
   * **Tip:** Memoize this object with `useMemo` so the connection
   * isn't re-established on every render.
   *
   * @example { Authorization: "Bearer <token>" }
   */
  headers?: Record<string, string>;

  /** Maximum reconnect attempts before giving up.  Default: `Infinity`. */
  maxReconnectAttempts?: number;

  /** Initial reconnect delay in ms.  Default: `1000`. */
  reconnectDelay?: number;

  /** Maximum reconnect delay in ms (caps exponential backoff).  Default: `30000`. */
  maxReconnectDelay?: number;
}

export interface UseEventBusReturn {
  /** Current connection state. */
  status: ConnectionStatus;

  /** The most recently received event (any topic). `null` until the first event arrives. */
  lastEvent: { topic: string; data: unknown; info: EventInfo } | null;

  /** Manually close the connection.  Will not auto-reconnect. */
  disconnect: () => void;

  /** Manually reconnect (resets the attempt counter). */
  reconnect: () => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildURL(base: string, topics: string[]): string {
  // Support both absolute URLs and path-only URLs
  const url = new URL(base, globalThis.window?.location?.origin ?? "http://localhost");
  url.searchParams.set("topics", topics.join(","));
  return url.toString();
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * React hook to subscribe to the event bus via SSE.
 *
 * @typeParam TMap — an EventMap interface mapping topic names to payload types.
 *
 * @example
 * ```tsx
 * interface Events {
 *   'orders.created': { order_id: string; total: number };
 *   'orders.updated': { order_id: string; status: string };
 * }
 *
 * function Dashboard() {
 *   const { status } = useEventBus<Events>({
 *     url: '/events/sse',
 *     topics: ['orders.*'],
 *     handlers: {
 *       'orders.created': (data) => addOrder(data),
 *       'orders.updated': (data) => updateOrder(data.order_id, data.status),
 *     },
 *   });
 *
 *   return <div>Connection: {status}</div>;
 * }
 * ```
 */
export function useEventBus<TMap extends EventMap>(
  options: UseEventBusOptions<TMap>,
): UseEventBusReturn {
  const {
    url,
    topics,
    enabled = true,
    withCredentials,
    headers,
    maxReconnectAttempts = Infinity,
    reconnectDelay = 1000,
    maxReconnectDelay = 30_000,
  } = options;

  // -- State ---
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [lastEvent, setLastEvent] = useState<UseEventBusReturn["lastEvent"]>(null);

  // Bump this to force the effect to reconnect
  const [connectToken, setConnectToken] = useState(0);

  // -- Stable refs for callbacks (avoid reconnect on handler identity change) --
  const handlersRef = useRef(options.handlers);
  const onEventRef = useRef(options.onEvent);
  const onConnectRef = useRef(options.onConnect);
  const onDisconnectRef = useRef(options.onDisconnect);
  const onErrorRef = useRef(options.onError);

  // Sync refs every render
  handlersRef.current = options.handlers;
  onEventRef.current = options.onEvent;
  onConnectRef.current = options.onConnect;
  onDisconnectRef.current = options.onDisconnect;
  onErrorRef.current = options.onError;

  // Refs for imperative disconnect
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const manuallyDisconnectedRef = useRef(false);

  // -- Stable identity for topics array --
  const topicsKey = topics.join(",");
  const headersKey = headers ? JSON.stringify(headers) : "";

  // -- Main connection effect --
  useEffect(() => {
    // Don't connect if disabled or manually disconnected
    if (!enabled || manuallyDisconnectedRef.current) {
      if (abortRef.current) abortRef.current.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
      abortRef.current = null;
      timerRef.current = null;
      setStatus("disconnected");
      return;
    }

    let mounted = true;

    // Guard: only one reconnect timer can be pending at a time.
    let reconnectScheduled = false;

    function scheduleReconnect(): void {
      if (!mounted || reconnectScheduled) return;
      if (attemptRef.current >= maxReconnectAttempts) {
        setStatus("disconnected");
        return;
      }

      reconnectScheduled = true;
      const delay = Math.min(
        reconnectDelay * 2 ** attemptRef.current,
        maxReconnectDelay,
      );
      // Jitter: up to 20% of delay, capped at 1s
      const jitter = Math.random() * Math.min(delay * 0.2, 1000);
      attemptRef.current += 1;

      timerRef.current = setTimeout(() => {
        reconnectScheduled = false;
        if (mounted) startConnection();
      }, delay + jitter);
    }

    function startConnection(): void {
      if (!mounted) return;

      // Tear down previous connection / timer
      if (abortRef.current) abortRef.current.abort();
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      reconnectScheduled = false;

      const abort = new AbortController();
      abortRef.current = abort;

      setStatus(attemptRef.current === 0 ? "connecting" : "reconnecting");

      // Read url/topics/headers from the current closure — these are
      // captured fresh each time the effect runs so reconnects after
      // a dependency change always use the latest values.
      const fullURL = buildURL(url, topics);

      connectSSE({
        url: fullURL,
        signal: abort.signal,
        headers,
        withCredentials,

        onEvent(frame) {
          if (!mounted) return;

          try {
            const data: unknown = JSON.parse(frame.data);
            const topic = frame.event;
            const info: EventInfo = { eventId: frame.id, topic };

            setLastEvent({ topic, data, info });

            // Typed per-topic handler
            const handler = handlersRef.current?.[topic as keyof TMap];
            if (handler) {
              (handler as EventHandler<unknown>)(data, info);
            }

            // Catch-all
            onEventRef.current?.(topic, data, info);
          } catch (err: unknown) {
            onErrorRef.current?.(
              err instanceof Error
                ? err
                : new Error(`Failed to parse SSE data: ${err}`),
            );
          }
        },

        onOpen() {
          if (!mounted) return;
          attemptRef.current = 0;
          setStatus("connected");
          onConnectRef.current?.();
        },

        onClose() {
          if (!mounted) return;
          setStatus("disconnected");
          onDisconnectRef.current?.();
          scheduleReconnect();
        },

        onError(error) {
          if (!mounted) return;
          setStatus("disconnected");
          onErrorRef.current?.(error);
          onDisconnectRef.current?.();
          scheduleReconnect();
        },
      });
    }

    startConnection();

    return () => {
      mounted = false;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    enabled,
    url,
    topicsKey,
    withCredentials,
    headersKey,
    maxReconnectAttempts,
    reconnectDelay,
    maxReconnectDelay,
    connectToken,
  ]);

  // -- Imperative methods --

  const disconnect = useCallback(() => {
    manuallyDisconnectedRef.current = true;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    attemptRef.current = 0;
    setStatus("disconnected");
  }, []);

  const reconnect = useCallback(() => {
    manuallyDisconnectedRef.current = false;
    attemptRef.current = 0;
    // Trigger the effect to re-run by bumping the token
    setConnectToken((t) => t + 1);
  }, []);

  return { status, lastEvent, disconnect, reconnect };
}
