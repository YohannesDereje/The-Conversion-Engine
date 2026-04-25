"""
Warm reply composer (P3-F2/F3) — composes replies to warm prospect replies.

Functions:
  detect_handoff_triggers:  checks 5 warm.md handoff conditions (P3-F3)
  compose_handoff_message:  fixed-template routing message (no LLM)
  compose_engaged_reply:    grounded answer + discovery call ask (≤150 words)
  compose_curious_reply:    targeted context + Cal link (≤90 words)
  compose_soft_defer_reply: gracious close + re-engagement date (≤60 words)
  compose_objection_reply:  handles price / incumbent_vendor / poc_only (≤120 words)
  handle_hard_no:           marks opted-out in HubSpot + Langfuse, no reply email
  handle_ambiguous_reply:   routes to human review via HubSpot note, no reply email
"""
import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent.utils import CALCOM_BASE_URL, emit_span, compute_cost_usd

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_BENCH = json.loads(
    (_ROOT / "tenacious_sales_data" / "seed" / "bench_summary.json").read_text(encoding="utf-8")
)

_OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-235b-a22b")
_DEV_TEMP = float(os.getenv("DEV_MODEL_TEMPERATURE", "0.0"))

_oai_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = AsyncOpenAI(api_key=_OPENROUTER_API_KEY, base_url=_OPENROUTER_BASE_URL)
    return _oai_client


# ── handoff detection (P3-F3) ─────────────────────────────────────────────────

_PRICING_KEYWORDS = frozenset({
    "discount", "negotiate", "lower rate", "better price", "cheaper",
    "reduce the price", "total contract value", "annual contract",
    "custom pricing", "budget is", "what's the total", "how much total",
    "can you do better", "match the price",
})
_CLIENT_REF_KEYWORDS = frozenset({
    "reference", "case study", "speak to a client", "talk to a customer",
    "past client", "client reference", "existing client", "can i speak with",
    "who have you worked with", "portfolio", "past work",
})
_LEGAL_KEYWORDS = frozenset({
    "msa", "master services agreement", "dpa", "data processing agreement",
    "soc 2", "soc2", "hipaa", "baa", "business associate agreement",
    "contract terms", "legal review", "compliance review", "specific clauses",
    "liability", "indemnification", "ip ownership", "intellectual property",
    "nda", "non-disclosure", "gdpr", "data residency",
})
_STAFFING_KEYWORDS = frozenset({
    "databricks specialist", "databricks expert", "available starting",
    "available in", "starting in", "with healthcare experience",
    "with fintech experience", "with pharma experience",
    "security clearance", "specific engineer", "particular skill",
    "with experience in", "specialist available",
})
# Regex: catches "5 NestJS engineers", "10 ML engineers", "provide 8 engineers",
# "guarantee 8 engineers start" — numeric headcount commitments require human bench check.
_STAFFING_COUNT_RE = re.compile(
    r"\b\d+\s+(?:\w+\s+)?engineers?\b"           # "5 engineers", "5 NestJS engineers"
    r"|\b(?:provide|supply|assign|need|want|require)\s+\d+\s+engineers?\b"  # "provide 5 engineers"
    r"|\bguarantee\b.{0,80}\bengineer",            # "guarantee 8 engineers start"
    re.IGNORECASE,
)
_CLEVEL_TITLES = frozenset({
    "ceo", "cto", "coo", "cfo", "cpo", "chief", "president", "founder", "co-founder",
})


def detect_handoff_triggers(reply_text: str, contact_info: dict) -> dict:
    """
    Check 5 warm.md handoff conditions.

    contact_info keys used: title (str), headcount (int).

    Returns:
        {handoff: bool, trigger: str, reason: str}
    """
    text_lower = reply_text.lower()

    # Trigger 1: pricing outside quotable bands
    if any(kw in text_lower for kw in _PRICING_KEYWORDS):
        return {
            "handoff": True,
            "trigger": "pricing_outside_bands",
            "reason": "Prospect is asking about custom pricing or discounts outside standard rates.",
        }

    # Trigger 2: specific staffing beyond bench confirmation
    # Catches both keyword phrases AND numeric headcount requests ("5 NestJS engineers",
    # "provide 10 ML engineers", "guarantee 8 engineers") — any specific count requires
    # human bench verification to prevent over-commitment (BOC probes).
    if any(kw in text_lower for kw in _STAFFING_KEYWORDS) or _STAFFING_COUNT_RE.search(reply_text):
        return {
            "handoff": True,
            "trigger": "specific_staffing",
            "reason": "Prospect is requesting specific staffing that requires human confirmation.",
        }

    # Trigger 3: public client reference
    if any(kw in text_lower for kw in _CLIENT_REF_KEYWORDS):
        return {
            "handoff": True,
            "trigger": "client_reference",
            "reason": "Prospect is asking for a named client reference.",
        }

    # Trigger 4: legal / regulatory terms
    if any(kw in text_lower for kw in _LEGAL_KEYWORDS):
        return {
            "handoff": True,
            "trigger": "legal_terms",
            "reason": "Prospect is referencing legal, contractual, or regulatory terms.",
        }

    # Trigger 5: C-level executive at company with >2000 headcount
    title = str(contact_info.get("title", "")).lower()
    headcount = int(contact_info.get("headcount", 0) or 0)
    is_clevel = any(t in title for t in _CLEVEL_TITLES)
    if is_clevel and headcount > 2000:
        return {
            "handoff": True,
            "trigger": "clevel_large_company",
            "reason": f"C-level executive at a {headcount}-person company (>2000 threshold).",
        }

    return {"handoff": False, "trigger": "", "reason": ""}


def compose_handoff_message(contact_name: str, original_subject: str) -> dict:
    """Fixed-template handoff message — no LLM. Tells the prospect a human will follow up."""
    first_name = contact_name.split()[0] if contact_name else ""
    greeting = f"{first_name},\n\n" if first_name else ""
    body = (
        f"{greeting}"
        "Thanks for the context. This is outside what I can confirm directly — "
        "our delivery lead will follow up within 24 hours with specifics.\n\n"
        "Elena\nResearch Partner, Tenacious Intelligence Corporation\ngettenacious.com"
    )
    return {"subject": f"Re: {original_subject[:55]}", "body": body}


# ── shared helpers ────────────────────────────────────────────────────────────

def _format_cal_block(slots: list) -> str:
    if not slots:
        return f"\n\n→ Book a 15-minute call: {CALCOM_BASE_URL}"
    lines = []
    for slot in slots[:2]:
        time_str = slot.get("time", "")
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            day_name = dt.strftime("%A")
            mon_day = dt.strftime("%b %d")
            h = int(dt.strftime("%I"))
            m = dt.strftime("%M")
            ampm = dt.strftime("%p")
            lines.append(f"→ {day_name} {mon_day}, {h}:{m} {ampm} UTC  [book: {CALCOM_BASE_URL}]")
        except Exception:
            lines.append(f"→ {time_str}  [book: {CALCOM_BASE_URL}]")
    return "\n\n" + "\n".join(lines)


def _enforce_word_limit(body: str, max_words: int) -> tuple[str, bool]:
    words = body.split()
    if len(words) <= max_words:
        return body, False
    return " ".join(words[:max_words]), True


def _parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
    if "<think>" in text and "</think>" in text:
        text = text[text.rfind("</think>") + len("</think>"):].strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*?\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


async def _run_tone_probe(subject: str, body: str, trace_id: str) -> tuple[dict | None, list]:
    flags = []
    tone_result = None
    try:
        from agent.tone_probe import score_tone
        tone_result = await score_tone(subject, body, trace_id)
        if not tone_result.get("passed", True):
            flags.append("tone_violation")
    except Exception:
        pass
    return tone_result, flags


# ── engaged reply ─────────────────────────────────────────────────────────────

_ENGAGED_SYSTEM = """\
You write warm reply emails for Tenacious Consulting — a managed engineering delivery firm
with a delivery team in Addis Ababa serving US and EU scale-ups.

Structure (max 150 words body):
1. Thank the prospect — one sentence, not effusive, not "great question".
2. Direct answer to the specific question raised. Grounded in the hiring brief. Cite specifics.
   Never invent data. If something is unknown, say it honestly.
3. ONE additional piece of value: either a data point from the competitor gap brief,
   OR a concrete engagement structure example. Never more than one.
4. The ask: 15 or 30 minutes, two time slot options (day + time), mention Cal link is below.
5. Signature: Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com

Tone: Direct, Grounded, Honest, Professional, Non-condescending. No emojis.
Never improvise pricing. Never commit to staffing beyond what the bench shows.

Return ONLY valid JSON: {"subject": "Re: <original_subject_up_to_55_chars>", "body": "<body_text>"}
"""


async def compose_engaged_reply(
    hiring_brief: dict,
    competitor_brief: dict,
    reply_text: str,
    contact_name: str,
    original_subject: str,
    cal_slots: list,
    trace_id: str = "",
) -> dict:
    """
    Compose a warm engaged reply (≤150 words body).

    Returns:
        {subject, body, word_count, honesty_flags, tone_probe_result}
    """
    t0 = time.monotonic()
    first_name = contact_name.split()[0] if contact_name else ""
    bench_avail = {k: v.get("available_engineers", 0) for k, v in _BENCH.get("stacks", {}).items()}
    gap_findings = (competitor_brief.get("gap_findings") or [])[:2]

    user_prompt = (
        f"Original email subject: {original_subject}\n"
        f"Prospect first name: {first_name or 'not known'}\n\n"
        f"Prospect's reply:\n{reply_text}\n\n"
        f"Key hiring brief facts:\n"
        f"{json.dumps({k: hiring_brief.get(k) for k in ('prospect_name','primary_segment_match','hiring_velocity','ai_maturity','funding')}, indent=2)}\n\n"
        f"Bench availability: {json.dumps(bench_avail)}\n\n"
        f"Competitor gap findings: {json.dumps(gap_findings)}\n\n"
        "Write the engaged reply. Max 150 words body. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=600,
            messages=[
                {"role": "system", "content": _ENGAGED_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or f"Re: {original_subject[:55]}")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 150)
    body_for_tone = body
    body = body + _format_cal_block(cal_slots)

    flags = []
    if truncated:
        flags.append("reply_body_truncated_at_150_words")
    if not parsed:
        flags.append("llm_parse_failed")

    tone_result, tone_flags = await _run_tone_probe(subject, body_for_tone, trace_id)
    flags.extend(tone_flags)

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.compose_engaged_reply",
        input={"reply_text": reply_text[:200]},
        output={"subject": subject, "word_count": len(body_for_tone.split())},
        latency_ms=latency_ms,
        metadata={
            "model": _DEV_MODEL,
            "input_tokens": int(getattr(_usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(_usage, "completion_tokens", 0) or 0),
        },
    )
    return {
        "subject": subject,
        "body": body,
        "word_count": len(body_for_tone.split()),
        "honesty_flags": flags,
        "tone_probe_result": tone_result,
    }


# ── curious reply ─────────────────────────────────────────────────────────────

_CURIOUS_SYSTEM = """\
You write warm reply emails for Tenacious Consulting — a managed engineering delivery firm.

Structure (max 90 words body):
1. One-sentence hook tying back to the signal from the cold email.
2. Three sentences describing what Tenacious does, calibrated to the prospect's segment.
   No service menu, no bullet lists.
3. The ask: 15 minutes, mention Cal link is below.
4. Signature: Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com

Tone: Direct, Grounded, Honest, Professional, Non-condescending. No emojis.

Return ONLY valid JSON: {"subject": "Re: <original_subject_up_to_55_chars>", "body": "<body_text>"}
"""


async def compose_curious_reply(
    hiring_brief: dict,
    reply_text: str,
    contact_name: str,
    original_subject: str,
    cal_slots: list,
    trace_id: str = "",
) -> dict:
    """
    Compose a warm curious reply (≤90 words body).

    Returns:
        {subject, body, word_count, honesty_flags, tone_probe_result}
    """
    t0 = time.monotonic()
    first_name = contact_name.split()[0] if contact_name else ""

    user_prompt = (
        f"Original email subject: {original_subject}\n"
        f"Prospect first name: {first_name or 'not known'}\n"
        f"Segment: {hiring_brief.get('primary_segment_match', 'unknown')}\n"
        f"Company: {hiring_brief.get('prospect_name', 'the company')}\n\n"
        f"Prospect's reply:\n{reply_text}\n\n"
        "Write the curious reply. Max 90 words body. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=400,
            messages=[
                {"role": "system", "content": _CURIOUS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or f"Re: {original_subject[:55]}")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 90)
    body_for_tone = body
    body = body + _format_cal_block(cal_slots)

    flags = []
    if truncated:
        flags.append("reply_body_truncated_at_90_words")
    if not parsed:
        flags.append("llm_parse_failed")

    tone_result, tone_flags = await _run_tone_probe(subject, body_for_tone, trace_id)
    flags.extend(tone_flags)

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.compose_curious_reply",
        input={"reply_text": reply_text[:200]},
        output={"subject": subject, "word_count": len(body_for_tone.split())},
        latency_ms=latency_ms,
        metadata={
            "model": _DEV_MODEL,
            "input_tokens": int(getattr(_usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(_usage, "completion_tokens", 0) or 0),
        },
    )
    return {
        "subject": subject,
        "body": body,
        "word_count": len(body_for_tone.split()),
        "honesty_flags": flags,
        "tone_probe_result": tone_result,
    }


# ── soft defer reply ──────────────────────────────────────────────────────────

_SOFT_DEFER_SYSTEM = """\
You write warm reply emails for Tenacious Consulting.

Structure (max 60 words body):
1. One sentence acknowledging the timing isn't right. No guilt, no pushback.
2. One sentence with a concrete re-engagement plan — name a specific month (e.g., "early Q3 2026").
3. Signature: Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com

Tone: Direct, Grounded, Honest, Professional, Non-condescending. No emojis.

Return ONLY valid JSON: {"subject": "...", "body": "...", "reengage_month": "<e.g. Q3 2026>"}
"""


async def compose_soft_defer_reply(
    reply_text: str,
    contact_name: str,
    original_subject: str,
    trace_id: str = "",
) -> dict:
    """
    Compose a gracious soft-defer close (≤60 words body).

    Returns:
        {subject, body, word_count, reengage_month, honesty_flags, tone_probe_result}
    """
    t0 = time.monotonic()
    first_name = contact_name.split()[0] if contact_name else ""

    user_prompt = (
        f"Original email subject: {original_subject}\n"
        f"Prospect first name: {first_name or 'not known'}\n\n"
        f"Prospect's reply:\n{reply_text}\n\n"
        "Write the soft-defer reply. Max 60 words body. "
        'Return ONLY JSON: {"subject": "...", "body": "...", "reengage_month": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=300,
            messages=[
                {"role": "system", "content": _SOFT_DEFER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or f"Re: {original_subject[:55]}")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 60)
    reengage_month = str(parsed.get("reengage_month") or "Q3 2026")

    flags = []
    if truncated:
        flags.append("reply_body_truncated_at_60_words")
    if not parsed:
        flags.append("llm_parse_failed")

    tone_result, tone_flags = await _run_tone_probe(subject, body, trace_id)
    flags.extend(tone_flags)

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.compose_soft_defer_reply",
        input={"reply_text": reply_text[:200]},
        output={"subject": subject, "word_count": len(body.split())},
        latency_ms=latency_ms,
        metadata={
            "model": _DEV_MODEL,
            "input_tokens": int(getattr(_usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(_usage, "completion_tokens", 0) or 0),
        },
    )
    return {
        "subject": subject,
        "body": body,
        "word_count": len(body.split()),
        "reengage_month": reengage_month,
        "honesty_flags": flags,
        "tone_probe_result": tone_result,
    }


# ── objection reply ───────────────────────────────────────────────────────────

_OBJECTION_SYSTEM = """\
You write warm objection-handling emails for Tenacious Consulting. Max 120 words body.

PRICE OBJECTION ("Your price is higher than India"):
- Acknowledge the price differential directly — no denial
- The Tenacious answer: reliability, overlap hours, retention. Not price matching.
- ONE concrete mechanism: 18-month average engineer tenure, 3-hour overlap guarantee,
  or full HR/insurance coverage
- The ask: 15-min call to walk through the mechanism, NOT to negotiate price
- NEVER improvise a discount or custom price

INCUMBENT VENDOR OBJECTION ("We already have a vendor / in-house team"):
- Acknowledge the existing arrangement likely works for core scope
- The gap Tenacious fills: specialized capability or new-initiative capacity
- Reference one specific finding from the competitor gap brief if available
- The ask for a discovery call

POC-ONLY OBJECTION ("We only need a small POC"):
- Acknowledge starting small is the right move — never push back
- Name the starter-project floor (use "$[PROJECT_ACV_MIN]" — do not invent a number)
- Ask the prospect to describe the smallest deliverable that proves value
- The ask for a discovery call to scope

Tone: Direct, Grounded, Honest, Professional, Non-condescending. No emojis.

Return ONLY valid JSON: {"subject": "Re: <original_subject_up_to_55_chars>", "body": "<body_text>"}
"""


async def compose_objection_reply(
    hiring_brief: dict,
    competitor_brief: dict,
    reply_text: str,
    objection_type: str,
    contact_name: str,
    original_subject: str,
    cal_slots: list,
    trace_id: str = "",
) -> dict:
    """
    Compose a warm objection reply (≤120 words body).
    objection_type: price | incumbent_vendor | poc_only | other

    Returns:
        {subject, body, word_count, honesty_flags, tone_probe_result}
    """
    t0 = time.monotonic()
    first_name = contact_name.split()[0] if contact_name else ""
    gap_findings = (competitor_brief.get("gap_findings") or [])[:1]

    user_prompt = (
        f"Objection type: {objection_type}\n"
        f"Original email subject: {original_subject}\n"
        f"Prospect first name: {first_name or 'not known'}\n\n"
        f"Prospect's reply:\n{reply_text}\n\n"
        f"Competitor gap findings: {json.dumps(gap_findings)}\n\n"
        "Write the objection reply. Max 120 words body. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=500,
            messages=[
                {"role": "system", "content": _OBJECTION_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or f"Re: {original_subject[:55]}")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 120)
    body_for_tone = body
    body = body + _format_cal_block(cal_slots)

    flags = []
    if truncated:
        flags.append("reply_body_truncated_at_120_words")
    if not parsed:
        flags.append("llm_parse_failed")

    tone_result, tone_flags = await _run_tone_probe(subject, body_for_tone, trace_id)
    flags.extend(tone_flags)

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.compose_objection_reply",
        input={"reply_text": reply_text[:200], "objection_type": objection_type},
        output={"subject": subject, "word_count": len(body_for_tone.split())},
        latency_ms=latency_ms,
        metadata={
            "model": _DEV_MODEL,
            "input_tokens": int(getattr(_usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(_usage, "completion_tokens", 0) or 0),
        },
    )
    return {
        "subject": subject,
        "body": body,
        "word_count": len(body_for_tone.split()),
        "honesty_flags": flags,
        "tone_probe_result": tone_result,
    }


# ── hard no / ambiguous handlers ──────────────────────────────────────────────

async def handle_hard_no(
    contact_id: str,
    from_email: str,
    reply_text: str,
    trace_id: str = "",
) -> dict:
    """
    Handle an opt-out reply.
    - No reply email — ever.
    - Marks HubSpot contact DISQUALIFIED + outreach_sequence_step=hard_no.
    - Logs Langfuse span with reply text for probe-library analysis.

    Returns:
        {status, action, contact_id}
    """
    t0 = time.monotonic()
    try:
        from agent.hubspot_handler import update_lead_status, update_sequence_step
        await update_lead_status(
            contact_id, "DISQUALIFIED",
            reason="hard_no reply received", trace_id=trace_id,
        )
        await update_sequence_step(
            contact_id, "hard_no",
            datetime.now(timezone.utc).isoformat(), trace_id,
        )
    except Exception:
        pass

    result = {
        "status": "handled",
        "action": "opted_out_no_reply",
        "contact_id": contact_id,
        "from_email": from_email,
    }
    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.handle_hard_no",
        input={"contact_id": contact_id, "reply_text": reply_text[:300]},
        output=result,
        latency_ms=latency_ms,
    )
    return result


async def handle_ambiguous_reply(
    contact_id: str,
    reply_text: str,
    trace_id: str = "",
) -> dict:
    """
    Handle an ambiguous reply.
    - No reply email.
    - Logs a HubSpot note flagging for human review.
    - Logs Langfuse span.

    Returns:
        {status, action, contact_id}
    """
    t0 = time.monotonic()
    try:
        from agent.hubspot_handler import log_email_activity
        await log_email_activity(
            contact_id=contact_id,
            email_data={
                "to": "",
                "subject": "[AMBIGUOUS REPLY — needs human review]",
                "body": reply_text,
                "resend_id": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            trace_id=trace_id,
        )
    except Exception:
        pass

    result = {
        "status": "routed_to_human",
        "action": "ambiguous_reply_flagged_for_review",
        "contact_id": contact_id,
    }
    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_composer.handle_ambiguous_reply",
        input={"contact_id": contact_id, "reply_text": reply_text[:300]},
        output=result,
        latency_ms=latency_ms,
    )
    return result
