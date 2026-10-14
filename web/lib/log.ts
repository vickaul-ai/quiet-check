import "server-only";

// Order lines on the web server, in the backend's format ("order <id>: ..."), so one Render log search for an
// order ID shows its whole path across web, backend and workflow (D10). Never log payer details.
// Public routes take the ID from the URL, so anything that isn't an ID is logged as "(invalid id)":
// a crafted path can't forge log lines.
const ORDER_ID = /^ord_[0-9A-Za-z]{1,64}$/;

export function logOrder(orderId: string, message: string, level: "info" | "warn" = "info") {
  const id = ORDER_ID.test(orderId) ? orderId : "(invalid id)";
  (level === "warn" ? console.warn : console.info)(`order ${id}: ${message}`);
}
