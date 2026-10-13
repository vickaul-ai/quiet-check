# Quiet Check: Product

_Last reviewed: 2026-10-12_

The audience, problem, demo story, personas, decision policy and out-of-scope list are in [brief.md](brief.md). This
file tracks what exists today.

## Capabilities (built)

None yet: the repo is a scaffold (D1). The backend serves `/health`; the web app shows a placeholder page.

## Roadmap

| Deliverable | Adds |
| --- | --- |
| D2 | AP3 consortium: Members A, B, C and `POST /risk/check` with signed receipts |
| D3 | PayPal payments service: create, authorise, capture, void, webhooks |
| D4 | Merchant risk agent: approved order -> check -> policy -> PayPal action, with AI rationale |
| D5 | Shopping agent and storefront with PayPal approval |
| D6 | Merchant console: review queue, receipts, who-saw-what, review copilot |
| D7 | Demo data and the three personas |
| D8 | Dispute evidence drafts (stretch) |

## Known gaps

- AP3 is a research preview: proof fields are placeholders, and PSI shows the merchant which member matched which field.
- Sandbox only; USD only.

## Non-goals

See "Out of scope" in [brief.md](brief.md).
