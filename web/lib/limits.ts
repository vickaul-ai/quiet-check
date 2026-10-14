import "server-only";

import { clientIp } from "./client-ip";

// Rate limits (D9) for the model-backed chat routes and the storefront's `onApprove` route. The
// hosted demo has a $10 model budget, enforced as a spend limit on the Anthropic key; these limits
// spread it out. The visitor key comes from a header a client may be able to forge, so the
// per-visitor limit is about fairness; the hourly route-wide limits are what bound abuse, and an
// hourly window means a flood locks a route out for at most an hour. In memory, so they assume
// one web instance (Render Starter, one instance).
type Limit = { requests: number; windowMs: number };

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const PER_VISITOR: Limit = { requests: 20, windowMs: 10 * MINUTE };

export const LIMITS = {
  // The public shopping agent.
  chat: { perVisitor: PER_VISITOR, route: { requests: 150, windowMs: HOUR } },
  // The review copilot: behind the console password, with its own budget that a flood of public
  // chat can't use up.
  "console-chat": { perVisitor: PER_VISITOR, route: { requests: 150, windowMs: HOUR } },
  // onApprove: no model call, but each call reads the PayPal order and may start a workflow run.
  process: { perVisitor: PER_VISITOR, route: { requests: 600, windowMs: HOUR } },
};

const hits = new Map<string, number[]>();

// Forged visitor keys could grow the map without end: drop keys with no hit in the last hour.
function prune(now: number) {
  if (hits.size < 5_000) return;
  for (const [key, times] of hits) {
    if (!times.length || now - times[times.length - 1] > HOUR) hits.delete(key);
  }
}

function take(key: string, { requests, windowMs }: Limit, now: number): number {
  const recent = (hits.get(key) ?? []).filter((t) => now - t < windowMs);
  if (recent.length >= requests) {
    hits.set(key, recent);
    return Math.ceil((recent[0] + windowMs - now) / 1000);
  }
  recent.push(now);
  hits.set(key, recent);
  return 0;
}

// A 429 response if this request is over a limit, otherwise null (and the request is counted).
export function rateLimit(req: Request, route: keyof typeof LIMITS): Response | null {
  const now = Date.now();
  prune(now);
  const limits = LIMITS[route];
  const wait = take(`${route}:${clientIp(req)}`, limits.perVisitor, now) || take(`${route}:all`, limits.route, now);
  if (!wait) return null;
  return Response.json(
    { error: { code: "rate_limited", message: "The demo's request limit was reached. Try again later." } },
    { status: 429, headers: { "Retry-After": String(wait) } },
  );
}

// Text for the chat UI when the model call fails mid-stream. A spent model budget (the Anthropic
// spend limit) or provider rate limit gets a plain explanation; anything else stays generic.
export function chatErrorText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  if (/usage limit|credit balance|rate.?limit|overloaded|\b429\b|\b529\b/i.test(text)) {
    return "ai_unavailable: The demo's AI budget or rate limit is used up for now.";
  }
  return "chat_error";
}
