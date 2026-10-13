# 0003. Identifier normalisation and hashing
_Date: 2026-10-12 · Status: accepted_

## Context
PSI only matches identical values, so the risk service and every member must turn the same buyer into the same bytes.

## Decision
One shared function: trim and lowercase emails and device IDs; normalise addresses field by field (NFKC, lowercase,
collapse whitespace, strip punctuation, ISO country code) and join them with `|`; then hash as
`sha256("<field>:" + value)`. Exact rules are in `docs/contracts.md`.

## Alternatives considered
Gmail dot and `+tag` stripping: catches more evasion but is provider-specific and harder to explain. No hashing: PSI
already hides values, but member files would then hold plaintext.

## Consequences
Simple to test and explain. Address matching stays exact after normalisation, so small typos won't match.
