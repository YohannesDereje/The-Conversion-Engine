# The Conversion Engine for Tenacious Consulting

This project is an automated, production-grade lead generation and conversion system built for Tenacious Consulting. It uses an AI agent to perform research-first outbound prospecting, qualify leads, and book discovery calls.

## Architecture

The system is a FastAPI application that orchestrates a multi-step enrichment and outreach pipeline.

```
[Lead Request] -> [FastAPI: /leads/process]
       |
       +--> [Enrichment Pipeline]
       |      |
       |      +--> Crunchbase Lookup
       |      +--> Layoffs.fyi Check
       |      +--> Live Job Post Scraping
       |      +--> AI Maturity Scoring (LLM)
       |      +--> Competitor Gap Analysis (LLM)
       |
       +--> [Agent Core (LLM)]
       |      |
       |      +--> Applies business logic & constraints
       |      +--> Composes personalized email
       |
       +--> [Integration Handlers]
              |
              +--> HubSpot: Create/Update Contact
              +--> Resend: Send Email
```

## Tech Stack & Status

| Component | Status |
|---|---|
| Web Framework (FastAPI) | OK |
| LLM Provider (OpenRouter — qwen/qwen3-235b-a22b) | OK |
| Observability (Langfuse) | OK |
| Email (Resend) | OK |
| SMS (Africa's Talking) | OK |
| CRM (HubSpot) | OK |
| Calendar (Cal.com) | OK |
| Evaluation (τ²-Bench) | Implemented, pending results |

## Setup Instructions

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YohannesDereje/The-Conversion-Engine.git
   cd The-Conversion-Engine
   ```

2. **Create and populate `.env`:** Create a `.env` file in the root directory and fill in all required API keys (see `.env` section below).

3. **Install dependencies:**
   ```bash
   pip install -r agent/requirements.txt
   playwright install
   ```

## Required .env Keys

```
KILL_SWITCH_LIVE_OUTBOUND=false
OUTBOUND_SINK_EMAIL=your@email.com
OUTBOUND_SINK_SMS=+10000000000
OPENROUTER_API_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://cloud.langfuse.com
RESEND_API_KEY=...
RESEND_FROM_EMAIL=onboarding@resend.dev
HUBSPOT_ACCESS_TOKEN=...
CALCOM_API_KEY=...
CALCOM_BASE_URL=https://api.cal.com
CALCOM_EVENT_TYPE_ID=...
AFRICAS_TALKING_API_KEY=...
AFRICAS_TALKING_USERNAME=sandbox
```

## Kill-Switch Documentation

**CRITICAL:** The system includes a kill switch for all outbound communication.

- `KILL_SWITCH_LIVE_OUTBOUND=false` (default) — every email and SMS is redirected to `OUTBOUND_SINK_EMAIL` / `OUTBOUND_SINK_SMS`. No real prospect is ever contacted.
- `KILL_SWITCH_LIVE_OUTBOUND=true` — live outbound. Only set with explicit program staff approval.

## How to Run

**Start the API server:**
```bash
uvicorn agent.main:app --reload
```

**Process a lead (example):**
```bash
curl.exe -X POST http://127.0.0.1:8000/leads/process \
  -H "Content-Type: application/json" \
  -d '{"company_name":"Stripe","contact_email":"test@sink.com","contact_name":"Test User"}'
```

**Health check:**
```bash
curl.exe http://127.0.0.1:8000/health
```

## How to Run τ²-Bench Harness

```bash
# Prerequisites: tau2-bench cloned, OPENROUTER_API_KEY in .env
python eval/tau2_bench_runner.py
```

Results are appended to `eval/score_log.json` and `eval/trace_log.jsonl`.

## Synthetic Load Test (20 leads)

```bash
# Start API first, then in a second terminal:
python scripts/generate_synthetic_interactions.py
```

Reports p50/p95 latency across 20 companies from the Crunchbase dataset.
