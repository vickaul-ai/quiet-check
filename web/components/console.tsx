"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Copilot } from "@/components/copilot";
import {
  FIELD_LABELS,
  type ConsoleEvent,
  type OrderDetail as Detail,
  type OrderRow,
  type Receipt,
} from "@/lib/types";

const EVENT_POLL_MS = 2000;
const ORDER_POLL_MS = 10000;
const FEED_SIZE = 100;

async function getJson<T>(url: string): Promise<T | null> {
  const res = await fetch(url, { cache: "no-store" }).catch(() => null);
  return res?.ok ? ((await res.json()) as T) : null;
}

export function Console() {
  const [orders, setOrders] = useState<OrderRow[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const [offline, setOffline] = useState(false);
  const selectedRef = useRef<string | null>(null);
  const cursor = useRef<string | undefined>(undefined);

  const refreshOrders = useCallback(async () => {
    const data = await getJson<{ orders: OrderRow[] }>("/api/console/orders");
    setOffline(!data);
    if (!data) return;
    setOrders(data.orders);
    // Open the newest held order first: that's the one waiting for a reviewer.
    if (!selectedRef.current && data.orders.length) {
      const first = data.orders.find((o) => o.status === "held") ?? data.orders[0];
      selectedRef.current = first.order_id;
      setSelectedId(first.order_id);
    }
  }, []);

  const refreshDetail = useCallback(async () => {
    const id = selectedRef.current;
    if (!id) return;
    const data = await getJson<Detail>(`/api/console/orders/${encodeURIComponent(id)}`);
    if (data && data.order_id === selectedRef.current) setDetail(data);
  }, []);

  const refresh = useCallback(async () => {
    await refreshOrders();
    await refreshDetail();
  }, [refreshOrders, refreshDetail]);

  // The live feed: page through /merchant/events to the newest, then poll for more. Any new event
  // (a webhook, a decision, a reviewer action) refreshes the table and the open order.
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let fresh: ConsoleEvent[] = [];
      for (;;) {
        const query = cursor.current ? `?after=${cursor.current}` : "";
        const data = await getJson<{ events: ConsoleEvent[] }>(`/api/console/events${query}`);
        if (!data || stopped) break;
        fresh = fresh.concat(data.events);
        if (data.events.length) cursor.current = data.events[data.events.length - 1].event_id;
        if (data.events.length < 50) break;
      }
      if (fresh.length && !stopped) {
        setEvents((old) => [...fresh.reverse(), ...old].slice(0, FEED_SIZE));
        await refresh();
      }
      if (!stopped) timer = setTimeout(poll, EVENT_POLL_MS);
    }
    poll();
    const slow = setInterval(refreshOrders, ORDER_POLL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
      clearInterval(slow);
    };
  }, [refresh, refreshOrders]);

  function select(id: string) {
    selectedRef.current = id;
    setSelectedId(id);
    setDetail(null);
    refreshDetail();
  }

  const selected = orders?.find((o) => o.order_id === selectedId) ?? null;

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 px-4 py-4 sm:py-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Merchant console</h1>
          <p className="text-sm text-[var(--muted)]">
            Orders, private consortium checks and held payments. Sandbox only.
          </p>
        </div>
        <Link href="/" className="text-sm underline underline-offset-4">
          Back to the shop
        </Link>
      </header>

      {offline && (
        <p className="rounded-md bg-[var(--panel)] px-3 py-2 text-sm text-[var(--bad)]">
          The backend isn&apos;t reachable. Start it with <code>make dev</code>.
        </p>
      )}

      <OrdersTable orders={orders} selectedId={selectedId} onSelect={select} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
        <OrderDetail detail={detail} loading={!!selectedId && !detail} />
        <div className="flex min-w-0 flex-col gap-4">
          {selected && <Copilot key={selected.order_id} order={selected} onResolved={refresh} />}
          <EventFeed events={events} selectedId={selectedId} />
        </div>
      </div>
    </div>
  );
}

// ---- Shared bits

const STATUS_TONE: Record<string, string> = {
  captured: "text-[var(--good)]",
  held: "text-[var(--warn)]",
  blocked: "text-[var(--bad)]",
  voided: "text-[var(--bad)]",
  failed: "text-[var(--bad)]",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`inline-block rounded-full border border-current px-2 py-0.5 text-xs font-medium ${STATUS_TONE[status] ?? "text-[var(--muted)]"}`}>
      {status}
    </span>
  );
}

// Null until mounted: Next.js prerenders the page, so the clock is read only in the browser.
function useNow(intervalMs = 30000): number | null {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const t = setInterval(tick, intervalMs);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [intervalMs]);
  return now;
}

// Time left in PayPal's 3-day honour period; only meaningful while the authorisation is held.
function honourLeft(order: Pick<OrderRow, "status" | "honor_period_ends_at">, now: number | null): string {
  if (order.status !== "held" || !order.honor_period_ends_at || now === null) return "-";
  const ms = Date.parse(order.honor_period_ends_at) - now;
  if (ms <= 0) return "Ended: reauthorise";
  const hours = Math.floor(ms / 3_600_000);
  const days = Math.floor(hours / 24);
  return days ? `${days}d ${hours % 24}h left` : `${hours}h ${Math.floor((ms % 3_600_000) / 60_000)}m left`;
}

function shortTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function Mono({ children }: { children: React.ReactNode }) {
  return <code className="font-mono text-xs break-all">{children}</code>;
}

// ---- Orders table

function OrdersTable({
  orders,
  selectedId,
  onSelect,
}: {
  orders: OrderRow[] | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const now = useNow();
  if (!orders) return <p className="text-sm text-[var(--muted)]">Loading orders…</p>;
  if (!orders.length) return <p className="text-sm text-[var(--muted)]">No orders yet. Buy something in the shop.</p>;
  return (
    <section aria-label="Orders" className="overflow-x-auto rounded-lg border border-[var(--line)]">
      <table className="w-full min-w-[900px] text-left text-sm">
        <thead className="bg-[var(--panel)] text-xs text-[var(--muted)]">
          <tr>
            {["Order", "Created", "Amount", "Status", "Decision", "Hits", "PayPal order", "Authorisation", "Capture", "Honour period"].map((h) => (
              <th key={h} scope="col" className="px-3 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => {
            const active = o.order_id === selectedId;
            return (
              <tr
                key={o.order_id}
                onClick={() => onSelect(o.order_id)}
                aria-selected={active}
                className={`cursor-pointer border-t border-[var(--line)] ${active ? "bg-[var(--panel)]" : "hover:bg-[var(--panel)]"}`}
              >
                <td className="px-3 py-2">
                  <button onClick={() => onSelect(o.order_id)} className="font-mono text-xs underline-offset-2 hover:underline">
                    {o.order_id}
                  </button>
                </td>
                <td className="px-3 py-2 whitespace-nowrap">{shortTime(o.created_at)}</td>
                <td className="px-3 py-2">${o.amount.value}</td>
                <td className="px-3 py-2">
                  <StatusBadge status={o.status} />
                </td>
                <td className="px-3 py-2">{o.decision ?? "-"}</td>
                <td className="px-3 py-2">{o.hit_count ?? "-"}</td>
                <td className="px-3 py-2"><Mono>{o.paypal_order_id}</Mono></td>
                <td className="px-3 py-2"><Mono>{o.authorization_id ?? "-"}</Mono></td>
                <td className="px-3 py-2"><Mono>{o.capture_id ?? "-"}</Mono></td>
                <td className={`px-3 py-2 whitespace-nowrap ${o.status === "held" ? "text-[var(--warn)]" : ""}`}>{honourLeft(o, now)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// ---- Order detail

function OrderDetail({ detail, loading }: { detail: Detail | null; loading: boolean }) {
  const now = useNow();
  if (!detail) {
    return <section className="text-sm text-[var(--muted)]">{loading ? "Loading the order…" : "Select an order."}</section>;
  }
  const d = detail.decision;
  return (
    <section aria-label="Order detail" className="flex min-w-0 flex-col gap-4">
      <div className="rounded-lg border border-[var(--line)] p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-sm font-semibold">{detail.order_id}</h2>
          <StatusBadge status={detail.status} />
          <span className="text-sm">${detail.amount.value}</span>
          {detail.status === "held" && (
            <span className="text-sm text-[var(--warn)]">Honour period: {honourLeft(detail, now)}</span>
          )}
        </div>
        <p className="mt-1 text-sm text-[var(--muted)]">
          {detail.items.map((i) => `${i.quantity} x ${i.name}`).join(", ")} · device <Mono>{detail.device_id}</Mono>
        </p>
        {d ? (
          <div className="mt-3">
            <p className="text-xs font-medium text-[var(--muted)]">
              Policy decision: <strong className="text-[var(--fg)]">{d.decision}</strong> ({d.paypal_action}) · rationale{" "}
              {d.rationale_source === "template" ? "from the template (model unavailable)" : "written by the AI"}
            </p>
            <p className="mt-1 text-sm leading-relaxed">{d.rationale}</p>
            {d.resolution && (
              <p className="mt-2 text-sm">
                Reviewer {d.resolution === "capture" ? "captured" : "voided"} it
                {d.resolved_at ? ` on ${shortTime(d.resolved_at)}` : ""}
                {d.resolution_note ? `: “${d.resolution_note}”` : "."}
              </p>
            )}
          </div>
        ) : (
          <p className="mt-3 text-sm text-[var(--muted)]">No decision yet.</p>
        )}
      </div>

      {detail.check && <HitGrid receipts={detail.receipts} />}
      {detail.receipts.length > 0 && <Receipts receipts={detail.receipts} />}
      <WhoSawWhat rows={detail.who_saw_what} />
    </section>
  );
}

const MEMBERS = ["A", "B", "C"];
const FIELDS = ["email", "shipping_address", "device_id"];

function HitGrid({ receipts }: { receipts: Receipt[] }) {
  const hits = receipts.filter((r) => r.is_match).length;
  return (
    <div className="rounded-lg border border-[var(--line)] p-4">
      <h3 className="text-sm font-semibold">
        AP3 private check: {hits} {hits === 1 ? "hit" : "hits"} in 9 sessions
      </h3>
      <p className="mb-3 text-xs text-[var(--muted)]">One PSI session per member per field. We learn match or no match; no list leaves its owner.</p>
      <table className="w-full max-w-md text-center text-sm">
        <thead className="text-xs text-[var(--muted)]">
          <tr>
            <th scope="col" className="py-1 text-left font-medium">Member</th>
            {FIELDS.map((f) => (
              <th key={f} scope="col" className="py-1 font-medium">{FIELD_LABELS[f]}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {MEMBERS.map((m) => (
            <tr key={m} className="border-t border-[var(--line)]">
              <th scope="row" className="py-1.5 text-left font-medium">{m}</th>
              {FIELDS.map((f) => {
                const r = receipts.find((x) => x.member === m && x.field === f);
                return (
                  <td key={f} className={`py-1.5 ${r?.is_match ? "font-semibold text-[var(--bad)]" : "text-[var(--muted)]"}`}>
                    {r ? (r.is_match ? "match" : "clear") : "-"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Receipts({ receipts }: { receipts: Receipt[] }) {
  const verified = receipts.filter((r) => r.verified).length;
  return (
    <div className="rounded-lg border border-[var(--line)] p-4">
      <h3 className="text-sm font-semibold">
        Receipts: {verified} of {receipts.length} verified
      </h3>
      <p className="mb-2 text-xs text-[var(--muted)]">
        Each holds our 2 signed intents and signed result, plus the member&apos;s signed commitment. Members&apos; PSI
        replies aren&apos;t signed (AP3 1.2.2), and the proof fields are placeholders.
      </p>
      <ul className="flex flex-col divide-y divide-[var(--line)]">
        {receipts.map((r) => (
          <li key={r.receipt_id} className="py-1.5">
            <details>
              <summary className="cursor-pointer text-sm">
                Member {r.member} · {FIELD_LABELS[r.field] ?? r.field} ·{" "}
                <span className={r.is_match ? "text-[var(--bad)]" : ""}>{r.is_match ? "match" : "clear"}</span> ·{" "}
                <span className={r.verified ? "text-[var(--good)]" : "text-[var(--bad)]"}>
                  {r.verified ? "verified" : "NOT verified"}
                </span>
              </summary>
              <div className="mt-2 flex flex-col gap-2 text-xs">
                <p>
                  Receipt <Mono>{r.receipt_id}</Mono> · session <Mono>{r.session_id}</Mono>
                </p>
                <p>
                  Member public key <Mono>{r.member_public_key}</Mono>
                </p>
                <JsonBlock label="Signed intents (init, msg1)" value={r.intent_directives} />
                <JsonBlock label="Signed result" value={r.result_directive} />
                <JsonBlock label="Member's signed commitment" value={r.member_commitment} />
              </div>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <p className="mb-1 font-medium">{label}</p>
      <pre className="max-h-64 overflow-auto rounded bg-[var(--panel)] p-2 font-mono text-[11px] leading-snug">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function WhoSawWhat({ rows }: { rows: Detail["who_saw_what"] }) {
  return (
    <div className="rounded-lg border border-[var(--line)] p-4">
      <h3 className="mb-1 text-sm font-semibold">Who saw what</h3>
      <p className="mb-3 text-xs text-[var(--muted)]">
        PSI tells us which member matched which field; showing only a count is future work.
      </p>
      <ul className="grid gap-3 sm:grid-cols-2">
        {rows.map((r) => (
          <li key={r.party} className="rounded-md bg-[var(--panel)] p-3 text-sm">
            <p className="mb-1 font-semibold">{r.party}</p>
            <dl className="grid grid-cols-[5.5rem_1fr] gap-x-2 gap-y-1 text-xs">
              <dt className="text-[var(--muted)]">Sent</dt>
              <dd>{r.sent}</dd>
              <dt className="text-[var(--muted)]">Learned</dt>
              <dd>{r.learned}</dd>
              <dt className="text-[var(--muted)]">Never saw</dt>
              <dd>{r.never_saw}</dd>
            </dl>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---- Live event feed

function eventDetail(e: ConsoleEvent): string {
  const p = e.payload ?? {};
  const keys = ["summary", "decision", "hit_count", "actor", "note", "capture_id", "capture_status", "authorization_status", "action", "issue", "error"];
  return keys
    .filter((k) => p[k] !== undefined && p[k] !== null && p[k] !== "")
    .map((k) => `${k}: ${String(p[k])}`)
    .join(" · ");
}

function EventFeed({ events, selectedId }: { events: ConsoleEvent[]; selectedId: string | null }) {
  return (
    <section aria-label="Live event feed" className="rounded-lg border border-[var(--line)]">
      <div className="flex items-center justify-between border-b border-[var(--line)] px-3 py-2">
        <h2 className="text-sm font-semibold">Live events</h2>
        <span className="text-xs text-[var(--muted)]">PayPal webhooks and our own actions</span>
      </div>
      <ol className="max-h-[420px] overflow-y-auto text-xs" aria-live="polite">
        {events.length === 0 && <li className="px-3 py-2 text-[var(--muted)]">No events yet.</li>}
        {events.map((e) => {
          const webhook = e.source === "paypal_webhook";
          const mine = e.order_id && e.order_id === selectedId;
          return (
            <li key={e.event_id} className={`border-b border-[var(--line)] px-3 py-1.5 ${mine ? "bg-[var(--panel)]" : ""}`}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[var(--muted)]">{new Date(e.created_at).toLocaleTimeString()}</span>
                <span className="font-mono font-medium">{e.event_type}</span>
                <span className={webhook ? (e.verified ? "text-[var(--good)]" : "text-[var(--bad)]") : "text-[var(--muted)]"}>
                  {webhook ? (e.verified ? "PayPal webhook, verified" : "PayPal webhook, UNVERIFIED") : "system"}
                </span>
              </div>
              {(e.order_id || eventDetail(e)) && (
                <p className="text-[var(--muted)]">
                  {e.order_id && <Mono>{e.order_id}</Mono>} {eventDetail(e)}
                </p>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
