"""
Main LLM agent — composes personalized outreach from enrichment briefs.

Business rules enforced in Python after LLM call (never delegated to the model):
  - Segment 4 requires ai_maturity.score >= 2
  - segment_confidence < 0.6 -> override to abstain
  - Bench check: required_skills vs bench_summary.json availability (0 = cannot commit)
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

    # ── Enforce business rules in Python (E2) — NOT delegated to LLM ─────────
    decision_override = False

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

    return {
        "email_to_send": email_body,
        "icp_segment": icp_segment,
        "llm_confidence": round(llm_confidence, 4),
        "bench_match_result": bench_match_result,
        "decision_override": decision_override,
        "langfuse_trace_id": trace_id,
    }


# ── private helpers ───────────────────────────────────────────────────────────

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
