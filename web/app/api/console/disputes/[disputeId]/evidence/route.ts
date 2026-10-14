import { z } from "zod";

import { BackendError, submitDisputeEvidence } from "@/lib/backend";
import { logOrder } from "@/lib/log";
import { MAX_EVIDENCE_NOTE } from "@/lib/types";

// D8: the reviewer's "Submit evidence to PayPal" -> POST /merchant/disputes/{id}/evidence. Only this click
// submits; the model drafts the note and has no tool for it. `actor: "reviewer"` is added server-side.
const DISPUTE_ID = /^[A-Za-z0-9_-]{1,64}$/;
const Body = z.object({ note: z.string().trim().min(1).max(MAX_EVIDENCE_NOTE) });

export async function POST(req: Request, ctx: RouteContext<"/api/console/disputes/[disputeId]/evidence">) {
  const { disputeId } = await ctx.params;
  const parsed = Body.safeParse(await req.json().catch(() => null));
  if (!DISPUTE_ID.test(disputeId) || !parsed.success) {
    return Response.json({ error: { code: "validation_error" } }, { status: 422 });
  }
  try {
    const dispute = await submitDisputeEvidence(disputeId, parsed.data.note);
    if (dispute.order_id) logOrder(dispute.order_id, `dispute ${disputeId}: evidence submitted by the reviewer`);
    return Response.json(dispute);
  } catch (err) {
    if (err instanceof BackendError) {
      console.warn(`dispute ${disputeId}: evidence submit failed (${err.status} ${err.code})`);
      return Response.json({ error: { code: err.code } }, { status: err.status });
    }
    throw err;
  }
}
