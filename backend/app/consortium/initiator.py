"""The merchant's AP3 initiator: runs PSI sessions against the members and verifies receipts.

It serves its own AgentCard (port 8100 locally) because members fetch it to verify our signed
intents (ADR 0004). It runs inside the backend process, started from the FastAPI lifespan.
"""

import asyncio
import contextlib
import contextvars
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ap3.a2a import PrivacyAgent
from ap3.a2a.client import PeerClient, PeerInfo
from ap3.services.commitment import CommitmentMetadataSystem
from ap3.types import (
    CommitmentMetadata,
    DataStructure,
    PrivacyIntentDirective,
    PrivacyResultDirective,
)
from ap3_functions import PSIOperation

from app.consortium.common import build_commitment, load_keypair
from app.identifiers import FIELDS

logger = logging.getLogger("quietcheck.initiator")

# The intents sent by the current session's task; each session runs in its own task.
_session_intents: contextvars.ContextVar[list[tuple[str, dict]]] = contextvars.ContextVar(
    "session_intents"
)


class RecordingPeerClient(PeerClient):
    """Captures the signed PrivacyIntentDirectives we send, since run_intent doesn't return them."""

    async def send_envelope(self, *, peer_url, envelope, context_id=None):
        sent = _session_intents.get(None)
        if sent is not None and envelope.privacy_intent is not None:
            sent.append((envelope.phase, envelope.privacy_intent))
        return await super().send_envelope(
            peer_url=peer_url, envelope=envelope, context_id=context_id
        )


@dataclass(frozen=True)
class SessionResult:
    member: str
    field: str
    is_match: bool
    session_id: str
    intent_directives: list[dict]  # phases init, msg1, in send order
    result_directive: dict
    member_commitment: dict
    member_public_key: str  # hex
    verified: bool


class ConsortiumError(Exception):
    """One or more PSI sessions didn't complete. Carries no identifiers or hashes."""

    def __init__(self, failures: list[tuple[str, str, str]]):
        self.failures = failures  # (member, field, error type name)
        super().__init__(f"{len(failures)} PSI session(s) failed")


def verify_receipt(
    intent_directives: list[dict],
    result_directive: dict,
    member_commitment: dict,
    member_public_key: bytes,
    initiator_public_key: bytes,
) -> bool:
    """True when both intents and the result carry our valid signature, both intents belong
    to the result's session, and the member's commitment carries the member's valid signature."""
    try:
        result = PrivacyResultDirective.model_validate(result_directive)
        intents = [PrivacyIntentDirective.model_validate(d) for d in intent_directives]
        commitment = CommitmentMetadata.model_validate(member_commitment)
    except ValueError:
        return False
    return (
        len(intents) == 2
        and all(i.ap3_session_id == result.ap3_session_id for i in intents)
        and all(i.verify_signature(initiator_public_key) for i in intents)
        and result.verify_signature(initiator_public_key)
        and CommitmentMetadataSystem.verify_commitment_signature(commitment, member_public_key)
    )


class Initiator:
    def __init__(
        self,
        *,
        port: int,
        key_path: Path,
        member_urls: dict[str, str],
        host: str = "127.0.0.1",
        card_url: str = "",
        private_key: str = "",
    ):
        """`card_url` is the URL members reach us at; it must be on their allowlist. Hosted, it
        is the backend's private-network address and `host` is 0.0.0.0 (D9)."""
        self.member_urls = member_urls
        self.card_url = card_url or f"http://127.0.0.1:{port}"
        self.private_key, self.public_key = load_keypair(key_path, private_key)
        self.peer_client = RecordingPeerClient()
        self.agent = PrivacyAgent(
            name="Quiet Check merchant risk",
            description="Merchant risk agent: PSI initiator against the consortium members",
            card_url=self.card_url,
            port=port,
            host=host,
            role="ap3_initiator",
            operation=PSIOperation(),
            commitment=build_commitment("quietcheck_merchant", DataStructure.CUSTOMER_LIST, 1),
            private_key=self.private_key,
            public_key=self.public_key,
            peer_client=self.peer_client,
        )
        self._stack: contextlib.AsyncExitStack | None = None

    async def start(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        await self._stack.enter_async_context(self.agent.serving())
        logger.info(
            "AP3 initiator serving as %s; members: %s",
            self.card_url,
            ", ".join(f"{m} {url}" for m, url in self.member_urls.items()),
        )

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def _session(self, member: str, field: str, peer: PeerInfo, value_hash: str):
        sent: list[tuple[str, dict]] = []
        _session_intents.set(sent)
        result = await self.agent.run_intent(
            peer_url=self.member_urls[member], inputs={"customer_data": value_hash}
        )
        is_match = bool(json.loads(result.result_data.metadata["description"])["is_match"])
        intents = [intent for _phase, intent in sent]
        result_json = result.model_dump(mode="json")
        commitment_json = peer.ap3_params.commitments[0].model_dump(mode="json")
        verified = verify_receipt(
            intents, result_json, commitment_json, peer.public_key, self.public_key
        )
        if [phase for phase, _ in sent] != ["init", "msg1"]:
            verified = False
        return SessionResult(
            member=member,
            field=field,
            is_match=is_match,
            session_id=result.ap3_session_id,
            intent_directives=intents,
            result_directive=result_json,
            member_commitment=commitment_json,
            member_public_key=peer.public_key.hex(),
            verified=verified,
        )

    async def run_check(self, hashes: dict[str, str], timeout_s: float) -> list[SessionResult]:
        """One PSI session per (member, field), all concurrent. All-or-nothing: raises
        ConsortiumError if any session fails. Results are ordered by member, then field."""
        jobs = [(m, f) for m in self.member_urls for f in FIELDS]
        failures: list[tuple[str, str, str]] = []

        async def resolve(member: str) -> PeerInfo:
            # Refresh so a member restarted with a new list shows its current commitment.
            return await self.peer_client.resolve_peer(self.member_urls[member], force_refresh=True)

        peers: dict[str, PeerInfo] = {}
        resolved = await asyncio.gather(
            *(resolve(m) for m in self.member_urls), return_exceptions=True
        )
        for member, outcome in zip(self.member_urls, resolved, strict=True):
            if isinstance(outcome, BaseException):
                failures += [(member, f, type(outcome).__name__) for f in FIELDS]
            else:
                peers[member] = outcome
        if failures:
            raise ConsortiumError(failures)

        tasks = [asyncio.create_task(self._session(m, f, peers[m], hashes[f])) for m, f in jobs]
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            task.cancel()
        results: list[SessionResult] = []
        for (member, field), task in zip(jobs, tasks, strict=True):
            if task in pending:
                failures.append((member, field, "TimeoutError"))
            elif task.exception() is not None:
                failures.append((member, field, type(task.exception()).__name__))
            else:
                results.append(task.result())
        if failures:
            for member, field, error in failures:
                logger.warning(
                    "PSI session failed: member=%s field=%s error=%s", member, field, error
                )
            raise ConsortiumError(failures)
        return results
