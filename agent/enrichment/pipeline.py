"""
Enrichment pipeline orchestrator.
Runs C1->C2->D1->D2->D3 in sequence, validates outputs against JSON schemas,
emits a parent Langfuse trace, and returns (hiring_signal_brief, competitor_gap_brief).

Usage:
    python -m agent.enrichment.pipeline --company "Stripe"
"""
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

import jsonschema
from dotenv import load_dotenv

load_dotenv()

from agent.enrichment.ai_maturity_scorer import score_ai_maturity
from agent.enrichment.competitor_gap_builder import build_competitor_gap
from agent.enrichment.crunchbase_enricher import enrich as crunchbase_enrich
from agent.enrichment.job_post_scraper import scrape_job_postings
from agent.enrichment.layoffs_enricher import check_layoffs

# ── paths ────────────────────────────────────────────────────────────────────
_ROOT = pathlib.Path(__file__).parent.parent.parent
_SCHEMA_DIR = _ROOT / "tenacious_sales_data" / "schemas"
_BENCH_PATH = _ROOT / "tenacious_sales_data" / "seed" / "bench_summary.json"

# ── schema cache ─────────────────────────────────────────────────────────────
_HIRING_SCHEMA: dict | None = None
_COMPETITOR_SCHEMA: dict | None = None
_BENCH: dict | None = None


def _hiring_schema() -> dict:
    global _HIRING_SCHEMA
    if _HIRING_SCHEMA is None:
        _HIRING_SCHEMA = json.loads(
            (_SCHEMA_DIR / "hiring_signal_brief.schema.json").read_text()
        )
    return _HIRING_SCHEMA


def _competitor_schema() -> dict:
    global _COMPETITOR_SCHEMA
    if _COMPETITOR_SCHEMA is None:
        _COMPETITOR_SCHEMA = json.loads(
            (_SCHEMA_DIR / "competitor_gap_brief.schema.json").read_text()
        )
    return _COMPETITOR_SCHEMA


def _bench() -> dict:
    global _BENCH
    if _BENCH is None:
        _BENCH = json.loads(_BENCH_PATH.read_text())
    return _BENCH


# ── Langfuse client & v4 compatibility proxy ─────────────────────────────────
_langfuse = None


def _get_langfuse():
    global _langfuse
    if _langfuse is None:
        try:
            from langfuse import Langfuse
            _langfuse = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception:
            _langfuse = _NoopLangfuse()
    return _langfuse


class _NoopLangfuse:
    """Drop-in when Langfuse init fails — swallows all calls silently."""
    def create_trace_id(self): return "noop"
    def flush(self): pass


class _LangfuseTraceProxy:
    """
    Adapter that exposes a v2-style .generation() / .span() / .update() interface
    while routing to the Langfuse v4 start_observation() API.
    """

    def __init__(self, lf, trace_context):
        self._lf = lf
        self._tc = trace_context

    def generation(self, *, name, model=None, input=None, output=None,
                   usage=None, metadata=None, **_):
        try:
            usage_details = None
            if usage:
                usage_details = {
                    "input": int(usage.get("input", 0) or 0),
                    "output": int(usage.get("output", 0) or 0),
                }
            obs = self._lf.start_observation(
                trace_context=self._tc,
                name=name,
                as_type="generation",
                model=model,
                input=input,
                usage_details=usage_details,
                metadata=metadata,
            )
            if output is not None:
                obs.update(output=output)
            obs.end()
        except Exception:
            pass

    def span(self, *, name, input=None, output=None, **_):
        try:
            obs = self._lf.start_observation(
                trace_context=self._tc,
                name=name,
                as_type="span",
                input=input,
            )
            if output is not None:
                obs.update(output=output)
            obs.end()
        except Exception:
            pass

    def update(self, *, output=None, **_):
        # Best-effort: record pipeline summary as a final span
        try:
            obs = self._lf.start_observation(
                trace_context=self._tc,
                name="_pipeline_summary",
                as_type="span",
                output=output,
            )
            obs.end()
        except Exception:
            pass


def _make_trace(lf, company_name: str, now_iso: str) -> "_LangfuseTraceProxy":
    try:
        from langfuse.types import TraceContext
        tid = lf.create_trace_id()
        tc = TraceContext(trace_id=tid, name="enrichment_pipeline",
                         metadata={"company_name": company_name, "generated_at": now_iso})
        return _LangfuseTraceProxy(lf, tc)
    except Exception:
        return _LangfuseTraceProxy(_NoopLangfuse(), {})


# ── main pipeline ─────────────────────────────────────────────────────────────

async def run_enrichment_pipeline(company_name: str) -> tuple[dict, dict]:
    """
    Orchestrate all enrichment steps.

    Returns:
        (hiring_signal_brief, competitor_gap_brief) — both dicts validated against schemas.
    """
    lf = _get_langfuse()
    now_iso = datetime.now(timezone.utc).isoformat()
    trace = _make_trace(lf, company_name, now_iso)

    sources_checked = []

    # ── Step 1: Crunchbase ──────────────────────────────────────────────────
    crunchbase_data = crunchbase_enrich(company_name)
    _record_source(
        sources_checked,
        "crunchbase_csv",
        "success" if crunchbase_data.get("name") else "no_data",
    )
    _safe_span(trace, "crunchbase_enrich", company_name, crunchbase_data)

    # ── Step 2: Layoffs ─────────────────────────────────────────────────────
    layoff_data = check_layoffs(company_name)
    _record_source(sources_checked, "layoffs_fyi_csv", "success")
    _safe_span(trace, "layoffs_check", company_name, layoff_data)

    # ── Step 3: Job scraper ─────────────────────────────────────────────────
    domain = crunchbase_data.get("domain") or _guess_domain(company_name)
    job_data = await scrape_job_postings(domain)
    _record_source(sources_checked, "job_post_scraper", job_data.get("status", "no_data"))
    _safe_span(trace, "job_scrape", domain, job_data)

    # ── Step 4: AI maturity scoring ─────────────────────────────────────────
    company_context = {
        "name": crunchbase_data.get("name") or company_name,
        "domain": domain,
        "industry": crunchbase_data.get("industry"),
        "employee_count": crunchbase_data.get("employee_count"),
        "description": crunchbase_data.get("description"),
        "funding_stage": crunchbase_data.get("last_funding_stage"),
        "role_titles": job_data.get("role_titles", []),
    }
    maturity_data = await score_ai_maturity(company_context, langfuse_trace=trace)
    _record_source(
        sources_checked,
        "ai_maturity_scorer_llm",
        "error" if maturity_data.get("_error") else "success",
        maturity_data.get("_error"),
    )

    # ── Step 5: Competitor gap ──────────────────────────────────────────────
    company_context["ai_maturity_score"] = maturity_data.get("score", 0)
    gap_brief = await build_competitor_gap(company_context, langfuse_trace=trace)
    _record_source(
        sources_checked,
        "competitor_gap_builder_llm",
        "error" if gap_brief.get("_error") else "success",
        gap_brief.get("_error"),
    )

    # ── Assemble hiring_signal_brief ────────────────────────────────────────
    segment, seg_conf = _classify_segment(crunchbase_data, layoff_data, job_data, maturity_data)
    tech_stack = _infer_tech_stack(job_data.get("role_titles", []), crunchbase_data.get("industry"))
    bench_match = _compute_bench_match(tech_stack)
    honesty_flags = _compute_honesty_flags(maturity_data, job_data, bench_match, layoff_data, crunchbase_data)

    hsb = {
        "prospect_domain": domain,
        "prospect_name": crunchbase_data.get("name") or company_name,
        "generated_at": now_iso,
        "primary_segment_match": segment,
        "segment_confidence": round(seg_conf, 4),
        "ai_maturity": {
            "score": maturity_data.get("score", 0),
            "confidence": maturity_data.get("confidence", 0.1),
            "justifications": maturity_data.get("justifications", []),
        },
        "hiring_velocity": {
            "open_roles_today": job_data.get("open_roles_today", 0),
            "open_roles_60_days_ago": 0,
            "velocity_label": _velocity_label(job_data),
            "signal_confidence": 0.4 if job_data.get("status") in ("no_data", "error") else 0.6,
            "sources": [s for s in job_data.get("sources", []) if s in ("builtin", "wellfound", "linkedin_public", "company_careers_page")],
        },
        "buying_window_signals": {
            "funding_event": _funding_event(crunchbase_data),
            "layoff_event": _layoff_event(layoff_data),
            "leadership_change": {"detected": False, "role": "none"},
        },
        "tech_stack": tech_stack,
        "bench_to_brief_match": bench_match,
        "data_sources_checked": sources_checked,
        "honesty_flags": honesty_flags,
    }

    # ── Schema validation ────────────────────────────────────────────────────
    hsb_errors = _validate(hsb, _hiring_schema())
    if hsb_errors:
        _safe_span(trace, "hiring_brief_validation_errors", None, hsb_errors)

    cgb_errors = _validate(gap_brief, _competitor_schema())
    if cgb_errors:
        _safe_span(trace, "competitor_brief_validation_errors", None, cgb_errors)

    trace.update(
        output={
            "segment": segment,
            "maturity_score": maturity_data.get("score", 0),
            "competitors_count": len(gap_brief.get("competitors_analyzed", [])),
        }
    )
    lf.flush()

    return hsb, gap_brief


# ── helpers ───────────────────────────────────────────────────────────────────

def _guess_domain(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
    return f"{slug}.com"


def _record_source(sources: list, name: str, status: str, error: str | None = None):
    entry = {
        "source": name,
        "status": status,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        entry["error_message"] = str(error)
    sources.append(entry)


def _safe_span(trace, name, inp, out):
    trace.span(name=name, input=inp, output=out)


def _velocity_label(job_data: dict) -> str:
    if job_data.get("status") in ("no_data", "error") or job_data.get("open_roles_today", 0) == 0:
        return "insufficient_signal"
    return "insufficient_signal"  # no 60-day baseline available


def _classify_segment(
    crunchbase_data: dict,
    layoff_data: dict,
    job_data: dict,
    maturity_data: dict,
) -> tuple[str, float]:
    now = datetime.now(timezone.utc)

    # Parse employee count
    employee_str = str(crunchbase_data.get("employee_count") or "0")
    try:
        employees = int(
            employee_str.replace(",", "").replace("+", "").split("-")[0].strip()
        )
    except Exception:
        employees = 0

    # Parse investment stage
    investment_stage = (crunchbase_data.get("last_funding_stage") or "").lower()
    fresh_series_ab = any(
        s in investment_stage
        for s in ("series a", "series_a", "series-a", "series b", "series_b", "series-b")
    )
    mid_market_stage = employees >= 200 or any(
        s in investment_stage for s in ("series c", "late stage", "series d")
    )

    # Parse layoff
    layoff_recent_120d = False
    if layoff_data.get("detected"):
        date_str = str(layoff_data.get("date") or "")
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                from datetime import datetime as dt
                ld = dt.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                if (now - ld).days <= 120:
                    layoff_recent_120d = True
                break
            except ValueError:
                continue

    maturity_score = maturity_data.get("score", 0)
    open_roles = job_data.get("open_roles_today", 0)

    # AI-adjacent role count
    _AI_RE = re.compile(
        r"\b(ml|machine.?learning|llm|ai|nlp|mlops|data.?science|deep.?learning|"
        r"neural|transformer|embedding|vector|reinforcement)\b",
        re.IGNORECASE,
    )
    ai_role_count = sum(
        1 for t in job_data.get("role_titles", []) if _AI_RE.search(t)
    )

    # Priority order per icp_definition.md
    if layoff_recent_120d and (fresh_series_ab or mid_market_stage) and employees >= 200:
        return "segment_2_mid_market_restructure", 0.75

    if layoff_recent_120d and employees >= 100:
        return "segment_2_mid_market_restructure", 0.62

    # Segment 3 (leadership change) — not detectable from current data sources
    # Skipped intentionally; segment_confidence would be < 0.6 anyway

    if maturity_score >= 2 and ai_role_count >= 3:
        return "segment_4_specialized_capability", 0.70

    if maturity_score >= 2 and open_roles >= 5:
        return "segment_4_specialized_capability", 0.61

    if fresh_series_ab:
        if 15 <= employees <= 80 and open_roles >= 5:
            return "segment_1_series_a_b", 0.80
        if 15 <= employees <= 80:
            return "segment_1_series_a_b", 0.65
        return "segment_1_series_a_b", 0.52

    return "abstain", 0.38


def _funding_event(crunchbase_data: dict) -> dict:
    stage_raw = (crunchbase_data.get("last_funding_stage") or "").lower()
    _STAGE_MAP = {
        "seed": "seed",
        "series a": "series_a",
        "series_a": "series_a",
        "series-a": "series_a",
        "series b": "series_b",
        "series_b": "series_b",
        "series-b": "series_b",
        "series c": "series_c",
        "series_c": "series_c",
        "series d": "series_d_plus",
        "late stage venture": "series_d_plus",
        "early stage venture": "series_a",
        "debt": "debt",
    }
    mapped = next((v for k, v in _STAGE_MAP.items() if k in stage_raw), None)

    if not mapped and not stage_raw:
        return {"detected": False, "stage": "none"}

    event: dict = {"detected": mapped is not None, "stage": mapped or "other"}
    funding_total = crunchbase_data.get("funding_total")
    if funding_total is not None:
        try:
            ft = str(funding_total).replace("$", "").replace(",", "").strip()
            if ft.upper().endswith("M"):
                event["amount_usd"] = int(float(ft[:-1]) * 1_000_000)
            elif ft.upper().endswith("B"):
                event["amount_usd"] = int(float(ft[:-1]) * 1_000_000_000)
            else:
                event["amount_usd"] = int(float(ft))
        except Exception:
            pass
    return event


def _layoff_event(layoff_data: dict) -> dict:
    if not layoff_data.get("detected"):
        return {"detected": False}
    event: dict = {"detected": True}
    if layoff_data.get("date"):
        event["date"] = str(layoff_data["date"])
    hr = layoff_data.get("headcount_reduction")
    if hr is not None:
        try:
            event["headcount_reduction"] = int(float(str(hr).replace(",", "")))
        except Exception:
            pass
    pct = layoff_data.get("percentage_cut")
    if pct is not None:
        try:
            event["percentage_cut"] = float(str(pct).replace("%", ""))
        except Exception:
            pass
    if layoff_data.get("source_url"):
        event["source_url"] = str(layoff_data["source_url"])
    return event


_TECH_PATTERNS = {
    "Python": r"\bpython\b",
    "Go": r"\b(golang|go(?:\s+engineer|dev))\b",
    "React": r"\breact\b",
    "Next.js": r"\bnext\.?js\b",
    "TypeScript": r"\btypescript\b",
    "PyTorch": r"\bpytorch\b",
    "TensorFlow": r"\btensorflow\b",
    "LangChain": r"\blangchain\b",
    "Kubernetes": r"\b(kubernetes|k8s)\b",
    "Terraform": r"\bterraform\b",
    "AWS": r"\baws\b",
    "dbt": r"\bdbt\b",
    "Snowflake": r"\bsnowflake\b",
    "Databricks": r"\bdatabricks\b",
    "MLOps": r"\bmlops\b",
    "LLM": r"\bllm\b",
    "RAG": r"\b(rag|retrieval.augmented)\b",
}

_BENCH_STACK_KEYWORDS = {
    "python": ["python", "django", "fastapi", "flask"],
    "go": ["go", "golang"],
    "data": ["dbt", "snowflake", "databricks", "airflow", "fivetran"],
    "ml": ["pytorch", "tensorflow", "llm", "langchain", "mlops", "rag", "hugging face"],
    "infra": ["terraform", "kubernetes", "docker", "aws", "gcp", "azure", "k8s"],
    "frontend": ["react", "next.js", "typescript", "vue", "angular"],
}


def _infer_tech_stack(role_titles: list, industry: str | None) -> list:
    text = " ".join(role_titles).lower() + " " + (industry or "").lower()
    found = []
    for name, pat in _TECH_PATTERNS.items():
        if re.search(pat, text, re.IGNORECASE):
            found.append(name)
    return sorted(found)


def _compute_bench_match(tech_stack: list) -> dict:
    bench_stacks = _bench().get("stacks", {})
    required_keys: set[str] = set()
    for tech in tech_stack:
        t_lower = tech.lower()
        for bench_key, keywords in _BENCH_STACK_KEYWORDS.items():
            if any(kw in t_lower for kw in keywords):
                required_keys.add(bench_key)

    gaps = []
    bench_available = True
    for key in required_keys:
        available = bench_stacks.get(key, {}).get("available_engineers", 0)
        if available == 0:
            gaps.append(key)
            bench_available = False

    return {
        "required_stacks": sorted(required_keys),
        "bench_available": bench_available,
        "gaps": gaps,
    }


def _compute_honesty_flags(
    maturity_data: dict,
    job_data: dict,
    bench_match: dict,
    layoff_data: dict,
    crunchbase_data: dict,
) -> list:
    flags = []
    if job_data.get("status") in ("no_data", "error") or job_data.get("open_roles_today", 0) == 0:
        flags.append("weak_hiring_velocity_signal")
    if maturity_data.get("confidence", 0) < 0.5 or maturity_data.get("score", 0) == 0:
        flags.append("weak_ai_maturity_signal")
    layoff_detected = layoff_data.get("detected", False)
    funding_stage = (crunchbase_data.get("last_funding_stage") or "").lower()
    if layoff_detected and any(s in funding_stage for s in ("series a", "series b")):
        flags.append("layoff_overrides_funding")
    if not bench_match.get("bench_available", True):
        flags.append("bench_gap_detected")
    if bench_match.get("required_stacks"):
        flags.append("tech_stack_inferred_not_confirmed")
    return flags


def _validate(brief: dict, schema: dict) -> list[str]:
    errors = []
    try:
        jsonschema.validate(instance=brief, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(e.message)
    except Exception as e:
        errors.append(str(e))
    return errors


# ── __main__ entry point (D5 smoke test) ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Run enrichment pipeline for a company")
    parser.add_argument("--company", required=True, help='Company name, e.g. "Stripe"')
    parser.add_argument("--out-dir", default=".", help="Directory to write output JSON files")
    args = parser.parse_args()

    async def _main():
        print(f"Running enrichment pipeline for: {args.company}")
        hsb, cgb = await run_enrichment_pipeline(args.company)

        out_dir = pathlib.Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        hsb_path = out_dir / "hiring_signal_brief.json"
        cgb_path = out_dir / "competitor_gap_brief.json"

        hsb_path.write_text(json.dumps(hsb, indent=2))
        cgb_path.write_text(json.dumps(cgb, indent=2))

        print(f"\nHiring Signal Brief -> {hsb_path}")
        print(f"  segment: {hsb.get('primary_segment_match')}")
        print(f"  ai_maturity: {hsb.get('ai_maturity', {}).get('score')}")
        print(f"  open_roles: {hsb.get('hiring_velocity', {}).get('open_roles_today')}")

        print(f"\nCompetitor Gap Brief -> {cgb_path}")
        print(f"  competitors_analyzed: {len(cgb.get('competitors_analyzed', []))}")
        print(f"  gap_findings: {len(cgb.get('gap_findings', []))}")
        print(f"  top_quartile_benchmark: {cgb.get('sector_top_quartile_benchmark')}")

        return hsb, cgb

    asyncio.run(_main())
