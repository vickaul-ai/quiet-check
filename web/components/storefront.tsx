"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import Image from "next/image";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { PayPalCheckout } from "@/components/paypal-checkout";
import { buyerText, type CartLine, type CheckoutResult, type Persona, type Product } from "@/lib/types";

const MAX_QUANTITY = 10;

type Props = { catalog: Product[]; personas: Persona[]; clientId: string };

export function Storefront({ catalog, personas, clientId }: Props) {
  const [personaKey, setPersonaKey] = useState(personas[0]?.key ?? "");
  const persona = personas.find((p) => p.key === personaKey);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 px-4 py-4 sm:py-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Quiet Check demo shop</h1>
          <p className="text-sm text-[var(--muted)]">Ask the shopping agent, then approve the payment in PayPal.</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <PersonaSwitcher personas={personas} value={personaKey} onChange={setPersonaKey} />
          <Link href="/console" className="text-sm underline underline-offset-4">
            Merchant console
          </Link>
        </div>
      </header>
      {persona && (
        <p className="rounded-md bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted)]">
          Shopping as <strong>{persona.name}</strong> (device <code>{persona.device_id}</code>, expected:{" "}
          {buyerText(persona.status)}). Log in to PayPal as {persona.name}&apos;s sandbox buyer, and log out at
          www.sandbox.paypal.com before switching persona: PayPal reuses the last login.
        </p>
      )}
      {/* Keyed on the persona: switching starts a fresh chat and cart. */}
      <Shop key={personaKey} catalog={catalog} personaKey={personaKey} clientId={clientId} />
    </div>
  );
}

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
        </button>
      ))}
    </div>
  );
}

function Shop({ catalog, personaKey, clientId }: { catalog: Product[]; personaKey: string; clientId: string }) {
  const [cart, setCart] = useState<CartLine[]>([]);
  const [input, setInput] = useState("");
  const seen = useRef(new Set<string>());
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
        if (tool.type === "tool-checkout" && output.order_id) setCart([]);
      }
    }
  }, [messages]);

  function send(text: string) {
    if (!text.trim() || busy) return;
    sendMessage({ text }, { body: { persona: personaKey, cart } });
  }

  function add(sku: string) {
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
                    className="rounded-md border border-[var(--line)] px-2 py-1 text-xs hover:bg-[var(--panel)]"
                  >
                    Add
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
        <Cart cart={cart} catalog={catalog} onCheckout={() => send("Check out my cart.")} busy={busy} />
      </section>

      <section
        aria-label="Shopping agent"
        className="flex min-h-[420px] flex-col rounded-lg border border-[var(--line)] lg:sticky lg:top-4 lg:h-[calc(100vh-2rem)]"
      >
        <div className="border-b border-[var(--line)] px-3 py-2 text-sm font-semibold">Shopping agent</div>
        <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-3" aria-live="polite">
          {messages.length === 0 && (
            <div className="text-sm text-[var(--muted)]">
              <p className="mb-2">Try:</p>
              <button
                onClick={() => send("Buy the blue backpack in medium")}
                className="rounded-full border border-[var(--line)] px-3 py-1 hover:bg-[var(--panel)]"
              >
                Buy the blue backpack in medium
              </button>
            </div>
          )}
          {messages.map((m) => (
            <Message key={m.id} message={m} clientId={clientId} />
          ))}
          {status === "submitted" && <p className="text-sm text-[var(--muted)]">Thinking…</p>}
          {error && <p className="text-sm text-[var(--bad)]">The agent hit an error. Please try again.</p>}
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
            className="rounded-md bg-[var(--fg)] px-3 py-2 text-sm text-[var(--bg)] disabled:opacity-50"
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
  onCheckout,
  busy,
}: {
  cart: CartLine[];
  catalog: Product[];
  onCheckout: () => void;
  busy: boolean;
}) {
  if (!cart.length) return <p className="text-sm text-[var(--muted)]">Your cart is empty.</p>;
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
          className="rounded-md bg-[var(--fg)] px-3 py-1.5 text-[var(--bg)] disabled:opacity-50"
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
      {message.parts.map((part, i) => {
        const key = `${message.id}-${i}`;
        if (part.type === "text") {
          if (!part.text.trim()) return null;
          return (
            <p
              key={key}
              className={`max-w-[90%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                mine ? "bg-[var(--fg)] text-[var(--bg)]" : "bg-[var(--panel)]"
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
