# 0002. Deterministic decision policy; the AI explains, never decides
_Date: 2026-10-12 · Status: accepted_

## Context
The risk agent's outcome moves money (authorise, capture) or refuses a buyer. Judges and merchants need to trust it.

## Decision
A fixed policy decides: 0 hits clean, 1 hit hold, 2+ hits (or email and device both hit) block. A hit is one
(member, field) PSI match, so `hit_count` runs 0-9. The LLM writes a 2-3 sentence rationale from the hit summary only.
`/merchant/decide` recomputes the policy and rejects a mismatched decision (`409 policy_mismatch`).

## Alternatives considered
LLM decides: less predictable and harder to defend around money. Counting distinct fields or members: would make the
email-and-device clause meaningful, but adds a second notion of "hit" for little demo value.

## Consequences
Outcomes are testable and repeatable for the three personas. The email-and-device clause is redundant under pair
counting; policy code checks it anyway so a later counting change can't drop it silently.
