import type { Metadata } from "next";

import { Console } from "@/components/console";

export const metadata: Metadata = {
  title: "Quiet Check merchant console",
  description: "Held orders, AP3 receipts, who saw what, and the review copilot",
};

// No gate until D9 (localhost only). All data comes from the /api/console routes, polled by the page.
export default function ConsolePage() {
  return <Console />;
}
