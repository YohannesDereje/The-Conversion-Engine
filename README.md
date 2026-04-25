# Tenacious Conversion Engine

A production-grade AI-driven outbound prospecting system built for Tenacious Consulting. The engine classifies B2B prospects into four ICP segments using live enrichment signals (layoffs, funding rounds, leadership changes, AI maturity scores), generates honesty-constrained cold emails via a Python-enforced post-generation scan, routes warm replies through automated classification or human handoff, and orchestrates full multi-step sequences including re-engagement — all while maintaining strict per-contact thread isolation and a safe default kill-switch that routes all outbound to a sink until explicitly enabled.

---

## Architecture

```
[POST /leads/process]
        │
        ▼
[Enrichment Pipeline — agent/enrichment/]
        ├── crunchbase_enricher.py     (funding, headcount)
        ├── layoffs_enricher.py        (layoff events ≤120d)
        ├── job_scraper.py             (live Playwright job board scrape)
        ├── ai_maturity_scorer.py      (0–3 score via LLM)
        ├── competitor_gap_analyzer.py (sector gap brief via LLM)
        └── pipeline.py                (_classify_segment → segment + confidence)
        │
        ▼
[Agent Core — agent/agent_core.py]
        ├── 6 honesty rules (Python-enforced; 3 post-generation overrides)
        ├── Mechanism: signal-confidence-aware phrasing scan (Phase 5)
        │     └── _ASSERTIVE_CLAIM_RE + up to 2 LLM regen attempts
        ├── compose_outreach()        (cold email — 3-email sequence)
        ├── compose_engaged_reply()
        └── compose_discovery_call_brief()
        │
        ▼
[Tone Probe — agent/tone_probe.py]
        └── 5-marker rubric (direct, grounded, honest, professional, non-condescending)
        │
        ▼
[Integrations]
        ├── HubSpot  — agent/hubspot_handler.py   (create/update contact + notes)
        ├── Resend   — agent/email_handler.py      (send email / sink routing)
        ├── Cal.com  — agent/calcom_handler.py     (next available slot injection)
        ├── Africa's Talking — agent/sms_handler.py (SMS ≤160 chars enforced)
        └── Langfuse — inline spans in agent_core.py (observability + cost tracking)
        │
        ▼
[Reply Pipeline — POST /email/webhook]
        ├── agent/reply_classifier.py   (6 classes: engaged/curious/hard_no/soft_defer/objection/ambiguous)
        ├── agent/reply_composer.py     (detect_handoff_triggers → 5 conditions)
        └── HubSpot status update
        │
        ▼
[Re-engagement — POST /leads/reengage]
        └── agent/reengagement_composer.py  (3-email reengage sequence; 4 eligibility conditions)
```

---

## Five-Act Completion Status

| Act | Phase | Description | Status |
|-----|-------|-------------|--------|
| Act I | Phase 1 + 2 | Agent core, FastAPI, enrichment pipeline, HubSpot/Resend/Cal.com integrations, ICP classifier, 6 honesty rules, smoke tests | ✅ Complete (scored 100/100, Apr 23 2026) |
| Act II | Phase 3 | Reply classification (6 classes), warm reply pipeline, human handoff (5 triggers), multi-thread safeguard, HubSpot status machine | ✅ Complete |
| Act III | Phase 3 cont. | Re-engagement composer (3-email sequence), SMS scheduling (≤160 char), cal.com booking webhook, Langfuse cost tracking | ✅ Complete |
| Act IV | Phase 4 | 30-probe adversarial suite (ICP-*, BOC-*, MTL-*, SOC-*, SR-*, SE-*, DCC-*, HH-*), τ²-Bench harness, failure taxonomy, target failure mode, method.md | ✅ Complete (28/29 probes pass; 3 skip; 1 genuine fail SOC-01) |
| Act V | Phase 5 + 6 | Signal-confidence-aware phrasing mechanism (SOC-01 FAIL→PASS), held-out evaluation (pass@1=0.4000, n=20), ablation statistics, decision memo, evidence graph | ✅ Complete |

---

## HubSpot Custom Properties

These must be created in your HubSpot portal as Contact properties before running the system. All are type `single_line_text` unless noted.

| Property Name | Type | Description |
|---|---|---|
| `ai_maturity_score` | `single_line_text` | 0–3 integer score from ai_maturity_scorer.py |
| `icp_segment` | `single_line_text` | segment_1_series_a_b / segment_2_mid_market_restructure / segment_3_leadership_transition / segment_4_specialized_capability / abstain |
| `enrichment_timestamp` | `single_line_text` | ISO 8601 timestamp of last enrichment run |
| `hiring_signal_brief` | `single_line_text` (long) | JSON-serialised hiring brief (velocity_label, open_roles, signals) |
| `tenacious_status` | `single_line_text` | Always written as `draft` (Rule 6 compliance — human must promote) |
| `outreach_sequence_step` | `single_line_text` | 0 / 1 / 2 / 3 / reengage_1 / reengage_2 / reengage_3 / hard_no |
| `outreach_last_sent_at` | `single_line_text` | ISO 8601 timestamp of last outbound email |

Standard HubSpot properties also written: `firstname`, `lastname`, `email`, `company`, `hs_lead_status` (NEW / IN_PROGRESS / UNQUALIFIED / DISQUALIFIED).

---

## Setup

### 1. Install dependencies

```bash
pip install -r agent/requirements.txt
playwright install chromium
```

### 2. Configure `.env`

Copy and populate the following in a `.env` file at the project root:

```env
# ── KILL SWITCH (safe default: routes all outbound to sink) ──────────────────
KILL_SWITCH_LIVE_OUTBOUND=false
OUTBOUND_SINK_EMAIL=your-test-inbox@example.com
OUTBOUND_SINK_SMS=+10000000000

# ── LLM ─────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY=sk-or-...

# ── OBSERVABILITY ────────────────────────────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com

# ── EMAIL ────────────────────────────────────────────────────────────────────
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=onboarding@resend.dev

# ── CRM ──────────────────────────────────────────────────────────────────────
HUBSPOT_ACCESS_TOKEN=pat-na1-...

# ── CALENDAR ─────────────────────────────────────────────────────────────────
CALCOM_API_KEY=cal_...
CALCOM_BASE_URL=https://api.cal.com
CALCOM_EVENT_TYPE_ID=12345

# ── SMS ──────────────────────────────────────────────────────────────────────
AFRICAS_TALKING_API_KEY=atsk_...
AFRICAS_TALKING_USERNAME=sandbox

# ── MECHANISM TOGGLE (set false to ablate signal-confidence-aware phrasing) ──
MECHANISM_SIGNAL_AWARE_PHRASING=true
```

### 3. Cal.com setup

Create a 30-minute "Discovery Call" event type in your Cal.com account. Copy the numeric event type ID into `CALCOM_EVENT_TYPE_ID`. The agent will inject the next three available slots into every cold email.

### 4. Webhook tunnelling (local dev)

```bash
ngrok http 8000
```

Set the resulting HTTPS URL as the inbound email webhook in Resend (`<ngrok-url>/email/webhook`) and the booking webhook in Cal.com (`<ngrok-url>/calcom/webhook`).

---

## Kill-Switch Documentation

`KILL_SWITCH_LIVE_OUTBOUND=false` (default) — **every** email and SMS is redirected to `OUTBOUND_SINK_EMAIL` / `OUTBOUND_SINK_SMS`. No real prospect is ever contacted.

`KILL_SWITCH_LIVE_OUTBOUND=true` — live outbound. Only set with explicit program staff approval.

Automatic pause triggers (see `memo.html` §12):

| Metric | Threshold |
|---|---|
| `assertive_claim_regen_failed` flag | ≥ 3 leads in any 24h window |
| HubSpot DISQUALIFIED rate | ≥ 15% of outbound / 7-day window |
| Tone probe pass rate | < 80% / 7-day window |
| Human handoff rate | > 30% of engaged replies / 7-day window |

---

## How to Run

### Start the API server

```bash
uvicorn agent.main:app --reload
```

### API Endpoints

**`POST /leads/process`** — Full enrichment + cold email generation + HubSpot upsert
```bash
curl -X POST http://127.0.0.1:8000/leads/process \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Acme Corp",
    "contact_email": "cto@sink.com",
    "contact_name": "Jane Smith",
    "contact_title": "CTO"
  }'
```

**`POST /leads/followup`** — Send next email in sequence for an existing contact
```bash
curl -X POST http://127.0.0.1:8000/leads/followup \
  -H "Content-Type: application/json" \
  -d '{"contact_id": "12345"}'
```

**`POST /email/webhook`** — Inbound reply handler (Resend forwards here)
```bash
curl -X POST http://127.0.0.1:8000/email/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "from": "prospect@acmecorp.com",
    "to": "outbound@yourdomain.com",
    "subject": "Re: Your outreach",
    "text": "Interesting — tell me more about your ML team."
  }'
```

**`POST /leads/reengage`** — Trigger re-engagement sequence for an eligible stalled contact
```bash
curl -X POST http://127.0.0.1:8000/leads/reengage \
  -H "Content-Type: application/json" \
  -d '{"contact_id": "12345"}'
```

**`POST /sms/webhook`** — Inbound SMS handler (Africa's Talking forwards here)

**`POST /calcom/webhook`** — Cal.com booking confirmation handler (updates HubSpot)

**`GET /health`** — Liveness check
```bash
curl http://127.0.0.1:8000/health
```

---

## How to Run the τ²-Bench Harness

Prerequisites: clone [tau2-bench](https://github.com/tau-bench/tau2-bench) to `C:\Users\<you>\Desktop\tau2-bench` and set `OPENROUTER_API_KEY` in `.env`.

```bash
# Dev slice (30 tasks, repeatable)
python eval/tau2_bench_runner.py

# Sealed held-out slice (20 tasks, run ONCE only)
python eval/held_out_runner.py
```

Results are appended to `eval/score_log.json`. Per-task traces are written to `eval/trace_log.jsonl` (dev) or `probes/held_out_traces.jsonl` (held-out).

Latest results:

| Run | pass@1 | 95% CI | n / trials | Model |
|-----|--------|--------|-----------|-------|
| Held-out — mechanism ON (sealed) | **0.4000** | [0.22, 0.61] | 20 / 1 | qwen3-235b-a22b |
| Dev baseline — mechanism OFF proxy | 0.2667 | [0.14, 0.44] | 30 / 1 | qwen3-235b-a22b |
| Facilitator reference | 0.7267 | [0.65, 0.79] | 30 / 5 | qwen3-next-80b |

Delta A = +0.1333 (same-model comparison); p = 0.32 (not significant — n too small, CIs overlap).

---

## Synthetic Load Test

```bash
# Start API first, then in a second terminal:
python scripts/generate_synthetic_interactions.py
```

Runs 20 synthetic leads from the Crunchbase dataset. Reports p50/p95 latency. p50 = 83.12s; p95 = 156.76s (held-out run, qwen3-235b-a22b).

---

## Deliverables

| Deliverable | Path |
|---|---|
| FastAPI application entrypoint | `agent/main.py` |
| Agent core + honesty rules + mechanism | `agent/agent_core.py` |
| Enrichment pipeline + ICP classifier | `agent/enrichment/pipeline.py` |
| Reply classifier (6 classes) | `agent/reply_classifier.py` |
| Reply composer + handoff trigger detection | `agent/reply_composer.py` |
| Re-engagement composer (3-email sequence) | `agent/reengagement_composer.py` |
| Tone probe (5-marker rubric) | `agent/tone_probe.py` |
| HubSpot handler | `agent/hubspot_handler.py` |
| τ²-Bench dev runner | `eval/tau2_bench_runner.py` |
| τ²-Bench held-out runner (sealed) | `eval/held_out_runner.py` |
| Evaluation scores | `eval/score_log.json` |
| Dev trace log | `eval/trace_log.jsonl` |
| Held-out trace log | `probes/held_out_traces.jsonl` |
| Evidence graph (all numbered claims + sources) | `eval/evidence_graph.json` |
| Ablation results + statistics | `probes/ablation_results.json` |
| 30-probe adversarial probe library | `probes/probe_library.md` |
| Probe run results | `probes/probe_results.json` |
| Failure taxonomy (4 failure modes) | `probes/failure_taxonomy.md` |
| Target failure mode selection rationale | `probes/target_failure_mode.md` |
| Method document (Act III + IV — all sections) | `probes/method.md` |
| Decision memo (2-page PDF) | `memo.html` / `memo.pdf` |
| ICP definitions | `tenacious_sales_data/seed/icp_definition.md` |
| Style guide | `tenacious_sales_data/seed/style_guide.md` |
| Bench summary (36 engineers, 7 stacks) | `tenacious_sales_data/seed/bench_summary.json` |
| Baseline numbers (ACV, reply rates, benchmarks) | `tenacious_sales_data/seed/baseline_numbers.md` |
