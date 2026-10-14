"""The 2-3 sentence rationale for a decision (docs/brief.md, "Models"; D4).

The model sees only the hit summary: the decision, its PayPal action, and which member matched
which field. Never identifiers, hashes, list contents or other buyers' data. It writes text and
nothing else: the decision is fixed before it is called and re-checked by /merchant/decide.

If the model fails or takes longer than `rationale_timeout_s` (about 10 s), the decision goes
ahead with a template rationale built from the same summary, marked `rationale_source:
"template"` in the `decision.made` event (decided with Vic, 2026-10-13).
"""

import asyncio
import json
import logging

from app.config import MEMBERS, REPO_DIR, Settings
from app.identifiers import FIELDS
from app.policy import PAYPAL_ACTIONS

logger = logging.getLogger("quietcheck.rationale")

PROMPT_PATH = REPO_DIR / "prompts" / "risk_rationale.md"
FIELD_LABELS = {"email": "email", "shipping_address": "address", "device_id": "device"}
MAX_CHARS = 1000


def hit_summary(decision: str, hits: list[dict]) -> dict:
    """What the model may see: no identifiers, hashes or list contents."""
    matched = [{"member": h["member"], "field": h["field"]} for h in hits if h["is_match"]]
    hit_fields = {h["field"] for h in matched}
    return {
        "decision": decision,
        "paypal_action": PAYPAL_ACTIONS[decision],
        "hit_count": len(matched),
        "members_checked": list(MEMBERS),
        "fields_checked": list(FIELDS),
        "hits": matched,
        "fields_clear_at_all_members": [f for f in FIELDS if f not in hit_fields],
    }


def _join(words: list[str]) -> str:
    if len(words) <= 2:
        return " and ".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def template_rationale(summary: dict) -> str:
    """e.g. "Automatic explanation unavailable. Policy decision: hold. 1 hit: Member B, email.
    Address and device were clear at all members." """
    count = summary["hit_count"]
    parts = [f"Automatic explanation unavailable. Policy decision: {summary['decision']}."]
    if count:
        hits = "; ".join(
            f"Member {h['member']}, {FIELD_LABELS[h['field']]}" for h in summary["hits"]
        )
        parts.append(f"{count} hit{'s' if count > 1 else ''}: {hits}.")
    else:
        parts.append("No hits.")
    clear = [FIELD_LABELS[f] for f in summary["fields_clear_at_all_members"]]
    if clear:
        verb = "was" if len(clear) == 1 else "were"
        parts.append(f"{_join(clear).capitalize()} {verb} clear at all members.")
    return " ".join(parts)


def _model(settings: Settings):
    if settings.rationale_model is not None:
        return settings.rationale_model
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        f"{settings.llm_provider}:{settings.llm_model_fast}",
        timeout=settings.rationale_timeout_s,
        max_retries=1,
        # Room for a thinking block before the answer: Haiku sometimes thinks first, and a
        # 300-token cap produced an empty reply once in the live persona run (2026-10-13).
        max_tokens=1024,
    )


def _text(response) -> str:
    text = getattr(response, "text", None)
    text = str(text) if text is not None else str(getattr(response, "content", ""))
    return " ".join(text.split())[:MAX_CHARS]


async def _ask(model, messages) -> str:
    """One answer, asking a second time if the first reply has no text."""
    for attempt in (1, 2):
        text = _text(await model.ainvoke(messages))
        if text:
            return text
        logger.warning("rationale model returned no text (attempt %d)", attempt)
    return ""


async def write_rationale(settings: Settings, decision: str, hits: list[dict]) -> tuple[str, str]:
    """(rationale, source), where source is "llm" or "template". Never raises."""
    summary = hit_summary(decision, hits)
    try:
        model = _model(settings)
        messages = [
            ("system", PROMPT_PATH.read_text()),
            ("human", json.dumps(summary)),
        ]
        text = await asyncio.wait_for(_ask(model, messages), timeout=settings.rationale_timeout_s)
        if text:
            return text, "llm"
        logger.warning("rationale model gave no text twice; using the template")
    except Exception as exc:  # any model, network or timeout error: the decision still goes ahead
        logger.warning("rationale model failed (%s); using the template", type(exc).__name__)
    return template_rationale(summary), "template"
