import "server-only";

import { backendUrl } from "./env";
import type { CartLine, Product } from "./types";

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
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
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

// `onApprove`: 200 with the final status, or 202 `checking` when the webhook got there first.
export async function processOrder(orderId: string): Promise<string> {
  return (await call<{ status: string }>("POST", "/merchant/process", { order_id: orderId })).data.status;
}

export async function getOrderStatus(orderId: string): Promise<string> {
  const path = `/merchant/orders/${encodeURIComponent(orderId)}/status`;
  return (await call<{ status: string }>("GET", path)).data.status;
}
