"""
Tone-preservation probe (P3-E1).

score_tone() scores a draft email against the 5 Tenacious tone markers using Qwen3
via OpenRouter. A draft that scores below 4/5 is flagged — the send is NOT blocked,
but the caller receives a violations list and a tone_violation honesty flag.

Markers: Direct, Grounded, Honest, Professional, Non-condescending.
Pass threshold: total >= 4 out of 5.
"""
import json
import os
import pathlib
import re
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_STYLE_GUIDE = (
    _ROOT / "tenacious_sales_data" / "seed" / "style_guide.md"
).read_text(encoding="utf-8")

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


_SYSTEM_PROMPT = f"""\
You are a tone-compliance auditor for Tenacious Consulting outreach emails.

Your job is to score a draft email against the 5 Tenacious tone markers defined below.
Score each marker 1 (pass) or 0 (fail). For each failure, write exactly one sentence
explaining what rule was violated. For each pass, reason may be an empty string.

=== TENACIOUS STYLE GUIDE ===
{_STYLE_GUIDE}

=== SCORING RUBRIC ===

1. DIRECT (1 = pass, 0 = fail)
   FAIL if: filler words ("just", "quick", "hope this finds you well"), vague promises,
   excessive pleasantries, subject starts with "Quick" or "Hey" or "Just".

2. GROUNDED (1 = pass, 0 = fail)
   FAIL if: email asserts hiring patterns, growth trajectories, or AI investment without
   grounding them in a specific, verifiable fact. "Clearly scaling aggressively" with no
   data = fail. "You have 4 open Python roles since January" = pass.

3. HONEST (1 = pass, 0 = fail)
   FAIL if: email claims "aggressive hiring" when fewer than 5 roles are evidenced, or
   over-commits bench capacity, or fabricates peer-company practices, or asserts
   AI maturity when signal is absent. Confident claims with no stated data source = fail.

4. PROFESSIONAL (1 = pass, 0 = fail)
   FAIL if: email contains offshore-vendor clichés ("top talent", "world-class",
   "A-players", "rockstar", "ninja", "cost savings of X%"), uses the word "bench" in
   prospect-facing text, or uses language inappropriate for a CTO or VP Engineering.

5. NON-CONDESCENDING (1 = pass, 0 = fail)
   FAIL if: email implies the prospect is behind, failing, or incompetent. Framing a
   competitor gap as a judgment ("you're missing a critical capability") = fail.
   Framing it as a research question ("curious whether this is deliberate") = pass.

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no markdown, no preamble:
{{
  "direct":           {{"score": <0 or 1>, "reason": "<one sentence or empty>"}},
  "grounded":         {{"score": <0 or 1>, "reason": "<one sentence or empty>"}},
  "honest":           {{"score": <0 or 1>, "reason": "<one sentence or empty>"}},
  "professional":     {{"score": <0 or 1>, "reason": "<one sentence or empty>"}},
  "non_condescending":{{"score": <0 or 1>, "reason": "<one sentence or empty>"}}
}}
"""


async def score_tone(
    email_subject: str,
    email_body: str,
    trace_id: str = "",
) -> dict:
    """
    Score a draft email against the 5 Tenacious tone markers.

    Args:
        email_subject: the subject line of the email
        email_body:    the body text (without the Cal.com block)
        trace_id:      Langfuse trace ID for span attribution

    Returns:
        {
            "scores":    {"direct": int, "grounded": int, "honest": int,
                          "professional": int, "non_condescending": int},
            "total":     int (0-5),
            "passed":    bool (total >= 4),
            "violations": [list of failed marker names],
            "failure_reasons": {marker: reason_str for each failed marker},
        }
    """
    default = _default_result()
    t0 = time.monotonic()

    user_prompt = (
        f"Subject: {email_subject}\n\n"
        f"Body:\n{email_body}\n\n"
        "Score each of the 5 tone markers and return ONLY the JSON object."
    )

    raw_text = ""
    parsed: dict = {}
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=_DEV_TEMP,
            max_tokens=600,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
    except Exception:
        pass

    result = _build_result(parsed) if parsed else default
    latency_ms = (time.monotonic() - t0) * 1000

    # Emit Langfuse span
    if trace_id:
        try:
            from agent.utils import emit_span
            emit_span(
                trace_id=trace_id,
                name="tone_probe.score_tone",
                input={"subject": email_subject[:100], "body_chars": len(email_body)},
                output={
                    "total": result["total"],
                    "passed": result["passed"],
                    "violations": result["violations"],
                },
                latency_ms=latency_ms,
            )
        except Exception:
            pass

    return result


# ── helpers ───────────────────────────────────────────────────────────────────

_MARKERS = ("direct", "grounded", "honest", "professional", "non_condescending")


def _build_result(parsed: dict) -> dict:
    scores: dict = {}
    failure_reasons: dict = {}

    for marker in _MARKERS:
        entry = parsed.get(marker, {})
        if isinstance(entry, dict):
            raw_score = entry.get("score", 1)
        else:
            raw_score = 1
        try:
            score = 1 if int(raw_score) >= 1 else 0
        except (ValueError, TypeError):
            score = 1
        scores[marker] = score
        if score == 0:
            reason = ""
            if isinstance(entry, dict):
                reason = str(entry.get("reason", "")).strip()
            failure_reasons[marker] = reason

    total = sum(scores.values())
    violations = [m for m in _MARKERS if scores[m] == 0]

    return {
        "scores": scores,
        "total": total,
        "passed": total >= 4,
        "violations": violations,
        "failure_reasons": failure_reasons,
    }


def _default_result() -> dict:
    return {
        "scores": {m: 1 for m in _MARKERS},
        "total": 5,
        "passed": True,
        "violations": [],
        "failure_reasons": {},
    }


def _parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                data = json.loads(m.group())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    return None
