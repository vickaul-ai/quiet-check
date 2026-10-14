import "server-only";

import { clientIp } from "./client-ip";

// Rate limits for the two model-backed chat routes (D9). The hosted demo has a $10 model budget,
// enforced as a spend limit on the Anthropic key; these limits spread it out and stop one visitor
// using it all. In memory, so they assume one web instance (Render Starter, one instance).
export const LIMITS = {
  perVisitor: { requests: 20, windowMs: 10 * 60_000 }, // per IP, per route
  global: { requests: 600, windowMs: 24 * 60 * 60_000 }, // all visitors, both routes together
};

const hits = new Map<string, number[]>();

function take(key: string, { requests, windowMs }: { requests: number; windowMs: number }, now: number): number {
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
export function rateLimit(req: Request, route: string): Response | null {
  const now = Date.now();
  const wait = take(`${route}:${clientIp(req)}`, LIMITS.perVisitor, now) || take("global", LIMITS.global, now);
  if (!wait) return null;
  return Response.json(
    { error: { code: "rate_limited", message: "The demo's chat limit was reached. Try again later." } },
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
