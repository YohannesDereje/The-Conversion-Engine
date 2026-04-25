"""
Re-engagement composer (P3-H1) — three-email stalled-thread recovery sequence.

Based on tenacious_sales_data/seed/email_sequences/reengagement.md.

Functions:
  check_reengagement_eligible: all four eligibility conditions from reengagement.md
  compose_reengagement_email_1: "New data point" email, max 100 words body
  compose_reengagement_email_2: "One specific question" email, max 50 words body
  compose_reengagement_email_3: "6-month close" email, max 40 words body
"""
import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent.utils import emit_span, compute_cost_usd

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent

_OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-235b-a22b")
_DEV_TEMP = float(os.getenv("DEV_MODEL_TEMPERATURE", "0.0"))

_oai_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = AsyncOpenAI(
            api_key=_OPENROUTER_API_KEY,
            base_url=_OPENROUTER_BASE_URL,
        )
    return _oai_client


def _parse_json(text: str) -> dict | None:
    if "<think>" in text and "</think>" in text:
        text = text[text.rfind("</think>") + len("</think>"):].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def _enforce_word_limit(body: str, max_words: int) -> tuple[str, bool]:
    words = body.split()
    if len(words) <= max_words:
        return body, False
    return " ".join(words[:max_words]), True


# ── eligibility check ─────────────────────────────────────────────────────────

def check_reengagement_eligible(contact: dict) -> tuple[bool, str]:
    """
    All four conditions from reengagement.md must be true.

    Args:
        contact: HubSpot contact properties dict (from get_contact_by_email or similar)

    Returns:
        (eligible: bool, reason: str)
    """
    props = contact.get("properties", contact)

    status = str(props.get("hs_lead_status", "")).upper()
    if status in ("OPTED_OUT", "DISQUALIFIED"):
        return False, f"hs_lead_status is {status}"

    seq_step = str(props.get("outreach_sequence_step", "") or "")
    if "hard_no" in seq_step or "opted_out" in seq_step:
        return False, "hard_no or opted_out in outreach_sequence_step"

    if status != "STALLED":
        return False, f"contact is not STALLED (current: {status})"

    last_sent = props.get("outreach_last_sent_at", "") or ""
    if last_sent:
        try:
            from datetime import datetime, timezone, timedelta
            last_dt = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since < 45:
                return False, f"last re-engagement was {days_since} days ago (min 45)"
        except Exception:
            pass

    return True, "eligible"


# ── system prompts ────────────────────────────────────────────────────────────

_REENGAGE_1_SYSTEM = """\
You write re-engagement emails for Tenacious Consulting (engineering outsourcing).

This is re-engagement email 1 — "New data point". The prospect previously replied
engaged or curious but never booked a call. The thread has gone cold.

Rules:
- Subject: under 60 chars. Pattern: "Update on [sector] hiring signal" or similar.
- Body: max 100 words. Structure: one-line re-opener (no "just following up") →
  the new data point (2 sentences) → why it matters for this prospect →
  soft ask (offer a one-pager, reply "yes" — NO calendar link in this email).
- Signature: "Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com"
- Never apologize for reaching out. Never reference silence explicitly.
- Tone: Direct, Grounded, Honest, Professional. No emojis.

Return ONLY valid JSON: {"subject": "...", "body": "..."}
"""

_REENGAGE_2_SYSTEM = """\
You write re-engagement emails for Tenacious Consulting (engineering outsourcing).

This is re-engagement email 2 — "One specific question". No reply was received to
re-engagement email 1. Lower the bar to a one-line reply.

Rules:
- Subject: under 60 chars. Pattern: "One specific question" or "Last note on [topic]".
- Body: max 50 words. Structure: single-sentence opener → ONE specific yes/no question
  the prospect can answer in one line → signature.
- Signature: "Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com"
- Never stack multiple questions. Never use urgency language.
- Tone: Direct, Grounded, Honest, Professional. No emojis.

Return ONLY valid JSON: {"subject": "...", "body": "..."}
"""

_REENGAGE_3_SYSTEM = """\
You write re-engagement emails for Tenacious Consulting (engineering outsourcing).

This is re-engagement email 3 — the gracious 6-month close. No reply was received
to re-engagement email 2. This is the final touch; the thread closes after this.

Rules:
- Subject: under 60 chars. Pattern: "Parking this — [specific quarter] check-in".
- Body: max 40 words. One sentence: thread closed for now. Specific re-engagement date
  (month + year, not "sometime later"). Signature.
- Signature: "Elena / Research Partner, Tenacious Intelligence Corporation / gettenacious.com"
- Explicitly park, do not abandon. No guilt language. No deadline urgency.
- Tone: Direct, Grounded, Honest, Professional. No emojis.

Return ONLY valid JSON: {"subject": "...", "body": "..."}
"""


# ── compose functions ─────────────────────────────────────────────────────────

async def compose_reengagement_email_1(
    hiring_brief: dict,
    competitor_brief: dict,
    original_subject: str,
    trace_id: str = "",
) -> dict:
    """
    Compose re-engagement email 1 — "New data point", max 100 words body.

    Returns:
        {subject, body, word_count, honesty_flags}
    """
    t0 = time.monotonic()

    company = hiring_brief.get("company_name", hiring_brief.get("name", "your company"))
    industry = hiring_brief.get("industry", "your sector")
    job_titles = hiring_brief.get("job_postings", {}).get("role_titles", [])
    job_count = hiring_brief.get("job_postings", {}).get("open_roles_today", 0)
    competitors = (competitor_brief.get("competitors") or [])[:2]

    user_prompt = (
        f"Original email subject: {original_subject}\n"
        f"Company: {company}, Industry: {industry}\n"
        f"Current open roles: {job_count} — recent titles: {', '.join(job_titles[:5])}\n"
        f"Competitor signals: {json.dumps(competitors)}\n\n"
        "Write re-engagement email 1. Max 100 words body. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=400,
            messages=[
                {"role": "system", "content": _REENGAGE_1_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or f"Update on {industry} hiring signal")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 100)

    flags = []
    if truncated:
        flags.append("body_truncated_at_100_words")
    if not parsed:
        flags.append("llm_parse_failed")

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reengagement_composer.email_1",
        input={"company": company, "industry": industry},
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
        "honesty_flags": flags,
    }


async def compose_reengagement_email_2(
    contact_name: str,
    original_topic: str,
    trace_id: str = "",
) -> dict:
    """
    Compose re-engagement email 2 — "One specific question", max 50 words body.

    Returns:
        {subject, body, word_count, honesty_flags}
    """
    t0 = time.monotonic()
    first_name = contact_name.strip().split()[0] if contact_name.strip() else contact_name

    user_prompt = (
        f"Prospect first name: {first_name}\n"
        f"Original topic: {original_topic}\n\n"
        "Write re-engagement email 2. Max 50 words body. Single yes/no question. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=250,
            messages=[
                {"role": "system", "content": _REENGAGE_2_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or "One specific question")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 50)

    flags = []
    if truncated:
        flags.append("body_truncated_at_50_words")
    if not parsed:
        flags.append("llm_parse_failed")

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reengagement_composer.email_2",
        input={"contact_name": contact_name, "original_topic": original_topic},
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
        "honesty_flags": flags,
    }


async def compose_reengagement_email_3(
    contact_name: str,
    original_topic: str,
    trace_id: str = "",
) -> dict:
    """
    Compose re-engagement email 3 — 6-month gracious close, max 40 words body.

    Returns:
        {subject, body, word_count, honesty_flags}
    """
    t0 = time.monotonic()
    first_name = contact_name.strip().split()[0] if contact_name.strip() else contact_name

    user_prompt = (
        f"Prospect first name: {first_name}\n"
        f"Original topic: {original_topic}\n\n"
        "Write re-engagement email 3 (gracious close). Max 40 words body. "
        "Include a specific month+year for re-engagement. "
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )

    raw_text = ""
    parsed: dict = {}
    _usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL, temperature=_DEV_TEMP, max_tokens=200,
            messages=[
                {"role": "system", "content": _REENGAGE_3_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
        _usage = resp.usage
    except Exception:
        pass

    subject = str(parsed.get("subject") or "Parking this — Q3 check-in")
    body = str(parsed.get("body") or "")
    body, truncated = _enforce_word_limit(body, 40)

    flags = []
    if truncated:
        flags.append("body_truncated_at_40_words")
    if not parsed:
        flags.append("llm_parse_failed")

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reengagement_composer.email_3",
        input={"contact_name": contact_name, "original_topic": original_topic},
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
        "honesty_flags": flags,
    }
