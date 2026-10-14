import { BackendError, getOrderStatus } from "@/lib/backend";
import { logOrder } from "@/lib/log";

// Polled by the storefront after onApprove -> GET /merchant/orders/{id}/status (status only).
export async function GET(_req: Request, ctx: RouteContext<"/api/orders/[orderId]/status">) {
  const { orderId } = await ctx.params;
  try {
    return Response.json({ order_id: orderId, status: await getOrderStatus(orderId) });
  } catch (err) {
    if (err instanceof BackendError) {
      logOrder(orderId, `status poll failed (${err.status} ${err.code})`, "warn");
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
