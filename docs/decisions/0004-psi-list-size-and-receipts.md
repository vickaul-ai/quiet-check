# 0004. PSI list size, initiator hosting and receipt contents
_Date: 2026-10-13 · Status: accepted_

## Context
The D2 spike (AP3 1.2.2) found three things:
- **Cost:** the member side of PSI costs about 2.7 ms per list entry per session. It is pure Python and linear in list size.
  A check is 9 sessions; with members in separate processes it took 2.6-3.0 s at 300 entries and 8-10 s at 1,000.
- **Initiator hosting:** the initiator must serve its own AgentCard so members can verify its signed intents.
- **Signatures:** the result directive is signed with the initiator's own key. Member replies are unsigned; a member's
  only signature is on the commitment in its AgentCard.

## Decision
- Each member holds about 300 entries in one combined list. The field-prefixed hashes from ADR 0003 keep fields from
  matching each other.
- The initiator PrivacyAgent runs inside the backend process on port 8100.
- A receipt stores our 2 signed intents, our signed result and the member's signed commitment, and is described exactly
  that way. It is never called "member-signed".

## Alternatives considered
- Per-field receiver agents (3 per member) would keep ~1,000 entries at a similar speed, but means 9 receiver services.
- Keeping ~1,000 entries means 8-10 s per check in the demo.
- A separate initiator process would be cleaner isolation, at the cost of one more service.

## Consequences
- D7 seeds ~300 entries per member.
- Render needs `allow_private_initiator_urls=True` on members if they reach the initiator over private-network URLs.
- The README and the who-saw-what panel say who signed what.
