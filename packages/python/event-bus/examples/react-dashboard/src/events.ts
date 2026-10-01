/**
 * Event type map matching the Python example server.
 *
 * Flow:
 *   POST /api/orders
 *     → orders.created      (published by API)
 *     → orders.confirmed    (published by order-processor consumer group)
 *     → inventory.reserved  (published by order-processor consumer group)
 *     → notifications.sent  (published by notification-service consumer group)
 */

export type ServerEvents = {
  "orders.created": {
    order_id: string;
    customer: string;
    items: Array<{ sku: string; qty: number }>;
    total: number;
  };

  "orders.confirmed": {
    order_id: string;
    status: string;
  };

  "inventory.reserved": {
    order_id: string;
    items: Array<{ sku: string; qty: number }>;
  };

  "notifications.sent": {
    order_id: string;
    channel: string;
    message: string;
  };
};

/** Union of all topic names */
export type ServerTopic = keyof ServerEvents;
