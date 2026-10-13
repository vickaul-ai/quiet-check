# Quiet Check: project brief

Source of truth (copied from the D0 brief on 2026-10-12).
Interfaces live in `contracts.md`.

## Name and pitch

**Quiet Check** (working name). PayPal AI Hackathon entry by Vic Kaul (individual, solo). Deadline Nov 12, 2026, 12:00pm PST.

> A merchant agent privately checks an AI-agent purchase against other merchants' fraud lists, then uses PayPal to
> capture, hold or decline the payment. No merchant ever shares its list.

Prize targets: Best Use of Agentic Commerce, Best Use of PayPal + AI, Render sponsor prize. AG Grid is out of scope.

## Demo story (under 3 minutes)

1. **Shop.** A buyer asks the shopping agent to buy something ("buy the blue backpack in medium"). The agent builds the
   cart and hands off to PayPal checkout (order intent `AUTHORIZE`). The buyer approves in the PayPal pop-up.
2. **Check.** Before authorising, the merchant's risk agent runs an AP3 private set intersection (PSI) check of the payer's
   email, shipping address and device ID against the abuse lists of three consortium merchants (Members A, B, C).
   That is 3 identifiers x 3 members = 9 PSI sessions. No list leaves its owner.
3. **Decide.** A fixed policy decides and the AI explains the decision in 2-3 sentences. Clean: authorise and capture.
   One hit: authorise only (funds held) and send to review. Two or more hits: don't authorise.
4. **Review.** In the merchant console, a reviewer sees the signed AP3 receipts and a "who saw what" panel, then asks the
   copilot about the hold. On the reviewer's instruction and one confirmation, the copilot captures or voids the held payment.

## Personas

Each persona is a sandbox buyer account plus a device ID, selected in the storefront's persona switcher. D7 seeds them.

| Persona | Seeded on | Hits | Outcome | Buyer sees |
| --- | --- | --- | --- | --- |
| Ana (clean) | Nothing | 0 | Authorised and captured | "Paid" |
| Ben (hold) | Member B (email only) | 1 | Authorised only, queued for review | "Payment on hold for review" |
| Cy (block) | Members A and C (email + device) | 2+ | Not authorised; order left to expire | "We couldn't complete this order" |

## Decision policy

The policy is deterministic. The AI explains the decision but never makes it, and the LLM cannot change the outcome.

A **hit** is one (member, field) pair whose PSI result is `is_match: true`. `hit_count` is the number of hits (0-9).

| Condition | Decision | PayPal action |
| --- | --- | --- |
| `hit_count == 0` | `clean` | Authorise, then capture |
| `hit_count == 1` | `hold` | Authorise only; queue for human review (capture or void) |
| `hit_count >= 2`, or email and device both hit (at any members) | `block` | Don't authorise; tell the buyer; let the order expire |

Because hits are counted per (member, field) pair, "email and device both hit" always means at least 2 hits. The policy keeps
the clause anyway so it states the intent explicitly. Policy code checks it so that a later change to hit counting can't
quietly drop it.

**Guardrails:** money-moving calls (authorise, capture, void) run only from the policy branch or on a human reviewer's
instruction. The rationale prompt sees only the hit summary, never raw lists or other buyers' data.

**Holds:** a PayPal authorisation holds funds for 29 days. PayPal recommends capturing within the 3-day honour period, and
the console shows the time left in it. After 3 days, reauthorise. (Source: `reference/paypal/orders-and-payments.md`.)

## Stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.12 + FastAPI (risk agent, AP3 risk check, payments service, webhooks) |
| Consortium | Open-source AP3 SDK (`ap3-functions`, `ap3[a2a]`). Three receiver agents (Members A, B, C), each an A2A server. Not AP3 Studio |
| Payments | PayPal Orders v2 + Payments v2 (authorise, capture, void) + webhooks, sandbox only; PayPal JS SDK v6 for buyer approval |
| Web | Next.js storefront with shopping-agent chat, plus the merchant console |
| AI (web) | Vercel AI SDK with `@ai-sdk/anthropic`, created in one file (`web/lib/model.ts`) |
| AI (Python) | LangChain `init_chat_model(f"{provider}:{model}")` |
| Database | SQLite locally, Postgres on Render |
| Hosting | Render |
| Licence | Apache-2.0 |

## Models

Anthropic models, behind a swap layer: the `LLM_PROVIDER`, `LLM_MODEL_FAST` and `LLM_MODEL_SMART` env vars, prompts in
`/prompts/*.md`, and no provider-specific features in code that has to swap. D10 runs a smoke test with the models swapped.

| Where | Default model | Fallback |
| --- | --- | --- |
| Shopping agent (D5): short tool loops (search, cart, checkout) | Haiku 5.5 (`claude-haiku-5-5`) via `LLM_MODEL_FAST` | Sonnet 5.5 |
| Risk rationale (D4): 2-3 sentences from the hit summary | Haiku 5.5 via `LLM_MODEL_FAST` | - |
| Review copilot (D6): explains holds, captures or voids on instruction | Haiku 5.5 via `LLM_MODEL_FAST` | Sonnet 5.5 if answers are weak in testing |
| Dispute evidence draft (D8, stretch) | Sonnet 5.5 (`claude-sonnet-5-5`) via `LLM_MODEL_SMART` | Opus 5.5 |

## Out of scope

- Real Agent Ready, ACP or UCP integration. These surfaces are gated, so the storefront agent stands in for them; the merchant side is the same.
- Real customer data. All member lists and personas are synthetic.
- Cryptographic proofs of correct execution. AP3 proof fields are placeholders today.
- The AG Grid prize (skipped unless we're ahead at Gate B).
- Currencies other than USD.

## Honest limits (state in the README and the video)

- AP3 is a research preview. Its proof fields are placeholders, and only PSI is in the public SDK.
- PSI tells the initiator which member matched which field, so the merchant learns per-member hits. The who-saw-what panel
  says this; "count only" is future work.
- PSI alone doesn't prove a member used the dataset it committed to.
