# 0012. Dispute evidence: the AI drafts, a reviewer submits, and the note claims only what the check shows
_Date: 2026-10-14 · Status: accepted_

## Context
D8 (a stretch deliverable, approved by Vic on 2026-10-14) answers a PayPal dispute with what Quiet Check already
records: the authorise-then-capture timeline, the policy decision and the AP3 receipts. PayPal's Disputes API takes
evidence only while a dispute is `WAITING_FOR_SELLER_RESPONSE` and its HATEOAS links include `provide-evidence`. A
submitted note can't be edited or withdrawn. The webhook's dispute resource can be partial, and the dispute carries the
buyer's name and email.

## Decision
- **Draft only; a person submits.** On a verified `CUSTOMER.DISPUTE.*` webhook the backend reads the dispute, stores it
  and has `LLM_MODEL_SMART` draft a note once. Only the console's "Submit evidence to PayPal" button, after an in-page
  confirmation, calls `POST /merchant/disputes/{id}/evidence` (`actor: "reviewer"`). The review copilot can read a
  dispute's summary but has no tool to submit. Same principle as ADR 0002: the AI writes, it doesn't act.
- **The model sees a fact sheet with no identifiers.** It gets the dispute, the order, the timeline, the PayPal IDs,
  the decision and rationale, which member matched which field, and the receipt and AP3 session IDs. It never gets the
  payer email, shipping address, device ID, buyer name, identifier hashes, list contents or reviewer notes (free
  text). As with the rationale, a template built from the same facts stands in on any model failure, so a dispute
  always has a draft.
- **What the note may claim.** The check shows whether the buyer's email, address and device were on three other
  merchants' abuse lists at purchase time, and the timeline shows the buyer approved in PayPal's checkout before we
  authorised and that we captured only after the check. The note must say that this isn't proof of the buyer's
  identity and doesn't show who was using the account, report any hit rather than hide it, and leave out what isn't
  in the record (shipping and tracking above all).
- **Guard on PayPal's live state.** The submit endpoint re-reads the dispute and refuses with `409
  dispute_not_awaiting_evidence` unless PayPal is asking the seller for evidence, so a stale console can't send
  evidence PayPal would reject. Submission is idempotent on the dispute ID and sends `PayPal-Request-Id:
  <dispute_id>:evidence`.
- **Notes-only evidence of the type PayPal asked for** (else `OTHER`), sent as `multipart/form-data` with an `input`
  JSON part, as in PayPal's Disputes API guide; PayPal parsed this format live on 2026-10-14. No documents yet.
- **Tracking-type requests are refused in the app.** Live on 2026-10-14 an item-not-received inquiry asked for
  `PROOF_OF_FULFILLMENT`, and PayPal refused a note (`EVIDENCE_TYPE_IS_NOT_ALLOWED`). A demo shop ships nothing, so it
  has no tracking to give: the endpoint answers `409 evidence_needs_details` and the console says to respond in PayPal's
  Resolution Center. The draft's value there is the record it assembles, not the submission.
- **Matching:** the disputed transaction's `seller_transaction_id` is our capture ID; else `custom` is our order ID
  (we send it as `custom_id`). Unmatched disputes are stored without an order and get no draft.
- **Sandbox state changes stay out of the app.** `require-evidence` and `escalate` exist only in
  `scripts/dispute_sandbox.py`, for the live acceptance run.

## Alternatives considered
- The copilot submits with AI SDK tool approval, as for capture and void: one Approve on a 2,000-character note is
  easy to wave through, and evidence can't be withdrawn. A plain editable textarea keeps the reviewer reading it.
- Submit automatically when PayPal asks: fastest, but puts model-written claims in front of PayPal unreviewed.
- Send payer identifiers to the model for a "stronger" note: against the project's rule that models never see
  identifiers, and the note would only repeat what PayPal already holds.

## Consequences
- A dispute that arrives before PayPal asks for evidence shows the draft with a hint, and the submit button answers
  "PayPal isn't asking for evidence on this dispute yet" until it does.
- One draft per dispute; reviewers edit rather than regenerate. A redraft endpoint can come later.
- Evidence is text only. Documents (for example a receipt PDF) would be extra file parts in the same request.
- The multipart shape is checked against PayPal's guide and the CI fake, not yet against a live sandbox submission.
