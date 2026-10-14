import { connection } from "next/server";
import { Suspense } from "react";

import { Storefront } from "@/components/storefront";
import { getCatalog } from "@/lib/backend";
import { paypalClientId } from "@/lib/env";
import { loadPersonas } from "@/lib/personas";

export default function Home() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-[var(--muted)]">Loading the shop…</p>}>
      <Shop />
    </Suspense>
  );
}

async function Shop() {
  await connection(); // catalogue, personas and env are read per request
  const [catalog, personas] = await Promise.all([getCatalog().catch(() => null), loadPersonas()]);
  if (!catalog) {
    return (
      <p className="p-8 text-sm">
        The shop backend isn&apos;t reachable. Start it with <code>make dev</code> (or set <code>BACKEND_URL</code>).
      </p>
    );
  }
  return <Storefront catalog={catalog} personas={personas} clientId={paypalClientId()} />;
}
