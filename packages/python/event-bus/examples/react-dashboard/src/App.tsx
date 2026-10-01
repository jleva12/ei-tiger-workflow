import { useState } from "react";
import { useEventBus } from "@event-bus/react";
import type { ServerEvents } from "./events";

// ---------------------------------------------------------------------------
// Types for local state
// ---------------------------------------------------------------------------

interface Order {
  order_id: string;
  customer: string;
  items: Array<{ sku: string; qty: number }>;
  total: number;
  status: "created" | "confirmed";
  notifications: string[];
  inventoryReserved: boolean;
}

interface EventLogEntry {
  id: number;
  topic: string;
  data: unknown;
  timestamp: Date;
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

let nextEventId = 1;

export default function App() {
  const [orders, setOrders] = useState<Map<string, Order>>(new Map());
  const [eventLog, setEventLog] = useState<EventLogEntry[]>([]);
  const [creating, setCreating] = useState(false);

  // --- Hook ---
  // Handlers are passed inline — the hook stores them in refs internally,
  // so new function identity on every render does NOT cause a reconnect.

  const { status, disconnect, reconnect } = useEventBus<ServerEvents>({
    url: "/events/sse",
    topics: ["**"],
    handlers: {
      "orders.created": (data) => {
        setOrders((prev) => {
          const next = new Map(prev);
          next.set(data.order_id, {
            ...data,
            status: "created",
            notifications: [],
            inventoryReserved: false,
          });
          return next;
        });
      },

      "orders.confirmed": (data) => {
        setOrders((prev) => {
          const next = new Map(prev);
          const existing = next.get(data.order_id);
          if (existing) {
            next.set(data.order_id, { ...existing, status: "confirmed" });
          }
          return next;
        });
      },

      "inventory.reserved": (data) => {
        setOrders((prev) => {
          const next = new Map(prev);
          const existing = next.get(data.order_id);
          if (existing) {
            next.set(data.order_id, { ...existing, inventoryReserved: true });
          }
          return next;
        });
      },

      "notifications.sent": (data) => {
        setOrders((prev) => {
          const next = new Map(prev);
          const existing = next.get(data.order_id);
          if (existing) {
            next.set(data.order_id, {
              ...existing,
              notifications: [...existing.notifications, data.message],
            });
          }
          return next;
        });
      },
    },
    onEvent(topic, data) {
      setEventLog((prev) => [
        { id: nextEventId++, topic, data, timestamp: new Date() },
        ...prev.slice(0, 99), // keep last 100
      ]);
    },
  });

  // --- Create order ---

  const createOrder = async () => {
    setCreating(true);
    try {
      const res = await fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer: "dashboard-user",
          items: [
            { sku: "WIDGET-A", qty: Math.ceil(Math.random() * 5) },
            { sku: "GADGET-B", qty: Math.ceil(Math.random() * 3) },
          ],
          total: Math.round(Math.random() * 500 * 100) / 100,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error("Failed to create order:", err);
    } finally {
      setCreating(false);
    }
  };

  // --- Render ---

  const orderList = Array.from(orders.values()).reverse();

  return (
    <div style={styles.container}>
      <h1 style={styles.title}>Event Bus Dashboard</h1>

      {/* Connection bar */}
      <div style={styles.topBar}>
        <StatusBadge status={status} />
        <div style={styles.buttonGroup}>
          {status === "connected" ? (
            <button onClick={disconnect} style={styles.btnDanger}>
              Disconnect
            </button>
          ) : (
            <button onClick={reconnect} style={styles.btnPrimary}>
              Reconnect
            </button>
          )}
          <button
            onClick={createOrder}
            disabled={creating || status !== "connected"}
            style={{
              ...styles.btnPrimary,
              opacity: creating || status !== "connected" ? 0.5 : 1,
            }}
          >
            {creating ? "Creating..." : "Create Order"}
          </button>
        </div>
      </div>

      <div style={styles.grid}>
        {/* Orders panel */}
        <div style={styles.panel}>
          <h2 style={styles.panelTitle}>
            Orders ({orderList.length})
          </h2>
          <div style={styles.scrollArea}>
            {orderList.length === 0 && (
              <p style={styles.empty}>
                No orders yet. Click "Create Order" to start.
              </p>
            )}
            {orderList.map((order) => (
              <OrderCard key={order.order_id} order={order} />
            ))}
          </div>
        </div>

        {/* Event log panel */}
        <div style={styles.panel}>
          <h2 style={styles.panelTitle}>
            Event Log ({eventLog.length})
          </h2>
          <div style={styles.scrollArea}>
            {eventLog.length === 0 && (
              <p style={styles.empty}>Waiting for events...</p>
            )}
            {eventLog.map((entry) => (
              <div key={entry.id} style={styles.logEntry}>
                <div style={styles.logHeader}>
                  <TopicBadge topic={entry.topic} />
                  <span style={styles.timestamp}>
                    {entry.timestamp.toLocaleTimeString()}
                  </span>
                </div>
                <pre style={styles.logData}>
                  {JSON.stringify(entry.data, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "connected"
      ? "#22c55e"
      : status === "connecting" || status === "reconnecting"
        ? "#f59e0b"
        : "#ef4444";

  return (
    <span style={{ ...styles.badge, backgroundColor: color }}>
      {status}
    </span>
  );
}

function TopicBadge({ topic }: { topic: string }) {
  const colors: Record<string, string> = {
    orders: "#3b82f6",
    inventory: "#8b5cf6",
    notifications: "#06b6d4",
  };
  const prefix = topic.split(".")[0];
  const bg = colors[prefix] ?? "#6b7280";

  return (
    <span style={{ ...styles.topicBadge, backgroundColor: bg }}>
      {topic}
    </span>
  );
}

function OrderCard({ order }: { order: Order }) {
  return (
    <div style={styles.orderCard}>
      <div style={styles.orderHeader}>
        <strong>{order.order_id}</strong>
        <span
          style={{
            ...styles.statusChip,
            backgroundColor:
              order.status === "confirmed" ? "#22c55e" : "#f59e0b",
          }}
        >
          {order.status}
        </span>
      </div>
      <div style={styles.orderBody}>
        <div>Customer: {order.customer}</div>
        <div>Total: ${order.total.toFixed(2)}</div>
        <div>
          Items:{" "}
          {order.items.map((i) => `${i.sku} x${i.qty}`).join(", ")}
        </div>
        <div style={styles.orderFlags}>
          <Flag ok={order.status === "confirmed"} label="Confirmed" />
          <Flag ok={order.inventoryReserved} label="Inventory" />
          <Flag ok={order.notifications.length > 0} label="Notified" />
        </div>
      </div>
    </div>
  );
}

function Flag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span style={{ color: ok ? "#22c55e" : "#9ca3af", fontSize: 13 }}>
      {ok ? "\u2713" : "\u2717"} {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Styles (inline to keep the example self-contained)
// ---------------------------------------------------------------------------

const styles: Record<string, React.CSSProperties> = {
  container: {
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    maxWidth: 1200,
    margin: "0 auto",
    padding: "24px 16px",
    color: "#e5e7eb",
    backgroundColor: "#111827",
    minHeight: "100vh",
  },
  title: {
    fontSize: 28,
    fontWeight: 700,
    marginBottom: 16,
    color: "#f9fafb",
  },
  topBar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 16px",
    backgroundColor: "#1f2937",
    borderRadius: 8,
    marginBottom: 20,
  },
  buttonGroup: { display: "flex", gap: 8 },
  btnPrimary: {
    padding: "8px 16px",
    borderRadius: 6,
    border: "none",
    backgroundColor: "#3b82f6",
    color: "#fff",
    fontWeight: 600,
    cursor: "pointer",
    fontSize: 14,
  },
  btnDanger: {
    padding: "8px 16px",
    borderRadius: 6,
    border: "none",
    backgroundColor: "#ef4444",
    color: "#fff",
    fontWeight: 600,
    cursor: "pointer",
    fontSize: 14,
  },
  badge: {
    padding: "4px 12px",
    borderRadius: 12,
    color: "#fff",
    fontWeight: 600,
    fontSize: 13,
    textTransform: "uppercase" as const,
    letterSpacing: 0.5,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 20,
  },
  panel: {
    backgroundColor: "#1f2937",
    borderRadius: 8,
    padding: 16,
    display: "flex",
    flexDirection: "column" as const,
  },
  panelTitle: {
    fontSize: 18,
    fontWeight: 600,
    marginBottom: 12,
    color: "#f9fafb",
  },
  scrollArea: {
    flex: 1,
    maxHeight: "70vh",
    overflowY: "auto" as const,
  },
  empty: { color: "#6b7280", fontStyle: "italic" },
  orderCard: {
    backgroundColor: "#374151",
    borderRadius: 8,
    padding: 12,
    marginBottom: 10,
  },
  orderHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 8,
  },
  orderBody: {
    fontSize: 14,
    color: "#d1d5db",
    display: "flex",
    flexDirection: "column" as const,
    gap: 4,
  },
  orderFlags: { display: "flex", gap: 12, marginTop: 6 },
  statusChip: {
    padding: "2px 8px",
    borderRadius: 10,
    color: "#fff",
    fontSize: 12,
    fontWeight: 600,
  },
  logEntry: {
    backgroundColor: "#374151",
    borderRadius: 6,
    padding: 10,
    marginBottom: 8,
  },
  logHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 6,
  },
  topicBadge: {
    padding: "2px 8px",
    borderRadius: 10,
    color: "#fff",
    fontSize: 12,
    fontWeight: 600,
  },
  timestamp: { color: "#9ca3af", fontSize: 12 },
  logData: {
    backgroundColor: "#1f2937",
    borderRadius: 4,
    padding: 8,
    fontSize: 12,
    color: "#a5b4fc",
    overflow: "auto",
    margin: 0,
  },
};
