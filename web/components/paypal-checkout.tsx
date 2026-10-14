"use client";

import { useEffect, useRef, useState } from "react";

import { buyerText, FINAL_STATUSES, type CheckoutResult } from "@/lib/types";

// PayPal JS SDK v6, as in PayPal's one-time payment guide: createInstance with the public client
// ID, then a one-time payment session started from the <paypal-button> click with the order ID.
// "popup" rather than the guide's "auto": auto may pick the browser PaymentRequest sheet, and the
// demo story (docs/brief.md) has the buyer approve in the PayPal pop-up.
const SDK_URL = "https://www.sandbox.paypal.com/web-sdk/v6/core"; // sandbox only (docs/brief.md)
const POLL_MS = 1500;
const POLL_LIMIT_MS = 120_000;

type Session = {
  start: (options: { presentationMode: "popup" }, order: Promise<{ orderId: string }>) => Promise<void>;
};
type SdkInstance = {
  findEligibleMethods: () => Promise<{ isEligible: (method: string) => boolean }>;
  createPayPalOneTimePaymentSession: (callbacks: {
    onApprove: (data: { orderId: string }) => Promise<void>;
    onCancel: () => void;
    onError: (err: Error) => void;
  }) => Promise<Session>;
};
declare global {
  interface Window {
    paypal?: { createInstance: (options: { clientId: string; components: string[] }) => Promise<SdkInstance> };
  }
}

let sdkPromise: Promise<SdkInstance> | null = null;

function loadSdk(clientId: string): Promise<SdkInstance> {
  sdkPromise ??= new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SDK_URL;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("PayPal SDK failed to load"));
    document.head.append(script);
  }).then(() => window.paypal!.createInstance({ clientId, components: ["paypal-payments"] }));
  sdkPromise.catch(() => (sdkPromise = null));
  return sdkPromise;
}

type Phase =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "cancelled" }
  | { kind: "error"; message: string }
  | { kind: "status"; status: string };

export function PayPalCheckout({ order, clientId }: { order: CheckoutResult; clientId: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  const { order_id: orderId, paypal_order_id: paypalOrderId } = order;

  // Keyed on the IDs, not the object: message re-renders must not remount the button mid-payment.
  useEffect(() => {
    let cancelled = false;
    const mount = container.current;
    if (!mount) return;

    async function track() {
      setPhase({ kind: "status", status: "checking" });
      let status = "checking";
      const res = await fetch(`/api/orders/${orderId}/process`, { method: "POST" });
      if (res.ok) status = (await res.json()).status;
      const started = Date.now();
      while (!cancelled && !FINAL_STATUSES.includes(status) && Date.now() - started < POLL_LIMIT_MS) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const poll = await fetch(`/api/orders/${orderId}/status`);
        if (poll.ok) status = (await poll.json()).status;
      }
      if (!cancelled) setPhase({ kind: "status", status });
    }

    (async () => {
      try {
        const sdk = await loadSdk(clientId);
        if (!(await sdk.findEligibleMethods()).isEligible("paypal")) {
          throw new Error("PayPal isn't available in this browser.");
        }
        const session = await sdk.createPayPalOneTimePaymentSession({
          onApprove: () => track(),
          onCancel: () => setPhase({ kind: "cancelled" }),
          onError: (err) => setPhase({ kind: "error", message: err.message || "PayPal reported an error." }),
        });
        if (cancelled) return;
        const button = document.createElement("paypal-button");
        // The order already exists (the agent's checkout tool made it), so pass a settled promise.
        button.addEventListener("click", () => {
          session.start({ presentationMode: "popup" }, Promise.resolve({ orderId: paypalOrderId })).catch(
            (err: Error) => setPhase({ kind: "error", message: err.message }),
          );
        });
        mount.replaceChildren(button);
        setPhase({ kind: "ready" });
      } catch (err) {
        if (!cancelled) setPhase({ kind: "error", message: (err as Error).message });
      }
    })();

    return () => {
      cancelled = true;
      mount.replaceChildren();
    };
  }, [orderId, paypalOrderId, clientId]);

  const done = phase.kind === "status";
  return (
    <div className="rounded-lg border border-[var(--line)] bg-[var(--panel)] p-3 text-sm">
      <ul className="mb-2 space-y-0.5">
        {order.items.map((item) => (
          <li key={item.sku}>
            {item.quantity} x {item.name}
          </li>
        ))}
      </ul>
      <p className="mb-3 font-semibold">
        Total {order.amount.value} {order.amount.currency_code}
      </p>
      <div ref={container} className={done ? "hidden" : "min-h-11"} />
      {phase.kind === "loading" && <p className="text-[var(--muted)]">Loading PayPal…</p>}
      {phase.kind === "cancelled" && <p className="mt-2 text-[var(--muted)]">Payment cancelled. You can try again.</p>}
      {phase.kind === "error" && <p className="mt-2 text-[var(--bad)]">{phase.message}</p>}
      {done && <OrderStatus status={phase.status} />}
    </div>
  );
}

function OrderStatus({ status }: { status: string }) {
  const tone =
    status === "captured" ? "text-[var(--good)]" : status === "held" ? "text-[var(--warn)]" : "text-[var(--bad)]";
  const final = FINAL_STATUSES.includes(status);
  return (
    <p role="status" className={`text-base font-semibold ${final ? tone : "text-[var(--muted)]"}`}>
      {buyerText(status)}
      {!final && "…"}
    </p>
  );
}
