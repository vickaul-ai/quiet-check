import { BackendError, processOrder } from "@/lib/backend";

// PayPal `onApprove` -> POST /merchant/process. The browser gets the status only.
export async function POST(_req: Request, ctx: RouteContext<"/api/orders/[orderId]/process">) {
  const { orderId } = await ctx.params;
  try {
    return Response.json({ order_id: orderId, status: await processOrder(orderId) });
  } catch (err) {
    if (err instanceof BackendError) {
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
