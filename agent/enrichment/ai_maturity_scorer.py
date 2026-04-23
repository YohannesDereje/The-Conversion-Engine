"""LLM-based AI maturity scorer (0-3) using Qwen3 via OpenRouter with Langfuse tracing."""
import json
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-235b-a22b")
_DEV_TEMP = float(os.getenv("DEV_MODEL_TEMPERATURE", "0.0"))

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=_OPENROUTER_API_KEY, base_url=_OPENROUTER_BASE_URL)
    return _client


_SYSTEM_PROMPT = """\
You are an AI maturity analyst. Score a company's AI/ML readiness on a 0-3 scale.

Scoring rubric:
- 0: No detectable AI/ML signal in public data
- 1: Some data/analytics roles but no dedicated ML/AI function
- 2: Dedicated ML/AI roles open OR named ML/AI leadership present
- 3: Active AI function with named ML/AI leadership AND open AI-adjacent roles AND public AI product evidence

Evaluate all six signal types below. Base your assessment ONLY on the context provided.

Signal types and weights:
1. ai_adjacent_open_roles [weight: high] — Open ML/AI/Data Science/LLM engineer roles
2. named_ai_ml_leadership [weight: high] — Named VP AI, Head of ML, Chief Data Officer, etc.
3. github_org_activity [weight: medium] — Public GitHub with ML/AI repos
4. executive_commentary [weight: medium] — CEO/CTO public statements on AI strategy
5. modern_data_ml_stack [weight: medium] — Evidence of PyTorch, LangChain, MLflow, Hugging Face, etc.
6. strategic_communications [weight: low] — Blog posts, press releases, conference talks on AI

Output ONLY valid JSON (no markdown, no preamble):
{
  "score": <integer 0-3>,
  "confidence": <float 0.0-1.0>,
  "justifications": [
    {
      "signal": "<one of the 6 signal types above>",
      "status": "<what was found or explicitly not found>",
      "weight": "<high|medium|low>",
      "confidence": "<high|medium|low>",
      "source_url": "<public URL only if evidence exists — omit key for absences>"
    }
  ]
}
Include all 6 signals in justifications, even those with no evidence found.\
"""


async def score_ai_maturity(company_context: dict, langfuse_trace=None) -> dict:
    """
    Score AI maturity 0-3 using Qwen3 via OpenRouter.

    Args:
        company_context: dict with name, domain, industry, employee_count,
                         description, role_titles (list), funding_stage
        langfuse_trace: optional Langfuse trace object for span emission

    Returns:
        dict with score (int), confidence (float), justifications (list)
    """
    start = datetime.now(timezone.utc)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(company_context)},
    ]

    raw_text = ""
    usage = None
    result = _default_result()

    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=1500,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        parsed = _parse_json(raw_text)
        if parsed is not None:
            result = _normalise(parsed)
    except Exception as exc:
        result["_error"] = str(exc)

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    result["duration_ms"] = duration_ms

    if langfuse_trace is not None:
        _emit_trace(langfuse_trace, messages, raw_text, usage, duration_ms)

    return result


def _build_user_message(ctx: dict) -> str:
    lines = [
        f"Company: {ctx.get('name', 'Unknown')}",
        f"Domain: {ctx.get('domain', 'n/a')}",
        f"Industry: {ctx.get('industry', 'Unknown')}",
        f"Headcount: {ctx.get('employee_count', 'Unknown')}",
        f"Funding stage: {ctx.get('funding_stage', 'Unknown')}",
    ]
    desc = ctx.get("description")
    if desc:
        lines.append(f"Description: {str(desc)[:600]}")
    role_titles = ctx.get("role_titles") or []
    if role_titles:
        lines.append(f"Open job roles ({len(role_titles)} found):")
        lines.extend(f"  - {t}" for t in role_titles[:25])
    else:
        lines.append("Open job roles: none found / careers page unavailable")
    return "\n".join(lines)


def _parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _normalise(data: dict) -> dict:
    score = max(0, min(3, int(data.get("score", 0))))
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    justifications = []
    for j in data.get("justifications", []):
        if not isinstance(j, dict) or "signal" not in j or "status" not in j:
            continue
        j.setdefault("weight", "medium")
        j.setdefault("confidence", "low")
        if not j.get("source_url"):
            j.pop("source_url", None)
        justifications.append(j)
    return {"score": score, "confidence": confidence, "justifications": justifications}


def _default_result() -> dict:
    return {
        "score": 0,
        "confidence": 0.1,
        "justifications": [
            {
                "signal": "ai_adjacent_open_roles",
                "status": "Scoring unavailable — LLM call failed",
                "weight": "high",
                "confidence": "low",
            }
        ],
    }


def _emit_trace(trace, messages, output, usage, duration_ms):
    try:
        trace.generation(
            name="score_ai_maturity",
            model=_DEV_MODEL,
            input=messages,
            output=output,
            usage={
                "input": getattr(usage, "prompt_tokens", 0),
                "output": getattr(usage, "completion_tokens", 0),
            } if usage else None,
            metadata={"duration_ms": duration_ms},
        )
    except Exception:
        pass
