import { BackendError, startOrder } from "@/lib/backend";

// PayPal `onApprove` -> POST /merchant/start (D9). The browser gets the status only, then polls.
export async function POST(_req: Request, ctx: RouteContext<"/api/orders/[orderId]/process">) {
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
