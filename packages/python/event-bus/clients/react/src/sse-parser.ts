/**
 * Low-level SSE stream parser using the Fetch API.
 *
 * Uses fetch + ReadableStream instead of native EventSource so we get:
 * - Custom headers (Authorization, etc.)
 * - Catch-all event handling (EventSource only dispatches to named listeners)
 * - Full control over reconnect behavior
 *
 * @module
 */

import type { SSEFrame } from "./types";

export interface SSEConnectionOptions {
  /** Full URL to the SSE endpoint (including query params) */
  url: string;
  /** AbortSignal for cancellation */
  signal: AbortSignal;
  /** Custom HTTP headers (e.g. Authorization) */
  headers?: Record<string, string>;
  /** Send cookies with the request */
  withCredentials?: boolean;
  /** Called for each parsed SSE event */
  onEvent: (frame: SSEFrame) => void;
  /** Called when the connection opens successfully */
  onOpen: () => void;
  /** Called when the stream ends cleanly (server closed) */
  onClose: () => void;
  /** Called on fetch or parse errors */
  onError: (error: Error) => void;
}

/**
 * Open an SSE connection using fetch and parse the text/event-stream
 * response frame by frame.  Returns when the stream ends or is aborted.
 */
export async function connectSSE(options: SSEConnectionOptions): Promise<void> {
  const {
    url,
    signal,
    headers,
    withCredentials,
    onEvent,
    onOpen,
    onClose,
    onError,
  } = options;

  // -- Connect --

  let response: Response;
  try {
    response = await fetch(url, {
      signal,
      credentials: withCredentials ? "include" : "same-origin",
      headers: {
        Accept: "text/event-stream",
        "Cache-Control": "no-cache",
        ...headers,
      },
    });
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    onError(err instanceof Error ? err : new Error(String(err)));
    return;
  }

  if (!response.ok) {
    onError(
      new Error(
        `SSE connection failed: ${response.status} ${response.statusText}`,
      ),
    );
    return;
  }

  if (!response.body) {
    onError(new Error("Response body is null (ReadableStream not supported)"));
    return;
  }

  onOpen();

  // -- Parse SSE frames --

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Current frame being assembled
  let frameId = "";
  let frameEvent = "message";
  let frameDataParts: string[] = [];

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE lines can end with \n, \r, or \r\n
      const lines = buffer.split(/\r\n|\r|\n/);
      // Last element is the incomplete line — keep in buffer
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line === "") {
          // Empty line = end of frame, dispatch if we have data
          if (frameDataParts.length > 0) {
            onEvent({
              id: frameId,
              event: frameEvent,
              data: frameDataParts.join("\n"),
            });
          }
          // Reset for next frame
          frameId = "";
          frameEvent = "message";
          frameDataParts = [];
          continue;
        }

        if (line.startsWith(":")) {
          // Comment line (heartbeat), silently ignore
          continue;
        }

        // Parse "field: value" or "field" (no colon)
        const colonIdx = line.indexOf(":");
        let field: string;
        let value: string;

        if (colonIdx === -1) {
          field = line;
          value = "";
        } else {
          field = line.slice(0, colonIdx);
          value = line.slice(colonIdx + 1);
          // Strip single leading space after colon per SSE spec
          if (value.startsWith(" ")) value = value.slice(1);
        }

        switch (field) {
          case "id":
            frameId = value;
            break;
          case "event":
            frameEvent = value;
            break;
          case "data":
            frameDataParts.push(value);
            break;
          case "retry":
            // Server-suggested retry interval — we manage our own backoff
            break;
        }
      }
    }
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    onError(err instanceof Error ? err : new Error(String(err)));
    return;
  } finally {
    reader.releaseLock();
  }

  // Stream ended cleanly (server closed the response)
  onClose();
}
