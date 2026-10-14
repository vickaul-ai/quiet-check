import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";

import { Console } from "@/components/console";
import { loadPersonas } from "@/lib/personas";

export const metadata: Metadata = {
  title: "Quiet Check merchant console",
  description: "Held orders, AP3 receipts, who saw what, and the review copilot",
};

// Hosted, the proxy asks for CONSOLE_PASSWORD (D9). All data comes from the /api/console routes, polled by the
// page; the personas (names and device IDs only) label each order's buyer.
export default function ConsolePage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-[var(--muted)]">Loading the console…</p>}>
      <ConsoleWithPersonas />
    </Suspense>
  );
}

async function ConsoleWithPersonas() {
  await connection(); // read per request, like the shop
  return <Console personas={await loadPersonas()} hosted={!!process.env.RENDER} />;
}
