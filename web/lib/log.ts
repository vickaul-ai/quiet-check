import "server-only";

// Order lines on the web server, in the backend's format ("order <id>: ..."), so one Render log search for an
// order ID shows its whole path across web, backend and workflow (D10). Never log payer details.
export function logOrder(orderId: string, message: string, level: "info" | "warn" = "info") {
  (level === "warn" ? console.warn : console.info)(`order ${orderId}: ${message}`);
}
