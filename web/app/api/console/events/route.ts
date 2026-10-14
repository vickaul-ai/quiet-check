import { BackendError, listEvents } from "@/lib/backend";
import type { ConsoleEvent } from "@/lib/types";

// Webhook payloads hold the full PayPal resource (payer email, address), so only these keys reach the browser.
const PAYLOAD_KEYS = [
  "summary", "decision", "hit_count", "verified", "check_id", "rationale_source", "paypal_action", "actor", "note", "capture_id",
  "capture_status", "authorization_id", "authorization_status", "action", "name", "debug_id", "issue", "error", "runner",
  "task_run_id",
];

function slim(e: ConsoleEvent): ConsoleEvent {
  const payload = Object.fromEntries(PAYLOAD_KEYS.filter((k) => k in (e.payload ?? {})).map((k) => [k, e.payload[k]]));
  return { ...e, payload };
}

// Live feed -> GET /merchant/events?after= (up to 50, oldest first). The console pages to the newest on load.
export async function GET(req: Request) {
  const after = new URL(req.url).searchParams.get("after") ?? undefined;
  if (after !== undefined && !/^evt_[0-9A-Za-z]{1,64}$/.test(after)) {
    return Response.json({ error: { code: "validation_error" } }, { status: 422 });
  }
  try {
    return Response.json({ events: (await listEvents(after)).map(slim) });
  } catch (err) {
    if (err instanceof BackendError) return Response.json({ error: { code: err.code } }, { status: err.status });
    throw err;
  }
}
