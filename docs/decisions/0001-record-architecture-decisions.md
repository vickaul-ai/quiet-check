# 0001. Record architecture decisions
_Date: 2026-10-12 · Status: accepted_

## Context
Quiet Check is built across many separate agent sessions. Decisions made in one session must survive into the next.

## Decision
Record non-obvious decisions as short ADRs in `docs/decisions/`, numbered `NNNN-title.md`, using this format.

## Alternatives considered
Chat history only: lost at every context reset. One decisions table: fine for one-liners, too thin for trade-offs.

## Consequences
Each session that makes a non-obvious trade-off adds an ADR in the same change.
