"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, lastAssistantMessageIsCompleteWithApprovalResponses, type UIMessage } from "ai";
import { useEffect, useRef, useState } from "react";

import { chatErrorMessage } from "@/lib/chat-errors";
import type { OrderRow } from "@/lib/types";

type Props = { order: OrderRow; onResolved: () => void };

// The review copilot for the order open in the console. Keyed on the order, so each order gets a fresh chat.
export function Copilot({ order, onResolved }: Props) {
  const [input, setInput] = useState("");
  const seen = useRef(new Set<string>());
  const { messages, sendMessage, status, error, addToolApprovalResponse } = useChat({
    transport: new DefaultChatTransport({ api: "/api/console/chat", body: { order_id: order.order_id } }),
    // After Approve or Deny, send the response back so the server runs (or skips) the tool.
    sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
  });
  const busy = status === "submitted" || status === "streaming";

  // A finished capture or void refreshes the console straight away (the webhook confirms it later).
  useEffect(() => {
    for (const m of messages) {
      for (const part of m.parts) {
        const tool = asTool(part);
        if (!tool || !MONEY_TOOLS.has(tool.type) || tool.state !== "output-available") continue;
        if (seen.current.has(tool.toolCallId)) continue;
        seen.current.add(tool.toolCallId);
        onResolved();
      }
    }
  }, [messages, onResolved]);

  function send(text: string) {
    if (!text.trim() || busy) return;
    sendMessage({ text });
  }

  const suggestions =
    order.status === "held"
      ? ["Why is this on hold?", "Capture it, the buyer confirmed by phone", "Void it, the buyer didn't answer"]
      : ["Explain this decision"];

  return (
    <section aria-label="Review copilot" className="flex min-h-[360px] flex-col rounded-lg border border-[var(--line)]">
      <div className="border-b border-[var(--line)] px-3 py-2">
        <h2 className="text-sm font-semibold">Review copilot</h2>
        <p className="text-xs text-[var(--muted)]">
          On <code className="font-mono">{order.order_id}</code>. It captures or voids only on your instruction, after you
          press Approve.
        </p>
      </div>
      <div className="flex max-h-[520px] flex-1 flex-col gap-3 overflow-y-auto p-3" aria-live="polite">
        {messages.length === 0 && (
          <div className="flex flex-wrap gap-2 text-sm">
            {suggestions.map((s) => (
              <button
                key={s}
                onClick={() => send(s)}
                className="rounded-full border border-[var(--line)] px-3 py-1 text-left hover:bg-[var(--panel)]"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} order={order} onRespond={addToolApprovalResponse} />
        ))}
        {status === "submitted" && <p className="text-sm text-[var(--muted)]">Thinking…</p>}
        {error && <p className="text-sm text-[var(--bad)]">{chatErrorMessage(error, "The copilot")}</p>}
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
          placeholder="Ask about this order…"
          aria-label="Message the review copilot"
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
  );
}

const MONEY_TOOLS = new Set(["tool-capture_held_payment", "tool-void_held_payment"]);

type ToolPart = {
  type: string;
  toolCallId: string;
  state: string;
  input?: { order_id?: string; note?: string };
  output?: { status?: string; capture_id?: string | null; error?: string };
  approval?: { id: string; approved?: boolean; reason?: string };
};

function asTool(part: UIMessage["parts"][number]): ToolPart | null {
  return part.type.startsWith("tool-") ? (part as unknown as ToolPart) : null;
}

type Respond = (r: { id: string; approved: boolean; reason?: string }) => void | PromiseLike<void>;

function Message({ message, order, onRespond }: { message: UIMessage; order: OrderRow; onRespond: Respond }) {
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
        if (MONEY_TOOLS.has(tool.type)) return <ActionCard key={key} tool={tool} order={order} onRespond={onRespond} />;
        if (tool.type === "tool-get_order") {
          return <Note key={key} text={tool.state === "output-available" ? "Read the order" : "Reading the order…"} />;
        }
        return null;
      })}
    </div>
  );
}

// The one confirmation: order, amount, action and note, with Approve and Deny. Nothing runs before Approve.
function ActionCard({ tool, order, onRespond }: { tool: ToolPart; order: OrderRow; onRespond: Respond }) {
  const capture = tool.type === "tool-capture_held_payment";
  const verb = capture ? "Capture" : "Void";
  const orderId = tool.input?.order_id ?? "";
  const note = tool.input?.note?.trim();

  if (tool.state === "input-streaming" || tool.state === "input-available") return <Note text={`Preparing ${verb.toLowerCase()}…`} />;

  if (tool.state === "approval-requested" && tool.approval) {
    const approvalId = tool.approval.id;
    return (
      <div className="w-full rounded-lg border-2 border-[var(--warn)] p-3 text-sm" role="group" aria-label={`Confirm ${verb.toLowerCase()}`}>
        <p className="mb-2 font-semibold">Confirm: {verb.toLowerCase()} the held payment?</p>
        <dl className="mb-3 grid grid-cols-[4.5rem_1fr] gap-x-2 gap-y-1 text-xs">
          <dt className="text-[var(--muted)]">Order</dt>
          <dd className="font-mono break-all">{orderId}</dd>
          <dt className="text-[var(--muted)]">Amount</dt>
          <dd>{orderId === order.order_id ? `$${order.amount.value} ${order.amount.currency_code}` : "Unknown order"}</dd>
          <dt className="text-[var(--muted)]">Action</dt>
          <dd>{capture ? "Capture: charge the buyer" : "Void: release the funds and cancel the order"}</dd>
          <dt className="text-[var(--muted)]">Note</dt>
          <dd>{note || "(none)"}</dd>
        </dl>
        <div className="flex gap-2">
          <button
            onClick={() => onRespond({ id: approvalId, approved: true })}
            className="rounded-md bg-[var(--fg)] px-3 py-1.5 text-[var(--bg)]"
          >
            Approve {verb.toLowerCase()}
          </button>
          <button
            onClick={() => onRespond({ id: approvalId, approved: false, reason: "The reviewer denied it." })}
            className="rounded-md border border-[var(--line)] px-3 py-1.5 hover:bg-[var(--panel)]"
          >
            Deny
          </button>
        </div>
      </div>
    );
  }

  if (tool.state === "approval-responded") {
    return <Note text={tool.approval?.approved ? `Approved. ${verb === "Capture" ? "Capturing" : "Voiding"} with PayPal…` : "Denied."} />;
  }
  if (tool.state === "output-denied") return <Note text={`${verb} denied: nothing was changed.`} />;
  if (tool.state === "output-error") return <Note text={`${verb} failed.`} tone="bad" />;
  if (tool.state === "output-available") {
    const out = tool.output ?? {};
    if (out.error) return <Note text={out.error} tone="bad" />;
    return (
      <Note
        text={`${verb} approved and done: ${orderId} is ${out.status}${out.capture_id ? ` (capture ${out.capture_id})` : ""}.`}
        tone="good"
      />
    );
  }
  return null;
}

function Note({ text, tone }: { text: string; tone?: "good" | "bad" }) {
  const color = tone === "good" ? "text-[var(--good)]" : tone === "bad" ? "text-[var(--bad)]" : "text-[var(--muted)]";
  return <p className={`text-xs ${color}`}>{text}</p>;
}
