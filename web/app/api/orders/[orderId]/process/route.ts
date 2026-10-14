import { BackendError, startOrder } from "@/lib/backend";
import { rateLimit } from "@/lib/limits";

// PayPal `onApprove` -> POST /merchant/start (D9). The browser gets the status only, then polls.
export async function POST(req: Request, ctx: RouteContext<"/api/orders/[orderId]/process">) {
  // Each call reads the PayPal order and may start a workflow run: limit it per visitor.
  const limited = rateLimit(req, "process", { global: false });
  if (limited) return limited;
  const { orderId } = await ctx.params;
  try {
    return Response.json({ order_id: orderId, status: await startOrder(orderId) });
  } catch (err) {
    if (err instanceof BackendError) {
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
