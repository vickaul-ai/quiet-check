"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import Image from "next/image";
import { useEffect, useRef, useState } from "react";

import { PayPalCheckout } from "@/components/paypal-checkout";
import { chatErrorMessage } from "@/lib/chat-errors";
import {
  buyerText,
  PERSONA_OUTCOME,
  type CartLine,
  type CheckoutResult,
  type Persona,
  type Product,
} from "@/lib/types";

const MAX_QUANTITY = 10;

type Props = { catalog: Product[]; personas: Persona[]; clientId: string };

export function Storefront({ catalog, personas, clientId }: Props) {
  const [personaKey, setPersonaKey] = useState(personas[0]?.key ?? "");
  const persona = personas.find((p) => p.key === personaKey);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 px-4 py-4 sm:py-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">
            <span className="text-[var(--accent)]">Quiet Check</span> demo shop
          </h1>
          <p className="text-sm text-[var(--muted)]">
            An AI shopping agent buys for you. Before PayPal moves any money, the merchant privately checks you against
            3 other merchants&apos; fraud lists.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <PersonaSwitcher personas={personas} value={personaKey} onChange={setPersonaKey} />
          {/* A full page load, so the browser asks for the console password (D9); a client-side
              navigation or prefetch would only get the 401. */}
          <a
            href="/console"
            className="rounded-md border border-[var(--accent)] px-3 py-1.5 text-sm font-medium text-[var(--accent)] hover:bg-[var(--accent-soft)]"
          >
            Merchant console
          </a>
        </div>
      </header>
      <ol className="grid gap-2 text-xs sm:grid-cols-3" aria-label="How the demo works">
        {[
          ["1", "Ask the agent to buy something", "It builds the cart and creates a PayPal order."],
          ["2", "Approve in the PayPal pop-up", "Log in as the persona's sandbox buyer."],
          ["3", "The merchant checks privately", "Capture, hold for review or decline. See why in the console."],
        ].map(([n, title, body]) => (
          <li key={n} className="flex gap-2 rounded-md border border-[var(--line)] px-3 py-2">
            <span className="font-mono font-semibold text-[var(--accent)]">{n}</span>
            <span>
              <strong className="font-semibold">{title}.</strong> <span className="text-[var(--muted)]">{body}</span>
            </span>
          </li>
        ))}
      </ol>
      {persona && (
        <div className="rounded-md bg-[var(--panel)] px-3 py-2 text-xs">
          <p>
            Shopping as <strong>{persona.name}</strong>. Expect: <strong>{buyerText(persona.status)}</strong>.{" "}
            <span className="text-[var(--muted)]">{PERSONA_NOTE[persona.status] ?? ""}</span>
          </p>
          <details className="mt-1 text-[var(--muted)]">
            <summary className="cursor-pointer">PayPal sandbox tips</summary>
            <p className="mt-1">
              Log in to PayPal as {persona.name}&apos;s sandbox buyer (judges: the logins are in the testing
              instructions). PayPal reuses the last login, so log out at www.sandbox.paypal.com before switching
              persona, or use a fresh private window. Device <code>{persona.device_id}</code>. Short on time? The
              merchant console already has orders for all three buyers.
            </p>
          </details>
        </div>
      )}
      {/* Keyed on the persona: switching starts a fresh chat and cart. */}
      <Shop key={personaKey} catalog={catalog} personaKey={personaKey} clientId={clientId} />
    </div>
  );
}

// Why each persona gets its outcome (the seeds in backend/app/personas.json).
const PERSONA_NOTE: Record<string, string> = {
  captured: "No merchant has this buyer on its list.",
  held: "Member B has this buyer's email on its list.",
  blocked: "Members A and C have this buyer's email and device on their lists.",
};

function PersonaSwitcher({
  personas,
  value,
  onChange,
}: {
  personas: Persona[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div role="radiogroup" aria-label="Persona" className="flex gap-1 rounded-lg border border-[var(--line)] p-1">
      {personas.map((p) => (
        <button
          key={p.key}
          role="radio"
          aria-checked={p.key === value}
          onClick={() => onChange(p.key)}
          className={`rounded-md px-3 py-1.5 text-sm ${
            p.key === value ? "bg-[var(--fg)] text-[var(--bg)]" : "hover:bg-[var(--panel)]"
          }`}
        >
          {p.name}
          {PERSONA_OUTCOME[p.status] && (
            <span className={p.key === value ? "opacity-70" : "text-[var(--muted)]"}> · {PERSONA_OUTCOME[p.status]}</span>
          )}
        </button>
      ))}
    </div>
  );
}

function Shop({ catalog, personaKey, clientId }: { catalog: Product[]; personaKey: string; clientId: string }) {
  const [cart, setCart] = useState<CartLine[]>([]);
  const [input, setInput] = useState("");
  const seen = useRef(new Set<string>());
  const [ordered, setOrdered] = useState(false);
  const { messages, sendMessage, status, error } = useChat({
    transport: new DefaultChatTransport({ api: "/api/chat" }),
  });
  const busy = status === "submitted" || status === "streaming";

  // The cart lives here; the agent's tools return the new cart, which we adopt once per call.
  useEffect(() => {
    for (const message of messages) {
      for (const part of message.parts) {
        const tool = asTool(part);
        if (!tool || tool.state !== "output-available" || seen.current.has(tool.toolCallId)) continue;
        seen.current.add(tool.toolCallId);
        const output = tool.output as { cart?: CartLine[]; order_id?: string };
        // Syncing from the agent's tool results, which arrive with the streamed messages.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        if (tool.type === "tool-add_to_cart" && output.cart) setCart(output.cart);
        if (tool.type === "tool-checkout" && output.order_id) {
          setCart([]);
          setOrdered(true);
        }
      }
    }
  }, [messages]);

  function send(text: string) {
    if (!text.trim() || busy) return;
    sendMessage({ text }, { body: { persona: personaKey, cart } });
  }

  function add(sku: string) {
    setOrdered(false);
    setCart((lines) => {
      const line = lines.find((l) => l.sku === sku);
      if (!line) return [...lines, { sku, quantity: 1 }];
      return lines.map((l) => (l.sku === sku ? { ...l, quantity: Math.min(MAX_QUANTITY, l.quantity + 1) } : l));
    });
  }

  return (
    <div className="grid flex-1 gap-4 lg:grid-cols-[1fr_400px]">
      <section aria-label="Products" className="flex flex-col gap-4">
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
          {catalog.map((p, i) => (
            <li key={p.sku} className="flex flex-col overflow-hidden rounded-lg border border-[var(--line)]">
              {p.image && (
                <Image
                  src={p.image}
                  alt=""
                  width={400}
                  height={400}
                  loading={i < 4 ? "eager" : "lazy"}
                  className="aspect-square w-full"
                />
              )}
              <div className="flex flex-1 flex-col gap-1 p-2.5">
                <p className="text-sm leading-snug">{p.name}</p>
                <div className="mt-auto flex items-center justify-between pt-1">
                  <span className="text-sm font-semibold">${p.unit_amount.value}</span>
                  <button
                    onClick={() => add(p.sku)}
                    aria-label={`Add ${p.name} to the cart`}
                    className="rounded-md border border-[var(--line)] px-2 py-1 text-xs hover:bg-[var(--panel)]"
                  >
                    Add
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
        <Cart
          cart={cart}
          catalog={catalog}
          ordered={ordered}
          onCheckout={() => send("Check out my cart.")}
          busy={busy}
        />
      </section>

      {/* On phones the agent comes first: it's where the demo starts. */}
      <section
        aria-label="Shopping agent"
        className="order-first flex min-h-[420px] flex-col rounded-lg border border-[var(--line)] lg:order-none lg:sticky lg:top-4 lg:h-[calc(100vh-2rem)]"
      >
        <div className="border-b border-[var(--line)] px-3 py-2">
          <p className="text-sm font-semibold">Shopping agent</p>
          <p className="text-xs text-[var(--muted)]">Claude finds the item, fills the cart and creates the PayPal order.</p>
        </div>
        <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-3" aria-live="polite">
          {messages.length === 0 && (
            <div className="text-sm text-[var(--muted)]">
              <p className="mb-2">Try one, or type your own:</p>
              <div className="flex flex-wrap gap-2">
                {["Buy the blue backpack in medium", "Buy the green duffel"].map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="rounded-full border border-[var(--line)] px-3 py-1 text-[var(--fg)] hover:bg-[var(--panel)]"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m) => (
            <Message key={m.id} message={m} clientId={clientId} />
          ))}
          {status === "submitted" && <p className="text-sm text-[var(--muted)]">Thinking…</p>}
          {error && <p className="text-sm text-[var(--bad)]">{chatErrorMessage(error, "The agent")}</p>}
        </div>
        <form
          className="flex gap-2 border-t border-[var(--line)] p-2"
          onSubmit={(e) => {
            e.preventDefault();
            send(input);
            setInput("");
          }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.currentTarget.value)}
            placeholder="Ask for something…"
            aria-label="Message the shopping agent"
            className="min-w-0 flex-1 rounded-md border border-[var(--line)] bg-transparent px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-md bg-[var(--accent)] px-3 py-2 text-sm font-medium text-[var(--accent-fg)] disabled:opacity-50"
          >
            Send
          </button>
        </form>
      </section>
    </div>
  );
}

function Cart({
  cart,
  catalog,
  ordered,
  onCheckout,
  busy,
}: {
  cart: CartLine[];
  catalog: Product[];
  ordered: boolean;
  onCheckout: () => void;
  busy: boolean;
}) {
  if (!cart.length) {
    return (
      <p className="text-sm text-[var(--muted)]">
        {ordered ? "Order created. Approve it with the PayPal button in the chat." : "Your cart is empty."}
      </p>
    );
  }
  const rows = cart.map((l) => ({ ...l, product: catalog.find((p) => p.sku === l.sku) }));
  // Display only: the backend prices the order.
  const total = rows.reduce((sum, r) => sum + Number(r.product?.unit_amount.value ?? 0) * r.quantity, 0);
  return (
    <div className="rounded-lg border border-[var(--line)] p-3 text-sm">
      <p className="mb-1 font-semibold">Cart</p>
      <ul className="mb-2">
        {rows.map((r) => (
          <li key={r.sku}>
            {r.quantity} x {r.product?.name ?? r.sku}
          </li>
        ))}
      </ul>
      <div className="flex items-center justify-between">
        <span>Total ${total.toFixed(2)}</span>
        <button
          onClick={onCheckout}
          disabled={busy}
          className="rounded-md bg-[var(--accent)] px-3 py-1.5 font-medium text-[var(--accent-fg)] disabled:opacity-50"
        >
          Ask the agent to check out
        </button>
      </div>
    </div>
  );
}

type ToolPart = { type: string; toolCallId: string; state: string; input?: unknown; output?: unknown };

function asTool(part: UIMessage["parts"][number]): ToolPart | null {
  return part.type.startsWith("tool-") ? (part as unknown as ToolPart) : null;
}

function Message({ message, clientId }: { message: UIMessage; clientId: string }) {
  const mine = message.role === "user";
  return (
    <div className={`flex flex-col gap-2 ${mine ? "items-end" : "items-start"}`}>
      {/* The PayPal card goes last, so the agent's "press the PayPal button" text sits above it. */}
      {orderedParts(message.parts).map(({ part, i }) => {
        const key = `${message.id}-${i}`;
        if (part.type === "text") {
          if (!part.text.trim()) return null;
          return (
            <p
              key={key}
              className={`max-w-[90%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                mine ? "bg-[var(--accent)] text-[var(--accent-fg)]" : "bg-[var(--panel)]"
              }`}
            >
              {part.text}
            </p>
          );
        }
        const tool = asTool(part);
        if (!tool) return null;
        if (tool.type === "tool-checkout" && tool.state === "output-available") {
          const output = tool.output as CheckoutResult | { error: string };
          if ("error" in output) return <ToolNote key={key} text={output.error} />;
          return (
            <div key={key} className="w-full">
              <PayPalCheckout order={output} clientId={clientId} />
            </div>
          );
        }
        return <ToolNote key={key} text={toolLabel(tool)} />;
      })}
    </div>
  );
}

function orderedParts(parts: UIMessage["parts"]) {
  const indexed = parts.map((part, i) => ({ part, i }));
  const isCard = (p: UIMessage["parts"][number]) => p.type === "tool-checkout" && asTool(p)?.state === "output-available";
  return [...indexed.filter(({ part }) => !isCard(part)), ...indexed.filter(({ part }) => isCard(part))];
}

function toolLabel(tool: ToolPart): string {
  const done = tool.state === "output-available";
  const input = (tool.input ?? {}) as { query?: string; sku?: string };
  if (tool.type === "tool-search_products") return done ? `Searched “${input.query ?? ""}”` : "Searching…";
  if (tool.type === "tool-add_to_cart") {
    const output = (tool.output ?? {}) as { added?: { name: string; quantity: number }; error?: string };
    if (!done) return "Adding to cart…";
    return output.added ? `Added ${output.added.quantity} x ${output.added.name}` : (output.error ?? "Not added");
  }
  if (tool.type === "tool-checkout") return tool.state === "output-error" ? "Checkout failed" : "Creating PayPal order…";
  return tool.type.slice(5);
}

function ToolNote({ text }: { text: string }) {
  return <p className="text-xs text-[var(--muted)]">{text}</p>;
}
