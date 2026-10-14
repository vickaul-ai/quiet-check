import { BackendError } from "@/lib/backend";
import { consoleOrder } from "@/lib/copilot";
import { logOrder } from "@/lib/log";

// Console order detail -> GET /merchant/orders/{id}, minus payer email and address.
export async function GET(_req: Request, ctx: RouteContext<"/api/console/orders/[orderId]">) {
  const { orderId } = await ctx.params;
  try {
    return Response.json(await consoleOrder(orderId));
  } catch (err) {
    if (err instanceof BackendError) {
      logOrder(orderId, `console detail failed (${err.status} ${err.code})`, "warn");
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
