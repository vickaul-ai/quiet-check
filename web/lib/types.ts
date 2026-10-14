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

// Short outcome labels for the persona switcher and the console's buyer names (presentation only).
export const PERSONA_OUTCOME: Record<string, string> = { captured: "clean", held: "hold", blocked: "block" };

// What happened behind the buyer's status, in the merchant's terms (shown under the buyer text).
export function behindTheScenes(status: string): string | null {
  if (status === "captured") return "No merchant had this buyer on its list, so the payment was authorised and captured.";
  if (status === "held") return "One merchant had a match, so the funds are authorised but not taken. A reviewer decides in the console.";
  if (status === "blocked") return "Two or more matches, so the merchant never authorised the payment. No money moved.";
  if (status === "voided") return "A reviewer voided the held payment, so the funds were released.";
  return null;
}

// Merchant console (D6, contracts.md "Read endpoints"). Payer email and address never reach the browser.
export type OrderRow = {
  order_id: string;
  status: string;
  decision: string | null;
  hit_count: number | null;
  amount: Money;
  paypal_order_id: string;
  authorization_id: string | null;
  capture_id: string | null;
  honor_period_ends_at: string | null;
  device_id: string; // the demo device the storefront recorded (names the persona)
  created_at: string;
};

export type Hit = { member: string; field: string; is_match: boolean };

export type Receipt = Hit & {
  receipt_id: string;
  check_id: string;
  session_id: string;
  intent_directives: unknown[];
  result_directive: unknown;
  member_commitment: unknown;
  member_public_key: string;
  verified: boolean;
  created_at: string;
};

export type Decision = {
  decision_id: string;
  check_id: string;
  decision: string;
  rationale: string;
  rationale_source: string | null;
  paypal_action: string;
  resolution: string | null;
  resolution_note: string | null;
  resolved_at: string | null;
  created_at: string;
};

export type WhoSawWhat = { party: string; sent: string; learned: string; never_saw: string };

export type OrderDetail = Omit<OrderRow, "decision"> & {
  items: (CartLine & { name: string; unit_amount: Money })[];
  device_id: string;
  check: { check_id: string; hits: Hit[]; hit_count: number } | null;
  receipts: Receipt[];
  decision: Decision | null;
  who_saw_what: WhoSawWhat[];
};

export type ConsoleEvent = {
  event_id: string;
  source: string;
  event_type: string;
  order_id: string | null;
  paypal_event_id: string | null;
  resource_id: string | null;
  verified: boolean | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export const FIELD_LABELS: Record<string, string> = {
  email: "email",
  shipping_address: "address",
  device_id: "device",
};
