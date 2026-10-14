// The visitor's IP, for per-visitor rate limits (D9). Render puts the client IP first in
// X-Forwarded-For (a Render staff answer, not documented); a client can probably prepend its own
// value, so this only spreads the budget fairly. The global limit in lib/limits.ts is the real
// bound. Locally there is no header, so every request shares one key.
export function clientIp(req: Request): string {
  const forwarded = req.headers.get("x-forwarded-for");
  return forwarded?.split(",")[0]?.trim() || "local";
}
