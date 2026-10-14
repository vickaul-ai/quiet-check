import "server-only";

import { backendToken, backendUrl } from "./env";
import type { CartLine, ConsoleEvent, Dispute, OrderDetail, OrderRow, Product } from "./types";

// Server-side calls to the FastAPI backend (docs/contracts.md). Errors keep the contract's
// `{error: {code, message}}` body so routes can pass the code on.
export class BackendError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<{ status: number; data: T }> {
  const res = await fetch(`${backendUrl()}${path}`, {
    method,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      // Hosted (D9): the backend's private routes need its API token.
      ...(backendToken() ? { Authorization: `Bearer ${backendToken()}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = data?.error ?? {};
    throw new BackendError(res.status, err.code ?? "backend_error", err.message ?? `Backend returned ${res.status}`);
  }
  return { status: res.status, data: data as T };
}

export async function getCatalog(): Promise<Product[]> {
  return (await call<{ items: Product[] }>("GET", "/catalog")).data.items;
}

export type CreatedOrder = {
  order_id: string;
  paypal_order_id: string;
  status: string;
  amount: { currency_code: string; value: string };
};

export async function createOrder(items: CartLine[], deviceId: string): Promise<CreatedOrder> {
  return (await call<CreatedOrder>("POST", "/payments/orders", { items, device_id: deviceId })).data;
}

// `onApprove`: starts the risk agent (a Render Workflows run when hosted, D9) and returns at once:
// 200 with the final status if already decided, otherwise 202 with the current status.
export async function startOrder(orderId: string): Promise<string> {
  return (await call<{ status: string }>("POST", "/merchant/start", { order_id: orderId })).data.status;
}

export async function getOrderStatus(orderId: string): Promise<string> {
  const path = `/merchant/orders/${encodeURIComponent(orderId)}/status`;
  return (await call<{ status: string }>("GET", path)).data.status;
}

// Merchant console (D6). The detail carries payer email and address; callers strip them (see lib/copilot.ts).
export async function listOrders(): Promise<OrderRow[]> {
  return (await call<{ orders: OrderRow[] }>("GET", "/merchant/orders")).data.orders;
}

export async function getOrder(orderId: string): Promise<OrderDetail & { payer_email?: unknown; shipping_address?: unknown }> {
  return (await call<OrderDetail>("GET", `/merchant/orders/${encodeURIComponent(orderId)}`)).data;
}

// Up to 50 events after `after`, oldest first.
export async function listEvents(after?: string): Promise<ConsoleEvent[]> {
  const query = after ? `?after=${encodeURIComponent(after)}` : "";
  return (await call<{ events: ConsoleEvent[] }>("GET", `/merchant/events${query}`)).data.events;
}

export type Resolution = { order_id: string; authorization_id: string; status: string; capture_id?: string };

// Reviewer actions on a held order. The backend enforces `held`, writes `resolution` and the events.
export async function resolveHold(action: "capture" | "void", authorizationId: string, note: string): Promise<Resolution> {
  const path = `/payments/authorizations/${encodeURIComponent(authorizationId)}/${action}`;
  return (await call<Resolution>("POST", path, { actor: "reviewer", note })).data;
}

// D8: the reviewer's click sends the (edited) evidence note to PayPal. The backend re-reads the dispute and refuses
// with 409 `dispute_not_awaiting_evidence` unless PayPal is asking the seller for evidence.
export async function submitDisputeEvidence(disputeId: string, note: string): Promise<Dispute> {
  const path = `/merchant/disputes/${encodeURIComponent(disputeId)}/evidence`;
  return (await call<Dispute>("POST", path, { note, actor: "reviewer" })).data;
}
