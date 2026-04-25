"""
Main LLM agent — composes personalized outreach from enrichment briefs.

Business rules enforced in Python after LLM call (never delegated to the model):
  - Email body word limit: 120 words max (truncated if exceeded, flag added)
  - Segment 4 requires ai_maturity.score >= 2
  - segment_confidence < 0.6 -> override to abstain
  - Bench check: required_skills vs bench_summary.json availability (0 = cannot commit)
  - Signal-confidence-aware phrasing (P5-A mechanism, toggle: MECHANISM_SIGNAL_AWARE_PHRASING):
      When weak_hiring_velocity_signal or weak_ai_maturity_signal flags are set, scans the
      composed email body for assertive claims. If found, triggers up to 2 regeneration
      attempts with an explicit ask-language override injected into the conversation.
"""
import json
import os
import pathlib
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_SEED = _ROOT / "tenacious_sales_data" / "seed"

_OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-235b-a22b")
_DEV_TEMP = float(os.getenv("DEV_MODEL_TEMPERATURE", "0.0"))

# ── P5-A mechanism: signal-confidence-aware phrasing ─────────────────────────
# Toggle OFF for ablation baseline: MECHANISM_SIGNAL_AWARE_PHRASING=false
_MECHANISM_ENABLED = os.getenv("MECHANISM_SIGNAL_AWARE_PHRASING", "true").lower() == "true"

# Flags that activate the post-generation scan
_WEAK_SIGNAL_FLAGS = frozenset({"weak_hiring_velocity_signal", "weak_ai_maturity_signal"})

# Assertive hiring/growth claim patterns the scan catches
_ASSERTIVE_CLAIM_RE = re.compile(
    r"\b(aggressively|rapidly|accelerat\w+|scaling up|expanding)\s+(hir\w+|grow\w+|team)\b"
    r"|\b(strong|impressive|significant|robust|aggressive)\s+(hiring|growth|momentum|velocity|expansion)\b"
    r"|\byou('re| are)\s+(grow\w+|hir\w+|scal\w+|expand\w+)\b"
    r"|\byour\s+(hiring|team|engineering|headcount)\s+(is|has been|has)\s+(grow\w+|scal\w+|expand\w+)\b"
    r"|\brapid(ly)?\s+(grow\w+|hir\w+|scal\w+|expand\w+)\b"
    r"|\bwith\s+(your|the)\s+(growing|expanding|scaling)\s+(team|engineering|workforce)\b"
    r"|\b(you('re| are)|your\s+company\s+is)\s+.{0,30}(grow\w+|hir\w+)\b",
    re.IGNORECASE,
)


def _has_assertive_claims(text: str) -> bool:
    return bool(_ASSERTIVE_CLAIM_RE.search(text))


_oai_client: AsyncOpenAI | None = None
_langfuse_client = None
_cached_system_prompt: str | None = None


def _get_client() -> AsyncOpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = AsyncOpenAI(api_key=_OPENROUTER_API_KEY, base_url=_OPENROUTER_BASE_URL)
    return _oai_client


def _get_langfuse():
    global _langfuse_client
    if _langfuse_client is None:
        try:
            from langfuse import Langfuse
            _langfuse_client = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception:
            _langfuse_client = _NoopLangfuse()
    return _langfuse_client


class _NoopLangfuse:
    def create_trace_id(self) -> str:
        return "noop-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    def flush(self): pass
    def start_observation(self, **_): return _NoopObs()


class _NoopObs:
    trace_id: str = "noop"
    def update(self, **_): pass
    def end(self): pass


# ── system prompt builder (E4) ────────────────────────────────────────────────

def _build_system_prompt() -> str:
    """
    Build the comprehensive system prompt from seed files.
    Reads: icp_definition.md, style_guide.md, bench_summary.json, cold.md.
    Cached after first call.
    """
    global _cached_system_prompt
    if _cached_system_prompt is not None:
        return _cached_system_prompt

    icp_text = (_SEED / "icp_definition.md").read_text(encoding="utf-8")
    style_text = (_SEED / "style_guide.md").read_text(encoding="utf-8")
    bench_json = (_SEED / "bench_summary.json").read_text(encoding="utf-8")
    cold_text = (_SEED / "email_sequences" / "cold.md").read_text(encoding="utf-8")

    _cached_system_prompt = f"""\
ROLE
====
You are a senior business development strategist at Tenacious Consulting and Outsourcing.
Your job is to craft precise, evidence-grounded cold outreach emails to engineering leaders
(CTOs, VPs of Engineering, Heads of AI/Data) at companies that are likely buyers of
offshore and nearshore engineering and AI/ML talent.

You never invent facts. You never commit to capacity the bench does not show. You always
prefer honest uncertainty over confident fabrication. Your emails are short, direct, and
grounded in specific evidence from the enrichment brief provided.

TONE AND STYLE
==============
{style_text}

ICP SEGMENT DEFINITIONS
=======================
{icp_text}

AVAILABLE ENGINEERING CAPACITY
================================
Here is our current engineering capacity. You must not propose skills we do not have.
Do NOT use the word "bench" in prospect-facing text — say "engineering team",
"available capacity", or "engineers ready to deploy" instead.

{bench_json}

HONESTY CONSTRAINTS
===================
1. If hiring_velocity.velocity_label is "insufficient_signal", you MUST use tentative
   language such as "I was wondering if..." or "Have you considered..." — do NOT assert
   hiring patterns or growth trajectories you cannot verify.
2. If ai_maturity.confidence is below 0.5 or score is 0, frame any AI-capability angle
   as a question, never as a confirmed finding.
3. You must never propose engineering capacity for skills that show 0 available_engineers
   in the capacity summary above.
4. If a layoff event is present in the brief, acknowledge that cost discipline is likely
   in scope — do not pretend the layoff did not happen and pitch headcount expansion.
5. For Segment 4 pitches, you may only proceed if the prospect's ai_maturity.score >= 2.
   If it is below 2, set icp_segment_id to "abstain" and write a generic exploratory email.

EMAIL TEMPLATE STRUCTURE (E3 — segment routing)
================================================
The email you write must follow the structure and per-segment rules below exactly.
Do NOT reproduce these templates verbatim — generate fresh, signal-grounded content
in this structure for every prospect.

{cold_text}

OUTPUT FORMAT
=============
Respond with ONLY a JSON object (no markdown, no preamble, no trailing commentary):
{{
  "icp_segment_id": <integer 1-4 or the string "abstain">,
  "required_skills": ["<skill1>", "<skill2>"],
  "email_body": "<subject line on first line, blank line, then email body — max 120 words in body>",
  "confidence": <float 0.0-1.0 reflecting your certainty in the segment and angle chosen>
}}
"""
    return _cached_system_prompt


# ── bench helpers (E2) ────────────────────────────────────────────────────────

_SKILL_TO_BENCH_KEY = {
    "python": "python",
    "django": "python",
    "fastapi": "python",
    "flask": "python",
    "celery": "python",
    "go": "go",
    "golang": "go",
    "grpc": "go",
    "data": "data",
    "dbt": "data",
    "snowflake": "data",
    "databricks": "data",
    "airflow": "data",
    "fivetran": "data",
    "sql": "data",
    "ml": "ml",
    "machine learning": "ml",
    "ai": "ml",
    "llm": "ml",
    "mlops": "ml",
    "pytorch": "ml",
    "tensorflow": "ml",
    "langchain": "ml",
    "rag": "ml",
    "hugging face": "ml",
    "infra": "infra",
    "infrastructure": "infra",
    "devops": "infra",
    "terraform": "infra",
    "kubernetes": "infra",
    "k8s": "infra",
    "aws": "infra",
    "gcp": "infra",
    "azure": "infra",
    "docker": "infra",
    "frontend": "frontend",
    "react": "frontend",
    "typescript": "frontend",
    "next.js": "frontend",
    "nextjs": "frontend",
    "vue": "frontend",
    "angular": "frontend",
    "tailwind": "frontend",
    "nestjs": "fullstack_nestjs",
    "nest": "fullstack_nestjs",
    "node": "fullstack_nestjs",
    "nodejs": "fullstack_nestjs",
}


def _check_bench(required_skills: list) -> dict:
    """
    Check required_skills against bench_summary.json availability.
    Returns {"match": bool, "missing_skills": list}.
    A skill is missing when its bench_key shows 0 available_engineers.
    """
    bench = json.loads((_SEED / "bench_summary.json").read_text(encoding="utf-8"))
    stacks = bench.get("stacks", {})
    missing = []
    checked: set[str] = set()

    for skill in required_skills:
        s = skill.lower()
        bench_key = next((v for k, v in _SKILL_TO_BENCH_KEY.items() if k in s), None)
        if bench_key and bench_key not in checked:
            checked.add(bench_key)
            available = stacks.get(bench_key, {}).get("available_engineers", 0)
            if available == 0:
                missing.append(skill)

    return {"match": len(missing) == 0, "missing_skills": missing}


# ── user prompt builder ───────────────────────────────────────────────────────

_SEGMENT_ANGLE_HINTS = {
    1: "funding-angle: fresh capital, need to scale engineering faster than in-house hiring supports",
    2: "restructuring-angle: preserve delivery capacity while reshaping cost structure post-layoff",
    3: "leadership-transition-angle: new CTO/VP Eng reassessing vendor mix in first 90 days",
    4: "capability-gap-angle: specific AI/ML gap vs. top-quartile sector peers (cite evidence)",
}


def _build_user_prompt(hsb: dict, cgb: dict) -> str:
    segment_raw = hsb.get("primary_segment_match", "abstain")
    seg_num = next((n for n in (1, 2, 3, 4) if f"segment_{n}" in str(segment_raw)), None)
    angle = _SEGMENT_ANGLE_HINTS.get(seg_num, "generic exploratory — ask an open question")

    velocity_label = hsb.get("hiring_velocity", {}).get("velocity_label", "insufficient_signal")
    ai_score = int(hsb.get("ai_maturity", {}).get("score", 0) or 0)
    seg_confidence = float(hsb.get("segment_confidence", 0.0) or 0.0)
    layoff = hsb.get("buying_window_signals", {}).get("layoff_event", {})
    honesty_flags = hsb.get("honesty_flags", [])

    # Build constraint hints to surface explicitly in the user turn
    hints = []
    if "weak_hiring_velocity_signal" in honesty_flags or velocity_label == "insufficient_signal":
        hints.append(
            "CONSTRAINT: Hiring velocity signal is weak (insufficient_signal). "
            "Use tentative language. Do NOT assert hiring patterns."
        )
    if "weak_ai_maturity_signal" in honesty_flags or ai_score == 0:
        hints.append(
            "CONSTRAINT: AI maturity signal is weak or absent. "
            "Frame any AI angle as a question, not a confirmed finding."
        )
    if layoff.get("detected"):
        hints.append(
            f"CONTEXT: Layoff event detected "
            f"({layoff.get('date', 'unknown date')}, "
            f"~{layoff.get('percentage_cut', '?')}% reduction, "
            f"{layoff.get('headcount_reduction', '?')} people). "
            "Acknowledge cost discipline — do not pitch headcount expansion."
        )
    if seg_confidence < 0.6:
        hints.append(
            f"CONSTRAINT: segment_confidence is {seg_confidence:.2f} (below 0.6 threshold). "
            "Recommend icp_segment_id = 'abstain' and write a generic exploratory email."
        )

    hint_block = "\n".join(hints) if hints else "(no active constraints beyond system prompt)"

    # Slim down the CGP to avoid token waste — include only what's useful for email composition
    cgp_summary = {k: cgb.get(k) for k in (
        "prospect_sector",
        "prospect_ai_maturity_score",
        "sector_top_quartile_benchmark",
        "gap_findings",
        "suggested_pitch_shift",
    )}

    return f"""\
TASK: Compose a cold outreach email using the enrichment briefs below.

RECOMMENDED ANGLE: {angle}

ACTIVE CONSTRAINTS:
{hint_block}

=== HIRING SIGNAL BRIEF ===
{json.dumps(hsb, indent=2)}

=== COMPETITOR GAP BRIEF (summary) ===
{json.dumps(cgp_summary, indent=2)}

INSTRUCTIONS:
1. Select icp_segment_id (1-4 or "abstain") — use primary_segment_match as a strong hint,
   but apply the ICP classification rules from your system prompt.
2. List required_skills: the engineering skills Tenacious would need for this engagement.
3. Write email_body: subject line first, then body (max 120 words). Address the engineering
   leader generically — we do not have a contact name.
   NOTE: Cal.com booking slot links will be appended automatically after your email body.
   In Sentence 4 write the timing ask (e.g., "Worth 15 minutes Tuesday or Wednesday?")
   but do NOT include a [Cal link] placeholder — the actual links are added programmatically.
4. Report confidence (0.0-1.0) in your segment and angle choice.

Return ONLY the JSON object. No markdown. No explanation.\
"""


# ── main function (E1) ────────────────────────────────────────────────────────

async def compose_outreach(
    hiring_signal_brief: dict,
    competitor_gap_brief: dict,
    conversation_history: list | None = None,
) -> dict:
    """
    Compose a personalized outreach email from enrichment briefs.

    Args:
        hiring_signal_brief:  validated dict from pipeline.run_enrichment_pipeline
        competitor_gap_brief: validated dict from pipeline.run_enrichment_pipeline
        conversation_history: optional list of {"role": ..., "content": ...} prior turns

    Returns:
        {
            "email_to_send":       str,
            "icp_segment":         int (1-4) | "abstain",
            "llm_confidence":      float,
            "bench_match_result":  {"match": bool, "missing_skills": list},
            "decision_override":   bool,
            "langfuse_trace_id":   str,
        }
    """
    # P3-D1: fetch Cal.com slots before LLM call so they are ready to append
    cal_slots: list = []
    try:
        from agent.calcom_handler import get_available_slots as _get_cal_slots
        cal_slots = await _get_cal_slots(days_ahead=7)
    except Exception:
        cal_slots = []

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(hiring_signal_brief, competitor_gap_brief)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_prompt})

    # ── Langfuse trace setup ──────────────────────────────────────────────────
    lf = _get_langfuse()
    trace_id: str = lf.create_trace_id()
    tc = _make_trace_ctx(lf, trace_id)

    raw_text = ""
    usage = None
    parsed: dict = {}
    llm_error: str | None = None

    # ── LLM call ──────────────────────────────────────────────────────────────
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=1500,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        parsed = _parse_json(raw_text) or {}
    except Exception as exc:
        llm_error = str(exc)

    # Emit Langfuse generation span
    _emit_generation(lf, tc, messages, raw_text, usage)
    lf.flush()

    # ── Extract LLM outputs ───────────────────────────────────────────────────
    email_body: str = str(parsed.get("email_body") or "")
    icp_raw = parsed.get("icp_segment_id", "abstain")
    required_skills: list = list(parsed.get("required_skills") or [])
    llm_confidence: float = max(0.0, min(1.0, float(parsed.get("confidence") or 0.5)))

    icp_segment = _normalise_segment(icp_raw)
    active_flags: list[str] = list(hiring_signal_brief.get("honesty_flags") or [])

    # ── Enforce business rules in Python (E2) — NOT delegated to LLM ─────────
    decision_override = False

    # Rule 0: 120-word body limit — hard Python enforcement (policy requirement)
    email_body, body_truncated = _enforce_word_limit(email_body, max_body_words=120)
    if body_truncated:
        active_flags.append("email_body_truncated_at_120_words")

    # Rule 1: segment_confidence < 0.6 → abstain (honesty constraint)
    seg_confidence = float(hiring_signal_brief.get("segment_confidence") or 0.0)
    if seg_confidence < 0.6 and icp_segment != "abstain":
        icp_segment = "abstain"
        decision_override = True

    # Rule 2: Segment 4 gate — requires ai_maturity.score >= 2 (ICP definition rule)
    if icp_segment == 4:
        ai_score = int((hiring_signal_brief.get("ai_maturity") or {}).get("score") or 0)
        if ai_score < 2:
            icp_segment = "abstain"
            decision_override = True

    # Rule 3: Bench availability — two sources of truth, both must pass
    # 3a: Pipeline-computed check from the HSB (inferred tech stack vs bench_summary.json)
    hsb_bench = hiring_signal_brief.get("bench_to_brief_match") or {}
    hsb_bench_ok = bool(hsb_bench.get("bench_available", True))
    hsb_gaps = list(hsb_bench.get("gaps") or [])

    # 3b: LLM's required_skills check (what the model says it needs to deliver)
    llm_bench = _check_bench(required_skills)

    all_missing = sorted(set(hsb_gaps + llm_bench.get("missing_skills", [])))
    bench_match_result = {
        "match": hsb_bench_ok and llm_bench["match"],
        "missing_skills": all_missing,
    }
    if not bench_match_result["match"]:
        decision_override = True  # route to human — do NOT commit unavailable capacity

    # Rule 4: LLM failure fallback
    if llm_error or not email_body:
        email_body = _generic_fallback_email(hiring_signal_brief)
        icp_segment = "abstain"
        decision_override = True

    # Rule 5 (P5-A): Signal-confidence-aware phrasing mechanism
    # Fires when: mechanism is ON, weak signal flags are present, AND assertive claims detected.
    # Triggers up to 2 regeneration passes with an explicit ask-language override.
    # Toggle OFF for ablation: set MECHANISM_SIGNAL_AWARE_PHRASING=false in .env
    if _MECHANISM_ENABLED and _WEAK_SIGNAL_FLAGS.intersection(active_flags):
        if _has_assertive_claims(email_body):
            active_flags.append("mechanism_signal_aware_phrasing_triggered")
            weak_flags_present = sorted(_WEAK_SIGNAL_FLAGS.intersection(active_flags))
            _regen_success = False
            for _attempt in range(2):
                regen_hint = (
                    "CRITICAL OVERRIDE — honesty flags active: "
                    + ", ".join(weak_flags_present) + ".\n"
                    "The email_body you generated contains assertive hiring or growth claims "
                    "that the enrichment data does NOT support. You MUST rewrite the "
                    "email_body field using ask language only — replace every assertive "
                    "statement with a specific, grounded question.\n"
                    "Swap example: 'you are aggressively expanding your ML team' "
                    "→ 'I noticed 2 open ML roles — is velocity a constraint on your roadmap?'\n"
                    "Return the same JSON structure with only email_body corrected."
                )
                regen_messages = messages + [
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content": regen_hint},
                ]
                try:
                    regen_resp = await _get_client().chat.completions.create(
                        model=_DEV_MODEL,
                        messages=regen_messages,
                        temperature=_DEV_TEMP,
                        max_tokens=1500,
                    )
                    regen_raw = (regen_resp.choices[0].message.content or "").strip()
                    regen_parsed = _parse_json(regen_raw) or {}
                    regen_body = str(regen_parsed.get("email_body") or "")
                    if regen_body and not _has_assertive_claims(regen_body):
                        email_body, _ = _enforce_word_limit(regen_body, max_body_words=120)
                        _regen_success = True
                        break
                except Exception:
                    pass
            if not _regen_success:
                active_flags.append("assertive_claim_regen_failed")

    # P3-D1: capture body before cal block for tone scoring (cal links are not email content)
    email_body_for_tone = email_body

    # P3-D1: append Cal.com booking block AFTER 120-word body (not counted in limit)
    from agent.utils import CALCOM_BASE_URL as _CALCOM_BASE_URL
    email_body = email_body + _format_cal_block(cal_slots, _CALCOM_BASE_URL)

    # P3-E2: score tone on the email content (subject + body, without cal block)
    tone_probe_result: dict | None = None
    try:
        from agent.tone_probe import score_tone
        subj_for_tone, body_for_tone = _split_subject_body(email_body_for_tone)
        tone_probe_result = await score_tone(subj_for_tone, body_for_tone, trace_id)
        if not tone_probe_result.get("passed", True):
            active_flags.append("tone_violation")
    except Exception:
        pass

    return {
        "email_to_send": email_body,
        "icp_segment": icp_segment,
        "llm_confidence": round(llm_confidence, 4),
        "bench_match_result": bench_match_result,
        "decision_override": decision_override,
        "honesty_flags": active_flags,
        "tone_probe_result": tone_probe_result,
        "langfuse_trace_id": trace_id,
    }


# ── Email 2 — research-finding follow-up (P3-C1) ─────────────────────────────

_FOLLOWUP2_SYSTEM = """\
You write Email 2 in a 3-email cold outreach sequence for Tenacious Consulting.

PURPOSE: Introduce ONE specific competitor-gap finding. The new data IS the reason \
to write — this is not a reminder or a bump.

RULES:
- Subject: "One more data point: [specific peer-company signal]" — under 60 characters
- Body: max 100 words (enforced in Python after this call — do not exceed it)
- Open with a one-line intro. No "just following up", "circling back", or guilt language.
- Name a specific competitor from the gap findings where public data allows.
- End with a soft question about the pattern — NOT a product pitch.
- Signature: Research Partner, Tenacious Intelligence Corporation, gettenacious.com
- No emojis. No fake urgency. No social proof dumps.
- Tone markers: Direct, Grounded, Honest, Professional, Non-condescending.

Return ONLY valid JSON: {"subject": "...", "body": "..."}
"""


def _build_followup2_prompt(
    hsb: dict,
    cgb: dict,
    original_subject: str,
) -> str:
    gap_findings = cgb.get("gap_findings") or []
    top_gap = gap_findings[0] if gap_findings else {}
    benchmark = cgb.get("sector_top_quartile_benchmark") or "sector peers"
    sector = cgb.get("prospect_sector") or hsb.get("ai_maturity", {})

    gap_block = (
        f"Gap finding: {json.dumps(top_gap)}"
        if top_gap
        else f"No specific gap data available — use sector benchmark: {benchmark}"
    )

    return (
        f"Original Email 1 subject: {original_subject}\n\n"
        f"Prospect: {hsb.get('prospect_name', 'the company')} "
        f"(segment: {hsb.get('primary_segment_match', 'unknown')})\n\n"
        f"{gap_block}\n\n"
        f"Sector benchmark: {benchmark}\n\n"
        "Compose Email 2. Return ONLY JSON: {\"subject\": \"...\", \"body\": \"...\"}\n"
        "Subject under 60 chars. Body under 100 words."
    )


async def compose_followup_email_2(
    hiring_signal_brief: dict,
    competitor_gap_brief: dict,
    original_subject: str,
    trace_id: str = "",
) -> dict:
    """
    Compose Email 2 — day-5 research-finding follow-up.

    Grounded in one specific competitor-gap finding from competitor_gap_brief.
    Body is Python-enforced to ≤ 100 words.

    Returns:
        {subject, body, email_type, word_count, honesty_flags, tone_probe_result}
    """
    messages = [
        {"role": "system", "content": _FOLLOWUP2_SYSTEM},
        {"role": "user", "content": _build_followup2_prompt(
            hiring_signal_brief, competitor_gap_brief, original_subject
        )},
    ]

    lf = _get_langfuse()
    tc = _make_trace_ctx(lf, trace_id) if trace_id else _make_trace_ctx(lf, lf.create_trace_id())

    raw_text = ""
    usage = None
    parsed: dict = {}

    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=600,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        parsed = _parse_json(raw_text) or {}
    except Exception:
        pass

    _emit_generation(lf, tc, messages, raw_text, usage)

    subject = str(parsed.get("subject") or "One more data point from our research")
    body = str(parsed.get("body") or "")

    # Python-enforced 60-char subject limit and 100-word body limit
    subject = subject[:60]
    body, truncated = _enforce_word_limit(body, max_body_words=100)

    flags = []
    if truncated:
        flags.append("email_body_truncated_at_100_words")
    if not parsed:
        flags.append("llm_parse_failed_used_fallback")

    # P3-E2: tone probe
    tone_probe_result: dict | None = None
    try:
        from agent.tone_probe import score_tone
        tone_probe_result = await score_tone(subject, body, trace_id)
        if not tone_probe_result.get("passed", True):
            flags.append("tone_violation")
    except Exception:
        pass

    return {
        "subject": subject,
        "body": body,
        "email_type": "followup_2",
        "word_count": len(body.split()),
        "honesty_flags": flags,
        "tone_probe_result": tone_probe_result,
    }


# ── Email 3 — gracious close (P3-C2) ─────────────────────────────────────────

_CLOSING3_SYSTEM = """\
You write Email 3 — the gracious close — in a cold outreach sequence for Tenacious Consulting.

PURPOSE: Close the thread with dignity. Leave the door open without pressure.

RULES:
- Subject: "Closing the loop on [original topic]" — under 60 characters
- Body: max 70 words (enforced in Python after this call — do not exceed it)
- Sentence 1: acknowledge the thread is likely not a fit right now (no guilt, no apology)
- Sentence 2: one non-pushy invitation — offer sector data, or name a specific quarter \
  for a check-in (e.g., "Q3 2026") — NOT "sometime in the future"
- No urgency, no "hope this finds you well", no "circling back"
- Signature: Research Partner, Tenacious Intelligence Corporation, gettenacious.com
- No emojis.
- Tone markers: Direct, Grounded, Honest, Professional, Non-condescending.

Return ONLY valid JSON: {"subject": "...", "body": "..."}
"""


def _build_closing3_prompt(
    hsb: dict,
    original_subject: str,
    contact_name: str,
) -> str:
    name = contact_name.split()[0] if contact_name else ""
    topic = re.sub(r"^(Context:|Note on|Congrats on|Question on|Closing the loop on)\s*",
                   "", original_subject, flags=re.IGNORECASE).strip() or "our research note"
    return (
        f"Contact first name: {name or 'not known'}\n"
        f"Original topic: {topic}\n"
        f"Prospect: {hsb.get('prospect_name', 'the company')}\n\n"
        "Compose Email 3 (gracious close). "
        "Return ONLY JSON: {\"subject\": \"...\", \"body\": \"...\"}\n"
        "Subject under 60 chars. Body under 70 words."
    )


async def compose_closing_email_3(
    hiring_signal_brief: dict,
    original_subject: str,
    contact_name: str,
    trace_id: str = "",
) -> dict:
    """
    Compose Email 3 — day-12 gracious close.

    Body is Python-enforced to ≤ 70 words.

    Returns:
        {subject, body, email_type, word_count, honesty_flags, tone_probe_result}
    """
    messages = [
        {"role": "system", "content": _CLOSING3_SYSTEM},
        {"role": "user", "content": _build_closing3_prompt(
            hiring_signal_brief, original_subject, contact_name
        )},
    ]

    lf = _get_langfuse()
    tc = _make_trace_ctx(lf, trace_id) if trace_id else _make_trace_ctx(lf, lf.create_trace_id())

    raw_text = ""
    usage = None
    parsed: dict = {}

    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=400,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        parsed = _parse_json(raw_text) or {}
    except Exception:
        pass

    _emit_generation(lf, tc, messages, raw_text, usage)

    subject = str(parsed.get("subject") or f"Closing the loop on {original_subject[:40]}")
    body = str(parsed.get("body") or "")

    subject = subject[:60]
    body, truncated = _enforce_word_limit(body, max_body_words=70)

    flags = []
    if truncated:
        flags.append("email_body_truncated_at_70_words")
    if not parsed:
        flags.append("llm_parse_failed_used_fallback")

    # P3-E2: tone probe
    tone_probe_result: dict | None = None
    try:
        from agent.tone_probe import score_tone
        tone_probe_result = await score_tone(subject, body, trace_id)
        if not tone_probe_result.get("passed", True):
            flags.append("tone_violation")
    except Exception:
        pass

    return {
        "subject": subject,
        "body": body,
        "email_type": "closing_3",
        "word_count": len(body.split()),
        "honesty_flags": flags,
        "tone_probe_result": tone_probe_result,
    }


# ── private helpers ───────────────────────────────────────────────────────────

def _split_subject_body(email_text: str) -> tuple[str, str]:
    """Split 'subject\\n\\nbody' email text into (subject, body) at the first blank line."""
    lines = email_text.splitlines()
    if not lines:
        return "", ""
    subject = lines[0].strip()
    body_start = 1
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    body = "\n".join(lines[body_start:]).strip()
    return subject, body


def _format_cal_block(slots: list, fallback_url: str) -> str:
    """
    Format up to 2 Cal.com slots as a booking-link block appended after the email body.
    Falls back to a generic booking link if no slots are available.
    Not counted toward the 120-word body limit.
    """
    if not slots:
        return f"\n\n→ Book a 15-minute call: {fallback_url}"

    lines = []
    for slot in slots[:2]:
        time_str = slot.get("time", "")
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            day_name = dt.strftime("%A")          # "Tuesday"
            mon_day = dt.strftime("%b %d")        # "Apr 29"
            h = int(dt.strftime("%I"))            # hour 1-12, no leading zero
            m = dt.strftime("%M")                 # "00", "30"
            ampm = dt.strftime("%p")              # "AM" or "PM"
            lines.append(f"→ {day_name} {mon_day}, {h}:{m} {ampm} UTC  [book: {fallback_url}]")
        except Exception:
            lines.append(f"→ {time_str}  [book: {fallback_url}]")

    return "\n\n" + "\n".join(lines)


def _normalise_segment(raw):
    if str(raw).lower() == "abstain":
        return "abstain"
    try:
        n = int(raw)
        if 1 <= n <= 4:
            return n
    except (ValueError, TypeError):
        pass
    return "abstain"


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


def _enforce_word_limit(email_text: str, max_body_words: int) -> tuple[str, bool]:
    """Truncate the body portion of an email to max_body_words words.

    The email format is: subject line, blank line, body. The word limit applies
    only to the body — the subject line is never truncated.
    Returns (email_text, was_truncated).
    """
    if not email_text:
        return email_text, False

    lines = email_text.split("\n")
    # Locate the first blank line that separates subject from body
    body_start = len(lines)  # default: no blank line found, entire text is body
    for i, line in enumerate(lines):
        if line.strip() == "":
            body_start = i + 1
            break

    subject_lines = lines[:body_start]
    body_lines = lines[body_start:]
    body_text = "\n".join(body_lines)

    words = body_text.split()
    if len(words) <= max_body_words:
        return email_text, False

    truncated_body = " ".join(words[:max_body_words])
    reassembled = "\n".join(subject_lines) + truncated_body
    return reassembled, True


def _generic_fallback_email(hsb: dict) -> str:
    name = hsb.get("prospect_name") or "your company"
    return (
        f"Question: engineering capacity at {name}\n\n"
        f"Hi,\n\n"
        f"I came across {name} and had a question about how you're thinking about "
        f"engineering capacity over the next quarter. We work with a small number of "
        f"Series A-C companies on staffing and delivery — happy to share what we're "
        f"seeing if the timing is right.\n\n"
        f"Would a 15-minute call make sense?\n\n"
        f"Research Partner\nTenacious Intelligence Corporation\ngettenacious.com"
    )


def _make_trace_ctx(lf, trace_id: str):
    try:
        from langfuse.types import TraceContext
        return TraceContext(trace_id=trace_id, name="compose_outreach")
    except Exception:
        return {}


def _emit_generation(lf, tc, messages, output, usage):
    try:
        usage_details = None
        if usage:
            usage_details = {
                "input": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        obs = lf.start_observation(
            trace_context=tc,
            name="compose_outreach_llm",
            as_type="generation",
            model=_DEV_MODEL,
            input=messages,
            usage_details=usage_details,
        )
        obs.update(output=output)
        obs.end()
    except Exception:
        pass
