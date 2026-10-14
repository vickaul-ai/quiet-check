import { BackendError, listOrders } from "@/lib/backend";

// Console orders table -> GET /merchant/orders (newest 100; no payer details in the list).
export async function GET() {
  try {
    return Response.json({ orders: await listOrders() });
  } catch (err) {
    if (err instanceof BackendError) return Response.json({ error: { code: err.code } }, { status: err.status });
    throw err;
  }
}
