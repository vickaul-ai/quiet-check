import "server-only";

import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { tool } from "ai";
import { z } from "zod";

import { BackendError, getOrder, resolveHold } from "./backend";
import { REPO_DIR } from "./env";
import { logOrder } from "./log";
import type { OrderDetail } from "./types";

export async function copilotPrompt(): Promise<string> {
  return readFile(path.join(REPO_DIR, "prompts/review_copilot.md"), "utf8");
}

// The console needs no payer identifiers, so they stop here: neither the browser nor the model sees them.
export async function consoleOrder(orderId: string): Promise<OrderDetail> {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { payer_email, shipping_address, ...rest } = await getOrder(orderId);
  return rest;
}

// Signs each approval request so a modified client can't fabricate an Approve (AI SDK "Securing Approvals").
// TOOL_APPROVAL_SECRET must be shared once the web app runs on more than one instance; locally a per-process
// secret is enough (kept on globalThis so dev reloads don't invalidate pending approvals).
export function approvalSecret(): string {
  const g = globalThis as { __copilotApprovalSecret?: string };
  g.__copilotApprovalSecret ??= process.env.TOOL_APPROVAL_SECRET || randomBytes(32).toString("base64");
  return g.__copilotApprovalSecret;
}

function timeLeft(endsAt: string | null): string | null {
  if (!endsAt) return null;
  const ms = Date.parse(endsAt) - Date.now();
  if (ms <= 0) return "ended (reauthorise before capturing)";
  const hours = Math.floor(ms / 3_600_000);
  return `${Math.floor(hours / 24)} days ${hours % 24} hours`;
}

// What the model sees about an order: the decision, its rationale and the hit summary. No identifiers.
function summary(order: OrderDetail) {
  const hits = order.check?.hits ?? [];
  return {
    order_id: order.order_id,
    status: order.status,
    amount: `${order.amount.value} ${order.amount.currency_code}`,
    items: order.items.map((i) => `${i.quantity} x ${i.name}`),
    decision: order.decision?.decision ?? null,
    rationale: order.decision?.rationale ?? null,
    rationale_source: order.decision?.rationale_source ?? null,
    hit_count: order.check?.hit_count ?? null,
    matches: hits.filter((h) => h.is_match).map((h) => `Member ${h.member}: ${h.field}`),
    clear: hits.filter((h) => !h.is_match).map((h) => `Member ${h.member}: ${h.field}`),
    receipts_verified: `${order.receipts.filter((r) => r.verified).length} of ${order.receipts.length}`,
    authorization_id: order.authorization_id,
    capture_id: order.capture_id,
    honor_period_left: order.status === "held" ? timeLeft(order.honor_period_ends_at) : null,
    resolution: order.decision?.resolution ?? null,
    resolution_note: order.decision?.resolution_note ?? null,
  };
}

const ActionInput = z.object({
  order_id: z.string().describe("Our order ID (ord_...)"),
  note: z.string().max(500).default("").describe("The reviewer's reason, in their words; empty if they gave none"),
});

// One set of tools per chat request, scoped to the order open in the console. The two money tools need the
// reviewer's Approve (`toolApproval` in the route) and call our D3 endpoints, which enforce `held`.
export function copilotTools(selectedOrderId: string) {
  async function act(action: "capture" | "void", orderId: string, note: string) {
    if (orderId !== selectedOrderId) {
      logOrder(selectedOrderId, `copilot refused to ${action} another order`, "warn");
      return { error: `Only the order open in the console (${selectedOrderId}) can be resolved here.` };
    }
    try {
      const order = await getOrder(orderId);
      if (order.status !== "held" || !order.authorization_id) {
        logOrder(orderId, `copilot refused to ${action}: order is ${order.status}`, "warn");
        return { error: `Order ${orderId} is ${order.status}; only a held order can be captured or voided.` };
      }
      const result = await resolveHold(action, order.authorization_id, note);
      logOrder(orderId, `reviewer ${action} approved in the copilot (status ${result.status})`);
      return { order_id: result.order_id, status: result.status, capture_id: result.capture_id ?? null };
    } catch (err) {
      if (err instanceof BackendError) {
        logOrder(orderId, `reviewer ${action} failed (${err.status} ${err.code})`, "warn");
        return { error: `The ${action} failed (${err.code}): ${err.message}` };
      }
      throw err;
    }
  }

  return {
    get_order: tool({
      description:
        "Read an order's status, policy decision, rationale, consortium hit summary, receipt verification and " +
        "honour-period time left. Call it before explaining an order.",
      inputSchema: z.object({ order_id: z.string().describe("Our order ID (ord_...)") }),
      execute: async ({ order_id }) => {
        try {
          return summary(await consoleOrder(order_id));
        } catch (err) {
          if (err instanceof BackendError) return { error: `Couldn't read ${order_id} (${err.code}).` };
          throw err;
        }
      },
    }),

    capture_held_payment: tool({
      description:
        "Capture the held PayPal authorisation of a held order (the buyer is charged). Only on the reviewer's " +
        "explicit instruction; the reviewer then confirms it with Approve.",
      inputSchema: ActionInput,
      execute: async ({ order_id, note }) => act("capture", order_id, note),
    }),

    void_held_payment: tool({
      description:
        "Void the held PayPal authorisation of a held order (the funds are released and the order is cancelled). " +
        "Only on the reviewer's explicit instruction; the reviewer then confirms it with Approve.",
      inputSchema: ActionInput,
      execute: async ({ order_id, note }) => act("void", order_id, note),
    }),
  };
}
