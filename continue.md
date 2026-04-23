# Continue.md — Session Handover for The Conversion Engine

> **How to use this file:** If you start a new Claude Code session and need to pick up where we left off, paste this entire file into the chat and say "continue from continue.md". Claude will have full context and can resume immediately without re-reading the codebase from scratch.

---

## Project Identity

**Project name:** The Conversion Engine for Tenacious Consulting
**Course:** 10Academy Week 10 — Sales Automation Challenge
**Student:** Yohannes Dereje (yohannes@10academy.org)
**Repository:** https://github.com/YohannesDereje/The-Conversion-Engine
**Root directory on this machine:** `c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10\The conversion Engine`
**Python:** 3.13 (system Python, `pip` installs go to AppData\Local\Programs\Python\Python313)

---

## What This System Does

The Conversion Engine is a FastAPI application that automates B2B outbound prospecting for Tenacious Consulting. Given a company name and contact email, it:

1. Runs a 5-signal enrichment pipeline (Crunchbase CSV, layoffs.fyi CSV, Playwright job scraping, AI maturity scoring via Qwen3, leadership change detection via Qwen3)
2. Classifies the prospect into one of 4 ICP segments (or abstain)
3. Composes a personalized cold email using Qwen3 via OpenRouter with honesty constraints
4. Upserts a HubSpot contact with all enrichment data
5. Sends the email via Resend (or routes to a sink if kill switch is off)
6. Logs the email activity as a HubSpot Note engagement

---

## Completion Status as of April 23, 2026

### INTERIM SUBMISSION: COMPLETED — SCORED 100/100

All groups A through J are complete. Here is the exact status:

| Group | Task | Status |
|-------|------|--------|
| A | Environment setup (HubSpot, Cal.com, ngrok, Resend, CSV) | Done by user manually |
| B | Project structure (agent/, enrichment/, eval/, infra/) | Done |
| C | Data layer (crunchbase_enricher.py, layoffs_enricher.py) | Done |
| D | Enrichment pipeline (job scraper, AI maturity, competitor gap, pipeline.py) | Done |
| E | Agent core (agent_core.py with honesty constraints + segment routing) | Done |
| F | Integration handlers (email, SMS, HubSpot, Cal.com, Langfuse tracing) | Done |
| G | FastAPI app + G2 e2e test + G3 20-lead synthetic run | Done |
| H | τ²-Bench harness + baseline run + score_log.json | Done |
| I | README.md, baseline.md, .gitignore, GitHub push | Done |
| J | interim_report.html + interim_report.pdf | Done |

---

## Architecture Summary

```
POST /leads/process
  → run_enrichment_pipeline(company_name)
      → crunchbase_enrich()             [CSV fuzzy match]
      → check_layoffs()                 [CSV fuzzy match]
      → scrape_job_postings()           [Playwright]
      → _detect_leadership_change_llm() [Qwen3 via OpenRouter]
      → score_ai_maturity()             [Qwen3 via OpenRouter]
      → build_competitor_gap()          [Qwen3 via OpenRouter]
      → returns (hiring_signal_brief, competitor_gap_brief)
  → compose_outreach(briefs)            [Qwen3 via OpenRouter]
  → create_or_update_contact()          [HubSpot API v3]
  → send_email()                        [Resend API v2.29.0]
  → log_email_activity()                [HubSpot Note engagement]
```

**Channel hierarchy:**
- Email (Resend) → PRIMARY — all outreach, cold and warm
- SMS (Africa's Talking) → SECONDARY — warm leads only (PermissionError raised for cold)
- Voice → FUTURE Act V — not yet implemented

**Kill switch:** `KILL_SWITCH_LIVE_OUTBOUND` in `.env`
- `false` (safe default) → email and SMS route to `OUTBOUND_SINK_EMAIL` / `OUTBOUND_SINK_SMS`
- `true` → live delivery to real recipients

---

## Key Files and What They Do

### Core Application
- `agent/main.py` — FastAPI app. 4 endpoints: POST /leads/process, POST /email/webhook, POST /sms/webhook, GET /health
- `agent/utils.py` — Shared env vars, Langfuse v4 singleton, `emit_span()` helper used by all handlers
- `agent/agent_core.py` — LLM outreach composer. Enforces honesty constraints in Python post-LLM.

### Enrichment Pipeline
- `agent/enrichment/pipeline.py` — Orchestrator. Runs 5 steps, validates output against JSON schemas.
- `agent/enrichment/crunchbase_enricher.py` — Fuzzy match against local Crunchbase CSV
- `agent/enrichment/layoffs_enricher.py` — Fuzzy match against `data/layoffs_fyi.csv`
- `agent/enrichment/job_post_scraper.py` — Playwright scraper. Returns `no_data` on failure (graceful).
- `agent/enrichment/ai_maturity_scorer.py` — Qwen3. Scores 0-3 on 6 signals. Returns justifications.
- `agent/enrichment/competitor_gap_builder.py` — Qwen3. Builds competitor list + gap findings.

### Integration Handlers
- `agent/email_handler.py` — Resend send + webhook handlers. `register_reply_handler()` / `register_bounce_handler()` callback registry.
- `agent/sms_handler.py` — Africa's Talking send + inbound webhook. `is_warm_lead=True` required to send.
- `agent/hubspot_handler.py` — HubSpot API v3. `create_or_update_contact()`, `log_email_activity()`, `log_meeting_booked()`.
- `agent/calcom_handler.py` — Cal.com Cloud v2. `get_available_slots()`, `book_slot()`. Booking auto-triggers HubSpot meeting log when `contact_id` is passed.

### Evaluation
- `eval/tau2_bench_runner.py` — Runs τ²-Bench retail domain, 30 tasks, 1 trial. Uses Qwen3 via OpenRouter.
- `eval/score_log.json` — Two entries: facilitator baseline (0.7267 pass@1) + reproduction run (0.2667 pass@1)
- `eval/trace_log.jsonl` — One line per τ²-Bench task run (150+ entries from multiple runs)
- `eval/baseline.md` — Written analysis of the τ²-Bench results

### Schemas
- `tenacious_sales_data/schemas/hiring_signal_brief.schema.json` — Full JSON schema for enrichment output
- `tenacious_sales_data/schemas/competitor_gap_brief.schema.json` — Full JSON schema for competitor brief
- `tenacious_sales_data/seed/bench_summary.json` — Tenacious bench engineer capacity by stack

### Reports (Interim Submission)
- `interim_report.html` — Full HTML interim report with embedded SVG architecture diagram, all rubric sections
- `generate_pdf.py` — Converts HTML to PDF using Chrome headless or weasyprint
- `baseline.md` (root) — τ²-Bench baseline reproduction report

---

## Environment Variables (.env file — NOT committed to git)

```
KILL_SWITCH_LIVE_OUTBOUND=false
OUTBOUND_SINK_EMAIL=yohannes@10academy.org
OUTBOUND_SINK_SMS=+251...
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEV_MODEL=qwen/qwen3-235b-a22b
DEV_MODEL_TEMPERATURE=0.0
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=onboarding@resend.dev
HUBSPOT_ACCESS_TOKEN=pat-na1-...
CALCOM_API_KEY=cal_...
CALCOM_BASE_URL=https://api.cal.com
CALCOM_EVENT_TYPE_ID=...
AFRICAS_TALKING_API_KEY=...
AFRICAS_TALKING_USERNAME=sandbox
AT_SHORTCODE=
```

---

## How to Run the System

```bash
# Start the API server (run from The conversion Engine/ directory)
uvicorn agent.main:app --reload

# Test a lead
curl.exe -X POST http://127.0.0.1:8000/leads/process \
  -H "Content-Type: application/json" \
  -d '{"company_name":"Stripe","contact_email":"test@sink.com","contact_name":"Test User"}'

# Health check
curl.exe http://127.0.0.1:8000/health

# Run enrichment pipeline directly (smoke test)
python -m agent.enrichment.pipeline --company "Stripe"

# Run τ²-Bench
python eval/tau2_bench_runner.py

# Run 20 synthetic leads (G3)
python scripts/generate_synthetic_interactions.py
```

---

## Known Issues and Limitations

### 1. Job Post Scraper Hit Rate ~25%
Most company careers pages block Playwright headless browsers. When blocked, `scrape_job_postings()` returns `{"status": "no_data"}`. This means:
- `hiring_velocity.velocity_label` is always `"insufficient_signal"` for most companies
- Segment 4 (ai_role_count ≥ 3) almost never activates
- The honesty flag `weak_hiring_velocity_signal` is set, forcing ask-not-assert language

**Plan for Act III:** Add SerpAPI as a fallback source for job listings.

### 2. τ²-Bench pass@1 = 0.2667 (vs facilitator's 0.7267)
Root causes:
- Only 1 trial run (no statistical averaging)
- Used `qwen3-235b-a22b` (dense model) not `qwen3-next-80b-a3b-thinking` (thinking mode)
- 5 tasks hit `runner_error` (tool call format mismatch)
The reproduction run serves as a reference point — not a claim of equivalence with the facilitator's baseline.

### 3. τ²-Bench cost_usd = $0.0000
tau2's internal cost calculator doesn't capture OpenRouter billing. Actual cost was ~$0.013–0.096 per task. See `eval/baseline.md` for explanation.

### 4. SMS Not End-to-End Tested
Africa's Talking is in sandbox mode. The warm-lead SMS follow-up workflow hasn't been triggered because no real prospect has replied to an outreach email. The code is complete — the workflow needs a real warm-lead event to fire.

### 5. Crunchbase CSV Gaps
Many well-known companies (Stripe, Notion, Linear) are absent from the local training CSV or have partial data. The enricher returns `no_data` and the pipeline relies purely on LLM inference for firmographics.

### 6. HubSpot Custom Properties Must Pre-Exist
Five custom properties must be manually created in the HubSpot portal Settings → Properties before they persist:
`crunchbase_id`, `ai_maturity_score`, `icp_segment`, `enrichment_timestamp`, `hiring_signal_brief`
Standard properties (firstname, lastname, email, company, hs_lead_status, industry) work without setup.

---

## ICP Segment Definitions

| Segment | Trigger Conditions | Email Angle | Template |
|---------|-------------------|-------------|----------|
| `segment_1_series_a_b` | fresh Series A or B + 15–80 employees | Funding-angle pitch | seed/email_sequences/cold.md |
| `segment_2_mid_market_restructure` | layoff ≤ 120 days + employees ≥ 100 | Restructuring-angle | cold.md |
| `segment_3_leadership_transition` | leadership_change.detected == true (LLM) | Leadership-transition-angle | cold.md |
| `segment_4_specialized_capability` | ai_maturity ≥ 2 AND (ai_role_count ≥ 3 OR open_roles ≥ 5) | Capability-gap-angle | cold.md |
| `abstain` | segment_confidence < 0.6 or no signal | Generic exploratory email | abstain fallback |

**Honesty overrides (Python, not LLM):**
- `velocity_label == "insufficient_signal"` → use "ask" language, never assert hiring velocity claims
- `segment_confidence < 0.6` → override to abstain
- `bench_available == False` → flag to human, do NOT commit capacity

---

## Langfuse Tracing Architecture

Every function call that touches an external API emits a span via `emit_span()` in `agent/utils.py`:

```python
emit_span(
    trace_id=trace_id,
    name="handler.function_name",
    input={...},
    output={...},
    latency_ms=latency_ms,
)
```

The enrichment pipeline creates a parent trace with child spans per step. The agent core creates its own trace. All trace IDs flow through the pipeline and are returned in the `/leads/process` response as `langfuse_trace_id`.

---

## τ²-Bench Harness Details

```python
# eval/tau2_bench_runner.py key config
DOMAIN = "retail"
NUM_TASKS = 30
NUM_TRIALS = 1  # facilitator update: 1 trial sufficient
MODEL = "openrouter/qwen/qwen3-235b-a22b"
TEMPERATURE = 0.0
```

tau2-bench must be cloned separately. The runner adds `tau2-bench/src` to `sys.path`. Dependencies installed: `addict`, `litellm`, `tenacity`, `deepdiff`, `rich`, `tabulate`, `docstring-parser`, `PyYAML`, `toml`, `audioop-lts` (Python 3.13 compatibility).

---

## What Comes Next (Acts III–V)

Based on the interim report's forward plan:

### Act III (next priority)
1. **SerpAPI fallback for job scraper** — add to `job_post_scraper.py` as fallback when Playwright returns `no_data`. SerpAPI has a Google Jobs endpoint.
2. **SMS warm-lead follow-up** — register a reply handler in `email_handler.py` that: detects positive reply intent → triggers `send_sms(is_warm_lead=True)` with a calendar link
3. **Cal.com slots in email body** — call `get_available_slots()` during lead processing and include 2-3 slots in the outreach email

### Act IV
1. **HubSpot lead-status state machine** — `NEW → IN_PROGRESS → REPLIED → SCHEDULED`
2. **Re-enrichment on reply** — when a prospect replies, run enrichment again and update HubSpot with fresh signal
3. **Segment confidence improvement** — use SerpAPI data to improve job velocity signal and unlock Segment 4

### Act V
1. **τ²-Bench 5-trial run** — run with `NUM_TRIALS=5` for a proper reproduction (budget ~$1.50)
2. **Cloud deployment** — Railway or Render, replace ngrok with permanent webhook URL
3. **Langfuse cost attribution** — inject OpenRouter pricing table into `emit_span` metadata so cost is visible in traces
4. **Live outbound test** — with `KILL_SWITCH_LIVE_OUTBOUND=true` and program-staff approval

---

## Development Notes and Past Fixes

These are things that burned time and should be remembered:

**uvicorn "No module named 'agent'"**
Must launch from inside `The conversion Engine/` directory, not from `week 10/`. Command:
```bash
uvicorn agent.main:app --reload
```
If that doesn't work: `uvicorn agent.main:app --app-dir "The conversion Engine" --reload`

**`curl` in PowerShell fails**
PowerShell's `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe` explicitly.

**Resend email_status: error (duplicate env var)**
If `.env` has `OUTBOUND_SINK_EMAIL` defined twice (once near top, once near bottom), dotenv uses the FIRST occurrence. The Gmail address at line 12 (if present) shadows the correct sink address. Check with `grep OUTBOUND_SINK_EMAIL .env`.

**HubSpot `contact_id: unknown`**
Means the custom properties (`crunchbase_id`, `ai_maturity_score`, etc.) were not created in the HubSpot portal. Go to Settings → Properties → Create property for each one (single-line text type is fine for all of them).

**tau2-bench `No module named 'toml'` or `'addict'`**
tau2 has extra dependencies not in the main requirements.txt. Install:
```bash
pip install addict litellm tenacity deepdiff rich tabulate docstring-parser PyYAML toml audioop-lts
```

**GitHub push blocked GH013 (API key in file)**
If a push is rejected for secrets, check `handover.md` — it previously contained raw API keys. Use a Python script to replace all `sk-`, `re_`, `pat-na1-` prefixed values with `_REDACTED`, then `git commit --amend --no-edit` and force push.

---

## Workflow Pattern Used in This Project

This project used a **Gemini → Claude** workflow:
1. User pastes a Gemini-generated implementation prompt into Claude
2. Claude implements it while cross-referencing `todos.md` for items Gemini may have missed
3. Standing instruction: **"Even if Gemini did not include the specific todos in the prompt, check todos.md and implement anything missing."**

The todos.md file tracks all tasks from Group A through J with `[ ]` and `[x]` markers.

---

*Last updated: April 23, 2026 — after scoring 100/100 on the interim submission.*
*All Groups A–J complete. Next session should start with Act III (job scraper SerpAPI fallback).*
