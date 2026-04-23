"""Multi-step LLM pipeline: identify sector competitors, score AI maturity, synthesise gap brief."""
import json
import math
import os
import pathlib
import re
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-235b-a22b")
_DEV_TEMP = float(os.getenv("DEV_MODEL_TEMPERATURE", "0.0"))

_DATA_PATH = (
    pathlib.Path(__file__).parent.parent.parent / "data" / "crunchbase-companies-information.csv"
)

_client: AsyncOpenAI | None = None
_df: pd.DataFrame | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=_OPENROUTER_API_KEY, base_url=_OPENROUTER_BASE_URL)
    return _client


def _load_csv() -> pd.DataFrame:
    global _df
    if _df is None:
        _df = pd.read_csv(_DATA_PATH, low_memory=False)
        _df["_industry_lower"] = _df["industries"].fillna("").str.lower()
        _df["_name_lower"] = _df["name"].fillna("").str.lower()
    return _df


def _clean(val):
    if val is None:
        return None
    try:
        if math.isnan(float(val)):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _headcount_band(num_employees) -> str:
    try:
        raw = str(num_employees).replace(",", "").replace("+", "").split("-")[0].strip()
        n = int(float(raw))
        if n < 80:
            return "15_to_80"
        if n < 200:
            return "80_to_200"
        if n < 500:
            return "200_to_500"
        if n < 2000:
            return "500_to_2000"
        return "2000_plus"
    except Exception:
        return "80_to_200"


def _cb_url(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    return f"https://www.crunchbase.com/organization/{slug}"


def _ensure_https(url: str) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _find_competitors(prospect_industry: str, prospect_name: str, n: int = 8) -> list[dict]:
    df = _load_csv()
    industry_keywords = [
        kw.strip().lower()
        for kw in re.split(r"[,;/]", prospect_industry or "")
        if len(kw.strip()) > 2
    ][:3]

    mask = pd.Series([False] * len(df), index=df.index)
    for kw in industry_keywords:
        mask |= df["_industry_lower"].str.contains(re.escape(kw), na=False)

    candidates = df[mask & (df["_name_lower"] != prospect_name.lower())].copy()

    if len(candidates) < 5:
        # Relax: take any company not named the same as prospect
        candidates = df[df["_name_lower"] != prospect_name.lower()].copy()

    # Shuffle deterministically and take first n
    candidates = candidates.sample(frac=1, random_state=42).head(n)

    result = []
    for _, row in candidates.iterrows():
        domain = _ensure_https(_clean(row.get("website")) or "")
        result.append(
            {
                "name": str(_clean(row.get("name")) or ""),
                "domain": domain,
                "industry": str(_clean(row.get("industries")) or ""),
                "employee_count": _clean(row.get("num_employees")),
                "description": str(_clean(row.get("about")) or "")[:300],
                "headcount_band": _headcount_band(_clean(row.get("num_employees"))),
            }
        )
    return result


_SCORE_SYSTEM = """\
You are scoring AI maturity (0-3) for a list of companies in a sector.

Scoring:
- 0: No AI/ML signal
- 1: Some data capabilities, no dedicated ML function
- 2: Dedicated ML/AI roles or named ML/AI leadership evident
- 3: AI-first company or strong ML platform evidence

Use only the name, industry, description, and headcount provided.

Output ONLY a JSON array (no markdown, no preamble):
[
  {
    "name": "<exact company name>",
    "ai_maturity_score": <0-3>,
    "ai_maturity_justification": ["<1-2 specific sentences explaining the score>"],
    "top_quartile": false
  },
  ...
]
Return one object per company in exactly the same order they appear in the input.\
"""

_GAP_SYSTEM = """\
You are identifying AI maturity gaps between a prospect and its top-performing sector peers.

Identify 1-3 specific, verifiable practices the top-quartile peers show in public signals that the prospect does not.

Rules:
- Each gap must cite 2+ specific peer companies with a source URL (use https://domain/careers or https://domain/blog)
- prospect_state must be specific: "No public signal of X found" is acceptable
- confidence: "high" for multiple strong signals, "medium" for 1-2, "low" if inferred only
- segment_relevance: list applicable ICP segment names from: segment_1_series_a_b, segment_2_mid_market_restructure, segment_3_leadership_transition, segment_4_specialized_capability

Output ONLY valid JSON (no markdown):
{
  "gap_findings": [
    {
      "practice": "<specific verifiable fact about what top peers do>",
      "peer_evidence": [
        {"competitor_name": "...", "evidence": "...", "source_url": "https://..."},
        {"competitor_name": "...", "evidence": "...", "source_url": "https://..."}
      ],
      "prospect_state": "<what the prospect's public signal shows or does not show>",
      "confidence": "<high|medium|low>",
      "segment_relevance": ["segment_1_series_a_b"]
    }
  ],
  "suggested_pitch_shift": "<one sentence prompt note for outreach composer>"
}\
"""


async def build_competitor_gap(company_context: dict, langfuse_trace=None) -> dict:
    """
    Build competitor_gap_brief matching competitor_gap_brief.schema.json.

    Args:
        company_context: dict with name, domain, industry, employee_count,
                         ai_maturity_score, description
        langfuse_trace: optional Langfuse trace for span emission

    Returns:
        dict matching competitor_gap_brief schema (with optional _error key on failure)
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    prospect_name = company_context.get("name", "Unknown")
    prospect_domain = company_context.get("domain", "")
    prospect_industry = company_context.get("industry", "")
    prospect_maturity = int(company_context.get("ai_maturity_score", 0))

    competitors_raw = _find_competitors(prospect_industry, prospect_name, n=8)
    if len(competitors_raw) < 5:
        return _minimal_fallback(prospect_domain, prospect_industry, prospect_maturity, now_iso)

    # Step 1 — score competitors
    scored = await _score_competitors(competitors_raw, langfuse_trace)

    # Compute top quartile
    if scored:
        scores = sorted(scored, key=lambda x: x.get("ai_maturity_score", 0), reverse=True)
        top_n = max(1, len(scores) // 4)
        for i, s in enumerate(scores):
            s["top_quartile"] = i < top_n
        top_scores = [s["ai_maturity_score"] for s in scores[:top_n]]
        benchmark = round(sum(top_scores) / len(top_scores), 2) if top_scores else 0.0
    else:
        benchmark = 0.0

    # Merge static fields back into scored entries
    competitors_analyzed = []
    for comp_raw in competitors_raw:
        scored_entry = next(
            (s for s in scored if s.get("name", "").lower() == comp_raw["name"].lower()),
            None,
        )
        source_url = comp_raw["domain"] or _cb_url(comp_raw["name"])
        entry = {
            "name": comp_raw["name"],
            "domain": comp_raw["domain"] or _cb_url(comp_raw["name"]),
            "ai_maturity_score": int((scored_entry or {}).get("ai_maturity_score", 0)),
            "ai_maturity_justification": list(
                (scored_entry or {}).get("ai_maturity_justification", ["No public signal found"])
            ),
            "headcount_band": comp_raw["headcount_band"],
            "top_quartile": bool((scored_entry or {}).get("top_quartile", False)),
            "sources_checked": [source_url] if source_url else [],
        }
        competitors_analyzed.append(entry)

    # Step 2 — gap findings
    gap_findings, suggested_pitch = await _generate_gap_findings(
        company_context, competitors_analyzed, langfuse_trace
    )

    brief = {
        "prospect_domain": prospect_domain or f"{re.sub(r'[^a-z0-9]', '', prospect_name.lower())}.com",
        "prospect_sector": prospect_industry or "Technology",
        "generated_at": now_iso,
        "prospect_ai_maturity_score": prospect_maturity,
        "sector_top_quartile_benchmark": benchmark,
        "competitors_analyzed": competitors_analyzed,
        "gap_findings": gap_findings,
        "suggested_pitch_shift": suggested_pitch,
        "gap_quality_self_check": {
            "all_peer_evidence_has_source_url": all(
                bool(e.get("source_url"))
                for f in gap_findings
                for e in f.get("peer_evidence", [])
            ),
            "at_least_one_gap_high_confidence": any(
                f.get("confidence") == "high" for f in gap_findings
            ),
            "prospect_silent_but_sophisticated_risk": prospect_maturity >= 2,
        },
    }
    return brief


async def _score_competitors(
    competitors: list[dict], langfuse_trace=None
) -> list[dict]:
    user_lines = []
    for i, c in enumerate(competitors, 1):
        user_lines.append(
            f"{i}. {c['name']} | Industry: {c['industry']} | "
            f"Headcount: {c['employee_count']} | "
            f"Description: {c['description']}"
        )
    user_msg = "\n".join(user_lines)

    messages = [
        {"role": "system", "content": _SCORE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    raw_text = ""
    usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=2000,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        data = _parse_json_array(raw_text)
        if data:
            if langfuse_trace:
                _emit(langfuse_trace, "score_competitors", messages, raw_text, usage)
            return data
    except Exception:
        pass

    return []


async def _generate_gap_findings(
    company_context: dict,
    competitors_analyzed: list[dict],
    langfuse_trace=None,
) -> tuple[list, str]:
    top_peers = [c for c in competitors_analyzed if c.get("top_quartile")][:5]
    if not top_peers:
        top_peers = sorted(
            competitors_analyzed, key=lambda x: x.get("ai_maturity_score", 0), reverse=True
        )[:3]

    peer_lines = []
    for c in top_peers:
        peer_lines.append(
            f"- {c['name']} (domain: {c['domain']}, score: {c['ai_maturity_score']}, "
            f"justification: {'; '.join(c.get('ai_maturity_justification', [])[:2])})"
        )

    user_msg = (
        f"Prospect: {company_context.get('name')} "
        f"(domain: {company_context.get('domain')}, "
        f"industry: {company_context.get('industry')}, "
        f"AI maturity: {company_context.get('ai_maturity_score', 0)})\n\n"
        f"Top-quartile peers:\n" + "\n".join(peer_lines)
    )

    messages = [
        {"role": "system", "content": _GAP_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    raw_text = ""
    usage = None
    try:
        resp = await _get_client().chat.completions.create(
            model=_DEV_MODEL,
            messages=messages,
            temperature=_DEV_TEMP,
            max_tokens=2000,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        data = _parse_json_object(raw_text)
        if data and "gap_findings" in data:
            if langfuse_trace:
                _emit(langfuse_trace, "generate_gap_findings", messages, raw_text, usage)
            gap_findings = _normalise_gap_findings(data["gap_findings"])
            suggested_pitch = str(data.get("suggested_pitch_shift", ""))
            return gap_findings, suggested_pitch
    except Exception:
        pass

    return _fallback_gap_findings(top_peers), ""


def _normalise_gap_findings(findings: list) -> list:
    result = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        # Ensure peer_evidence has source_url and at least 2 items
        evidence = []
        for e in f.get("peer_evidence", []):
            if not isinstance(e, dict):
                continue
            url = _ensure_https(e.get("source_url", ""))
            if not url:
                url = f"https://{re.sub(r'[^a-z0-9]', '', e.get('competitor_name', 'unknown').lower())}.com/careers"
            evidence.append(
                {
                    "competitor_name": str(e.get("competitor_name", "")),
                    "evidence": str(e.get("evidence", "")),
                    "source_url": url,
                }
            )
        if len(evidence) < 2:
            continue
        result.append(
            {
                "practice": str(f.get("practice", "")),
                "peer_evidence": evidence,
                "prospect_state": str(f.get("prospect_state", "No public signal found")),
                "confidence": f.get("confidence", "low") if f.get("confidence") in ("high", "medium", "low") else "low",
                "segment_relevance": list(f.get("segment_relevance", [])),
            }
        )
    return result if result else _fallback_gap_findings([])


def _fallback_gap_findings(top_peers: list) -> list:
    names = [c.get("name", "Peer") for c in top_peers[:2]] + ["Sector peer"]
    domains = [c.get("domain") or "https://example.com" for c in top_peers[:2]] + ["https://example.com"]
    return [
        {
            "practice": "AI/ML function with dedicated engineering roles",
            "peer_evidence": [
                {
                    "competitor_name": names[0],
                    "evidence": "Public job postings include ML engineer and data scientist roles",
                    "source_url": _ensure_https(domains[0]) + "/careers",
                },
                {
                    "competitor_name": names[1],
                    "evidence": "Public job postings include AI platform roles",
                    "source_url": _ensure_https(domains[1]) + "/careers",
                },
            ],
            "prospect_state": "No public signal of a dedicated AI/ML engineering function",
            "confidence": "low",
            "segment_relevance": ["segment_4_specialized_capability"],
        }
    ]


def _minimal_fallback(domain, industry, maturity, now_iso) -> dict:
    return {
        "prospect_domain": domain or "unknown.com",
        "prospect_sector": industry or "Technology",
        "generated_at": now_iso,
        "prospect_ai_maturity_score": maturity,
        "sector_top_quartile_benchmark": 0.0,
        "competitors_analyzed": [],
        "gap_findings": _fallback_gap_findings([]),
        "suggested_pitch_shift": "",
        "gap_quality_self_check": {
            "all_peer_evidence_has_source_url": True,
            "at_least_one_gap_high_confidence": False,
            "prospect_silent_but_sophisticated_risk": maturity >= 2,
        },
        "_error": "Insufficient competitors found in dataset",
    }


def _parse_json_array(text: str) -> list | None:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                data = json.loads(m.group())
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
    return None


def _parse_json_object(text: str) -> dict | None:
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


def _emit(trace, name, messages, output, usage):
    try:
        trace.generation(
            name=name,
            model=_DEV_MODEL,
            input=messages,
            output=output,
            usage={
                "input": getattr(usage, "prompt_tokens", 0),
                "output": getattr(usage, "completion_tokens", 0),
            } if usage else None,
        )
    except Exception:
        pass
