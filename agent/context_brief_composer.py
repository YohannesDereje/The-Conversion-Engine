"""
Discovery call context brief composer (P3-F4).

compose_discovery_call_brief: produces a Markdown brief filling all 10 template sections
                               from schemas/discovery_call_context_brief.md.
                               Attached to Cal.com calendar invites for human delivery leads.
"""
import json
import os
import pathlib
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent.utils import LANGFUSE_HOST, emit_span

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_BRIEF_TEMPLATE = (
    _ROOT / "tenacious_sales_data" / "schemas" / "discovery_call_context_brief.md"
).read_text(encoding="utf-8")
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


_SYSTEM = f"""\
You produce Tenacious Consulting discovery-call context briefs for human delivery leads.

The brief follows the template below. Fill every section — never skip one.
If data is unavailable for a field, write "Unknown" or "Not available" honestly.
Do not hallucinate data.

Quality rules:
- Section 3 (competitor gap): only cite findings you have explicit evidence for.
  Mark low-confidence findings as such.
- Section 4 (bench match): use only the actual available_engineers counts provided.
  Never over-commit capacity.
- Section 6 (objections): if no objections were raised, say so explicitly.
- Section 9 (what NOT to do): always include at least one concrete entry.
- Section 10 (confidence): be honest — "high confidence on everything" is implausible.
- Length: at most one laptop screen scroll. Sharp synthesis beats exhaustive coverage.

=== TEMPLATE ===
{_BRIEF_TEMPLATE}

Return the completed Markdown document. Replace every {{{{ }}}} placeholder.
Do NOT include the template instructions section — only the filled brief.
"""


async def compose_discovery_call_brief(
    prospect_name: str,
    prospect_title: str,
    prospect_company: str,
    call_datetime_utc: str,
    call_duration_minutes: int,
    tenacious_lead_name: str,
    original_subject: str,
    thread_start_date: str,
    langfuse_trace_id: str,
    hiring_signal_brief: dict,
    competitor_gap_brief: dict,
    conversation_history: list[str],
    trace_id: str = "",
) -> str:
    """
    Compose a discovery-call context brief for the human delivery lead.

    Args:
        prospect_name:           full name (e.g., "Marcus Lee")
        prospect_title:          job title (e.g., "VP Engineering")
        prospect_company:        company name
        call_datetime_utc:       ISO 8601 (e.g., "2026-05-02T14:00:00Z")
        call_duration_minutes:   booked call length (15 or 30)
        tenacious_lead_name:     human delivery lead assigned (e.g., "Arun")
        original_subject:        Email 1 subject line
        thread_start_date:       date of Email 1 (ISO date string)
        langfuse_trace_id:       trace ID for the Langfuse thread link
        hiring_signal_brief:     dict from enrichment pipeline
        competitor_gap_brief:    dict from enrichment pipeline
        conversation_history:    list of reply texts in order (alternating prospect/agent)
        trace_id:                Langfuse trace ID for this LLM call

    Returns:
        Markdown string — the completed context brief.
        Returns a minimal fallback brief on LLM error.
    """
    t0 = time.monotonic()

    bench_avail = {k: v.get("available_engineers", 0) for k, v in _BENCH.get("stacks", {}).items()}
    langfuse_url = (
        f"{LANGFUSE_HOST}/traces/{langfuse_trace_id}"
        if langfuse_trace_id
        else "not available"
    )

    history_block = ""
    for i, msg in enumerate(conversation_history, 1):
        history_block += f"\n[{i}] {msg[:400]}"

    user_prompt = (
        f"Fill in the context brief for this prospect.\n\n"
        f"Prospect: {prospect_name} — {prospect_title} at {prospect_company}\n"
        f"Call: {call_datetime_utc} ({call_duration_minutes} minutes)\n"
        f"Tenacious lead: {tenacious_lead_name}\n"
        f"Thread: started {thread_start_date} | Subject: \"{original_subject}\"\n"
        f"Langfuse trace: {langfuse_url}\n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"=== HIRING SIGNAL BRIEF ===\n{json.dumps(hiring_signal_brief, indent=2)}\n\n"
        f"=== COMPETITOR GAP BRIEF ===\n{json.dumps(competitor_gap_brief, indent=2)}\n\n"
        f"=== BENCH AVAILABILITY ===\n{json.dumps(bench_avail)}\n\n"
        f"=== CONVERSATION HISTORY ===\n"
        f"{history_block or '(no reply history — first contact)'}\n\n"
        "Return ONLY the completed Markdown brief. No preamble, no explanation."
    )

    raw_text = ""
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            temperature=_DEV_TEMP,
            max_tokens=2500,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        # Strip Qwen3 thinking block
        if "<think>" in raw_text and "</think>" in raw_text:
            raw_text = raw_text[raw_text.rfind("</think>") + len("</think>"):].strip()
    except Exception as exc:
        raw_text = _fallback_brief(prospect_name, prospect_company, call_datetime_utc, str(exc))

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="context_brief_composer.compose_discovery_call_brief",
        input={
            "prospect_name": prospect_name,
            "prospect_company": prospect_company,
            "call_datetime_utc": call_datetime_utc,
        },
        output={"brief_chars": len(raw_text), "status": "ok" if raw_text else "error"},
        latency_ms=latency_ms,
    )
    return raw_text


def _fallback_brief(
    prospect_name: str,
    prospect_company: str,
    call_datetime_utc: str,
    error_note: str,
) -> str:
    return (
        f"# Discovery Call Context Brief\n\n"
        f"**Prospect:** {prospect_name} at {prospect_company}\n"
        f"**Scheduled:** {call_datetime_utc}\n\n"
        f"*Brief generation failed: {error_note}*\n\n"
        "Please review the Langfuse trace and HubSpot contact record directly before the call."
    )
