import { timingSafeEqual } from "node:crypto";

import { NextResponse, type NextRequest } from "next/server";

// The merchant console and its routes sit behind one shared password (D9, decided with Vic): HTTP
// Basic auth, any username, password CONSOLE_PASSWORD. Judges get it in the Devpost testing
// instructions. The shop stays public. Unset in production: the console is closed; unset in
// `next dev`: open, as before D9.
export function proxy(request: NextRequest) {
  const password = process.env.CONSOLE_PASSWORD;
  if (!password) {
    if (process.env.NODE_ENV !== "production") return NextResponse.next();
    return new NextResponse("The console is not configured.", { status: 503 });
  }
  if (passwordMatches(request.headers.get("authorization"), password)) return NextResponse.next();
  return new NextResponse("Merchant console: enter the demo password (any username).", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Quiet Check merchant console", charset="UTF-8"' },
  });
}

function passwordMatches(header: string | null, password: string): boolean {
  if (!header?.startsWith("Basic ")) return false;
  const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
  const given = Buffer.from(decoded.slice(decoded.indexOf(":") + 1));
  const expected = Buffer.from(password);
  return given.length === expected.length && timingSafeEqual(given, expected);
}

export const config = {
  matcher: ["/console", "/console/:path*", "/api/console/:path*"],
};
