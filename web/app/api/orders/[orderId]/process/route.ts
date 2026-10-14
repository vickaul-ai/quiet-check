import { BackendError, startOrder } from "@/lib/backend";
import { rateLimit } from "@/lib/limits";
import { logOrder } from "@/lib/log";

// PayPal `onApprove` -> POST /merchant/start (D9). The browser gets the status only, then polls.
export async function POST(req: Request, ctx: RouteContext<"/api/orders/[orderId]/process">) {
  // Each call reads the PayPal order and may start a workflow run: limit it per visitor.
  const limited = rateLimit(req, "process");
  if (limited) return limited;
  const { orderId } = await ctx.params;
  try {
    const status = await startOrder(orderId);
    logOrder(orderId, `onApprove: risk agent start requested (status ${status})`);
    return Response.json({ order_id: orderId, status });
  } catch (err) {
    if (err instanceof BackendError) {
      logOrder(orderId, `onApprove: start failed (${err.status} ${err.code})`, "warn");
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
