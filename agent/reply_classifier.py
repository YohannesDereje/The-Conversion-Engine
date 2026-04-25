"""
Reply classifier (P3-F1) — classifies inbound prospect replies into 6 classes.

classify_reply: engaged, curious, hard_no, soft_defer, objection, ambiguous.
               Abstains to "ambiguous" when confidence < 0.70.
               Returns {class, confidence, objection_type, reasoning}.
"""
import json
import os
import pathlib
import re
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent.utils import emit_span

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_WARM_MD = (
    _ROOT / "tenacious_sales_data" / "seed" / "email_sequences" / "warm.md"
).read_text(encoding="utf-8")

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


_SYSTEM = f"""\
You are a reply classifier for Tenacious Consulting's B2B outreach system.

Classify the inbound prospect reply into exactly one of these 6 classes:
- engaged:     substantive response with a specific question or context about their situation
- curious:     "tell me more" / "what exactly do you do?" / general interest, no specifics
- hard_no:     "not interested" / "please remove" / "stop emailing" / any opt-out signal
- soft_defer:  "not right now" / "ask me in Q3" / "too busy" / timing objection only
- objection:   specific objection (price vs India vendors, existing vendor, POC-only budget)
- ambiguous:   cannot confidently classify — abstain, do not guess

Rules:
- Use "ambiguous" whenever you cannot assign a class with confidence >= 0.70
- For class "objection", also set objection_type: price | incumbent_vendor | poc_only | other
- For all other classes, set objection_type to null
- Never force a class — "ambiguous" is always the right abstention

Sales playbook context:
{_WARM_MD}

Return ONLY valid JSON — no markdown, no preamble:
{{
  "class": "<engaged|curious|hard_no|soft_defer|objection|ambiguous>",
  "confidence": <float 0.0-1.0>,
  "objection_type": "<price|incumbent_vendor|poc_only|other|null>",
  "reasoning": "<one sentence>"
}}
"""


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


async def classify_reply(
    reply_text: str,
    thread_context: str = "",
    trace_id: str = "",
) -> dict:
    """
    Classify an inbound prospect reply.

    Args:
        reply_text:      the prospect's reply text
        thread_context:  optional summary of prior thread for context
        trace_id:        Langfuse trace ID

    Returns:
        {class, confidence, objection_type, reasoning}
        Falls back to {class: "ambiguous", confidence: 0.0, ...} on any error.
    """
    t0 = time.monotonic()
    default = {
        "class": "ambiguous",
        "confidence": 0.0,
        "objection_type": None,
        "reasoning": "classification_error",
    }

    user_msg = f"Reply text:\n{reply_text}"
    if thread_context:
        user_msg = f"Thread context (earlier messages):\n{thread_context}\n\n{user_msg}"

    raw_text = ""
    parsed: dict = {}
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            temperature=_DEV_TEMP,
            max_tokens=300,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json(raw_text) or {}
    except Exception as exc:
        default["reasoning"] = str(exc)

    if parsed:
        reply_class = str(parsed.get("class", "ambiguous"))
        confidence = float(parsed.get("confidence", 0.0))
        if confidence < 0.70:
            reply_class = "ambiguous"
        obj_type = parsed.get("objection_type")
        result = {
            "class": reply_class,
            "confidence": confidence,
            "objection_type": None if (not obj_type or str(obj_type) == "null") else obj_type,
            "reasoning": str(parsed.get("reasoning", "")),
        }
    else:
        result = default

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="reply_classifier.classify_reply",
        input={"reply_text": reply_text[:300], "thread_context": thread_context[:200]},
        output=result,
        latency_ms=latency_ms,
    )
    return result
