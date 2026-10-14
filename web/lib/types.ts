// Shapes shared by the server routes and the storefront (docs/contracts.md).

export type Money = { currency_code: string; value: string };

export type Product = { sku: string; name: string; unit_amount: Money; image?: string };

export type CartLine = { sku: string; quantity: number };

// What the browser gets about a persona: no email (that stays in .env).
export type Persona = { key: string; name: string; device_id: string; status: string };

export type CheckoutResult = {
  order_id: string;
  paypal_order_id: string;
  amount: Money;
  items: (CartLine & { name: string })[];
};

export const FINAL_STATUSES = ["captured", "held", "blocked", "voided"];

// Buyer-facing text per order status (contracts.md, "Order status").
export function buyerText(status: string): string {
  if (status === "captured") return "Paid";
  if (status === "held") return "Payment on hold for review";
  if (status === "blocked" || status === "voided") return "We couldn't complete this order";
  return "Processing";
}
