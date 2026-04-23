# Conversion Engine — Session Handover Document

**Project:** Tenacious Consulting Conversion Engine (10Academy Week 10 challenge)
**Handover written:** 2026-04-23 ~20:00 UTC
**Interim submission deadline:** 2026-04-25 21:00 UTC (~48 hours from handover)
**Working directory:** `c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10\The conversion Engine`
**Codebase is NOT a git repo yet** — no commits have been made.

---

## 1. What This Project Is

An automated B2B outbound lead-generation system for Tenacious Consulting and Outsourcing.
The system finds engineering-leader prospects, enriches them with hiring signals and competitor
analysis, composes personalised outreach emails, sends them via Resend, logs contacts to HubSpot,
and books discovery calls via Cal.com. All LLM calls use **Qwen3-235B via OpenRouter** at
temperature 0.0 and are traced to Langfuse.

**Kill switch:** `KILL_SWITCH_LIVE_OUTBOUND=false` in `.env` — ALL outbound must route to the
sink addresses while this is false. Never change this without explicit staff approval.

---

## 2. How to Continue (Workflow)

The user generates Gemini prompts for each Group, pastes them here, and the Claude session
implements them. **Always cross-reference `todos.md`** — Gemini sometimes omits specific
todos. If a todo exists in `todos.md` but was not in the Gemini prompt, implement it anyway.

To resume: `cd "c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10\The conversion Engine"`
then run `python -m agent.enrichment.pipeline --company "Stripe"` to confirm the pipeline
still works.

---

## 3. Completed Groups (A–E)

### Group A — Environment (manual setup, all done)
- A1: HubSpot private app created; `HUBSPOT_ACCESS_TOKEN=pat-eu1-37460b4f...` in `.env`; `HUBSPOT_PORTAL_ID=148328985`
- A2: Cal.com Cloud (free tier, not self-hosted); `CALCOM_API_KEY=cal_live_373a554a...`; `CALCOM_BASE_URL=https://api.cal.com`; `CALCOM_EVENT_TYPE_ID=5467186`
- A3: ngrok tunnel configured; `AFRICAS_TALKING_WEBHOOK_URL=https://amendment-evergreen-abrasive.ngrok-free.dev/sms/webhook`
- A4: `data/layoffs_fyi.csv` — 4,361 rows, 11 columns, scraped from layoffs.fyi via Playwright route interception of Airtable embed
- A5: Resend `FROM` email is `onboarding@resend.dev` (sandbox)

### Group B — Project Structure (done)
- `agent/`, `agent/enrichment/`, `eval/`, `infra/`, `scripts/` directories created
- All `__init__.py` files created
- `agent/requirements.txt` created (see Section 6 below)

### Group C — Data Layer (done)
- `agent/enrichment/crunchbase_enricher.py` — fuzzy match against Crunchbase CSV, threshold 85
- `agent/enrichment/layoffs_enricher.py` — fuzzy match against layoffs_fyi.csv, threshold 85

**Known Crunchbase CSV limitation:** The CSV (`data/crunchbase-companies-information.csv`) is
a 1,000-row ODM sample of obscure/smaller companies. Well-known companies like Stripe, OpenAI,
and Shopify score ~65-75 (below the 85 threshold) and return `{"error": "Company not found"}`.
This is expected. The pipeline handles it gracefully — enrichment proceeds with whatever
data is available, and the LLM works from what it knows.

### Group D — Enrichment Pipeline (done)

| File | Status | Notes |
|------|--------|-------|
| `agent/enrichment/job_post_scraper.py` | Done | Async Playwright scraper; tries Wellfound + company careers page; graceful `{"status": "no_data"}` fallback |
| `agent/enrichment/ai_maturity_scorer.py` | Done | Qwen3 via OpenRouter; scores 0-3; all 6 schema signal enums; Langfuse generation span |
| `agent/enrichment/competitor_gap_builder.py` | Done | Finds 5-10 sector peers from Crunchbase CSV; batch-scores AI maturity; generates 1-3 gap findings |
| `agent/enrichment/pipeline.py` | Done | Async orchestrator; Langfuse v4 trace proxy; segment classifier; schema validation; D5 smoke test via `--company` arg |

**Smoke test command:**
```bash
cd "c:\Users\Yohannes\Desktop\tenx education\Weeks\week 10\The conversion Engine"
python -m agent.enrichment.pipeline --company "Stripe" --out-dir "."
```
Produces `hiring_signal_brief.json` and `competitor_gap_brief.json` in the project root.
Both validate against `tenacious_sales_data/schemas/*.schema.json`.

**Langfuse version:** v4.5.0 — the old `langfuse.trace()` API is gone. Use the compatibility
proxy in `pipeline.py` (`_LangfuseTraceProxy`, `_make_trace`, etc.). The pattern is:
```python
lf = _get_langfuse()
trace_id = lf.create_trace_id()
from langfuse.types import TraceContext
tc = TraceContext(trace_id=trace_id, name="my_trace")
obs = lf.start_observation(trace_context=tc, name="step", as_type="generation", model="...", input=...)
obs.update(output=response_text)
obs.end()
lf.flush()
```

### Group E — Agent Core (done)

**File:** `agent/agent_core.py`

**Main function:** `async def compose_outreach(hiring_signal_brief, competitor_gap_brief, conversation_history=None) -> dict`

**Returns:**
```python
{
    "email_to_send":       str,      # full email with subject on line 1
    "icp_segment":         int|str,  # 1, 2, 3, 4, or "abstain"
    "llm_confidence":      float,    # 0.0–1.0 from LLM
    "bench_match_result":  {"match": bool, "missing_skills": list},
    "decision_override":   bool,     # True if any Python rule fired
    "langfuse_trace_id":   str,
}
```

**System prompt reads at startup:**
- `tenacious_sales_data/seed/style_guide.md` — 5 tone markers (Direct, Grounded, Honest, Professional, Non-condescending)
- `tenacious_sales_data/seed/icp_definition.md` — 4 ICP segment definitions + classification rules
- `tenacious_sales_data/seed/bench_summary.json` — current engineering capacity
- `tenacious_sales_data/seed/email_sequences/cold.md` — 3-email sequence structure, subject patterns, per-segment body format

**E2 business rules enforced in Python after LLM call (not delegated to model):**
1. `segment_confidence < 0.6` → override `icp_segment` to `"abstain"`
2. `icp_segment == 4` AND `ai_maturity.score < 2` → override to `"abstain"`
3. `bench_to_brief_match.bench_available == False` (from HSB) OR LLM's `required_skills` maps to 0-availability stacks → `decision_override = True`
4. LLM call fails → fallback generic email, `decision_override = True`

**E3 routing:** `cold.md` is embedded in the system prompt. The LLM receives the exact
4-sentence body structure, subject line patterns per segment (`Context:` for Seg 1,
`Note on` for Seg 2, `Congrats on the` for Seg 3, `Question on` for Seg 4), and
per-segment adjustments.

---

## 4. Remaining Groups (F–J) — What Needs to Be Built

### Group F — Integration Handlers (~1.5 hours) ← START HERE

#### F1: `agent/email_handler.py`
```python
def send_email(to: str, subject: str, body: str, trace_id: str) -> dict
def handle_reply_webhook(payload: dict) -> dict
```
- Use `resend` Python package (`import resend`)
- `resend.api_key = os.getenv("RESEND_API_KEY")`
- `KILL_SWITCH_LIVE_OUTBOUND`: if `os.getenv("KILL_SWITCH_LIVE_OUTBOUND") != "true"`, send to `os.getenv("OUTBOUND_SINK_EMAIL")` instead of the real `to` address
- `RESEND_FROM_EMAIL = onboarding@resend.dev`
- `handle_reply_webhook` parses the Resend inbound webhook payload: extract `reply_text`, `thread_id`, `from_email`

#### F2: `agent/sms_handler.py`
```python
def send_sms(to: str, message: str, trace_id: str) -> dict
def handle_inbound_webhook(payload: dict) -> dict
```
- Use `africastalking` Python package
- Init: `africastalking.initialize(username, api_key)`, then `sms = africastalking.SMS`
- `KILL_SWITCH_LIVE_OUTBOUND`: if not `"true"`, send to `os.getenv("OUTBOUND_SINK_SMS")` instead
- `AFRICAS_TALKING_USERNAME = sandbox`, `AFRICAS_TALKING_API_KEY = atsk_b8aeaa...`
- `handle_inbound_webhook`: extract `sender`, `message`, `date`

#### F3: `agent/hubspot_handler.py`
```python
def create_or_update_contact(contact_data: dict, trace_id: str) -> str  # returns HubSpot contact ID
def log_email_activity(contact_id: str, email_data: dict) -> None
def log_meeting_booked(contact_id: str, cal_event_data: dict) -> None
```
- Use `hubspot-api-client` package (`from hubspot import HubSpot`)
- `client = HubSpot(access_token=os.getenv("HUBSPOT_ACCESS_TOKEN"))`
- Required contact fields: `firstname`, `lastname`, `email`, `company`, `hs_lead_status`,
  `industry`, `crunchbase_id`, `ai_maturity_score`, `icp_segment`, `enrichment_timestamp`,
  `hiring_signal_brief` (JSON string of the full brief)
- Use `client.crm.contacts.basic_api.create()` or `update()` with upsert by email
- `log_email_activity`: create engagement of type EMAIL on the contact
- `log_meeting_booked`: create engagement of type MEETING on the contact

#### F4: `agent/calcom_handler.py`
```python
async def get_available_slots(days_ahead: int = 7) -> list
async def book_slot(slot_datetime: str, attendee_email: str, attendee_name: str, discovery_call_context_brief: dict) -> dict
```
- Use `httpx` for async HTTP (`import httpx`)
- `CALCOM_BASE_URL = https://api.cal.com`, `CALCOM_API_KEY = cal_live_373a554a...`
- Cal.com Cloud API v2 — NOT v1 (v1 is decommissioned)
- `get_available_slots`: GET `{CALCOM_BASE_URL}/v2/slots?eventTypeId={CALCOM_EVENT_TYPE_ID}&startTime=...&endTime=...`
  Auth: header `Authorization: Bearer {CALCOM_API_KEY}`
- `book_slot`: POST `{CALCOM_BASE_URL}/v2/bookings`
  Body: `{"eventTypeId": CALCOM_EVENT_TYPE_ID, "start": slot_datetime, "attendee": {"name": ..., "email": ..., "timeZone": "UTC"}, "metadata": {"discovery_context": json.dumps(discovery_call_context_brief)}}`
- Attach `discovery_call_context_brief` as booking notes/metadata

#### F5: Langfuse tracing in ALL handlers
Every public function in F1–F4 must:
- Accept `trace_id: str` parameter (already in signatures above)
- Emit a Langfuse span using the same `_LangfuseTraceProxy` or direct `start_observation()` pattern from `pipeline.py`
- Record: `name`, `input`, `output`, `latency_ms`

### Group G — FastAPI App (~30 min)

#### G1: `agent/main.py`
```python
@app.post("/leads/process")
async def process_lead(body: LeadRequest) -> dict:
    # LeadRequest: company, contact_email, contact_name
    # Full pipeline: enrich → agent → email → HubSpot
    # Returns: trace_id, icp_segment, email_sent_to, hubspot_contact_id

@app.post("/email/webhook")
async def email_webhook(request: Request) -> dict:
    # Resend reply webhook handler

@app.post("/sms/webhook")
async def sms_webhook(request: Request) -> dict:
    # Africa's Talking inbound webhook

@app.get("/health")
async def health() -> dict:
    # Returns status of all components
```

Run with: `uvicorn agent.main:app --host 0.0.0.0 --port 8000 --reload`

#### G2: End-to-end test
```bash
curl -X POST http://localhost:8000/leads/process \
  -H "Content-Type: application/json" \
  -d '{"company": "Stripe", "contact_email": "test@sink.com", "contact_name": "Test User"}'
```
Verify: Langfuse trace appears, HubSpot contact created, email logged to sink, no errors.

#### G3: 20 synthetic interactions
`scripts/generate_synthetic_interactions.py` — loads 20 companies from Crunchbase CSV,
POSTs each to `/leads/process`, records p50/p95 latency.

### Group H — τ²-Bench Harness (~1 hour)

#### H1–H5: `eval/tau2_bench_runner.py`
- Ask user where tau2-bench is cloned on their machine
- Wraps τ²-Bench retail domain runner
- Uses `qwen/qwen3-235b-a22b` via OpenRouter, temperature 0.0
- 5 trials × 30-task dev slice
- Records `pass@1`, mean, Wilson 95% CI, cost_usd, p50/p95 latency per trial
- Writes `eval/score_log.json` and `eval/trace_log.jsonl`

### Group I — Documentation (~45 min)

#### I1–I4:
- `baseline.md` (max 400 words): what was reproduced, CI result, cost/run, latency, surprises
- `README.md` at repo root: architecture ASCII diagram, stack status, setup instructions,
  kill-switch docs, uvicorn run command, τ²-Bench harness instructions
- `.gitignore`: must exclude `.env`, `__pycache__`, `*.pyc`, `*.bin`, `data/*.csv`
- Push to GitHub (`git init`, add remote, push)

### Group J — PDF Interim Report (~30 min)

`interim_report.md` → convert to PDF via pandoc or similar.
Required sections:
1. Architecture overview + design decisions
2. Stack status (Resend, Africa's Talking, HubSpot, Cal.com, Langfuse, τ²-Bench)
3. Enrichment pipeline status (all 5 signals)
4. Competitor gap brief status (at least 1 test prospect)
5. τ²-Bench baseline score + methodology
6. p50/p95 latency from 20 interactions
7. What's working, what's not, plan for remaining days

---

## 5. Critical Architecture Details

### LLM Setup (OpenRouter + OpenAI client)
```python
from openai import AsyncOpenAI
client = AsyncOpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url=os.getenv("OPENROUTER_BASE_URL"),  # https://openrouter.ai/api/v1
)
# Model: qwen/qwen3-235b-a22b, temperature: 0.0, max_tokens varies per call
```
The `anthropic` SDK is NOT used for the dev-tier model. `ANTHROPIC_API_KEY` is still a
placeholder — it's only needed for the eval tier (Groups H eval runs, not implemented yet).

### Langfuse v4.5.0 API (breaking change from v2/v3)
There is NO `langfuse.trace()` method. Use the pattern below consistently:
```python
from langfuse import Langfuse
from langfuse.types import TraceContext

lf = Langfuse(public_key=..., secret_key=..., host=...)
trace_id = lf.create_trace_id()
tc = TraceContext(trace_id=trace_id, name="my_operation")

# For an LLM generation:
obs = lf.start_observation(trace_context=tc, name="llm_call", as_type="generation",
                            model=model_name, input=messages,
                            usage_details={"input": prompt_tokens, "output": completion_tokens})
obs.update(output=response_text)
obs.end()

# For a non-LLM step:
obs = lf.start_observation(trace_context=tc, name="db_lookup", as_type="span", input=query)
obs.update(output=result)
obs.end()

lf.flush()  # must call at end of request
```
The `_LangfuseTraceProxy` class in `pipeline.py` wraps this pattern with a v2-compatible
`.generation()` / `.span()` / `.update()` interface and can be reused in new handlers.

### Kill Switch Pattern (must be consistent across all outbound handlers)
```python
import os
KILL_SWITCH = os.getenv("KILL_SWITCH_LIVE_OUTBOUND", "false").lower() == "true"
SINK_EMAIL   = os.getenv("OUTBOUND_SINK_EMAIL", "staff-sink@program.com")
SINK_SMS     = os.getenv("OUTBOUND_SINK_SMS", "+10000000000")

def send_email(to, subject, body, trace_id):
    actual_to = to if KILL_SWITCH else SINK_EMAIL
    # ... resend call with actual_to ...
```

### Segment Classification Rules (from `icp_definition.md`)
Priority order — first match wins:
1. Layoff in last 120 days AND fresh funding/large headcount → **segment_2**
2. New CTO/VP Eng in last 90 days → **segment_3** (not detectable from current data)
3. Specialized capability + AI maturity ≥ 2 → **segment_4**
4. Fresh Series A/B funding → **segment_1**
5. Otherwise → **abstain** (generic exploratory email)

**Segment 4 hard gate:** `ai_maturity.score < 2` → always override to abstain (Python enforced).
**Low confidence gate:** `segment_confidence < 0.6` → always override to abstain (Python enforced).

---

## 6. File Inventory

### Existing and working
```
agent/
  __init__.py
  agent_core.py              ← Group E (compose_outreach)
  requirements.txt
  enrichment/
    __init__.py
    crunchbase_enricher.py   ← Group C (enrich)
    layoffs_enricher.py      ← Group C (check_layoffs)
    job_post_scraper.py      ← Group D1 (scrape_job_postings)
    ai_maturity_scorer.py    ← Group D2 (score_ai_maturity)
    competitor_gap_builder.py← Group D3 (build_competitor_gap)
    pipeline.py              ← Group D4+D5 (run_enrichment_pipeline)

data/
  layoffs_fyi.csv            ← 4,361 rows, 11 cols (Company, Date, # Laid Off, %, Source, ...)
  crunchbase-companies-information.csv  ← 1,000 rows, 92 cols (name, uuid, website, industries, ...)
  decode_json.py             ← Script that built layoffs_fyi.csv from raw Airtable response

eval/
  __init__.py

infra/
  docker-compose.yml         ← Cal.com + Postgres (not used — switched to Cal.com Cloud)

scripts/
  __init__.py

tenacious_sales_data/
  seed/
    icp_definition.md        ← ICP segment definitions (used in agent system prompt)
    style_guide.md           ← 5 tone markers (used in agent system prompt)
    bench_summary.json       ← Engineering capacity (used in agent + pipeline)
    email_sequences/
      cold.md                ← 3-email sequence structure (used in agent system prompt E3)
      warm.md
      reengagement.md
    baseline_numbers.md
    case_studies.md
    pricing_sheet.md
    sales_deck_notes.md
    discovery_transcripts/   ← 5 transcripts (context, not yet used in code)
  schemas/
    hiring_signal_brief.schema.json
    competitor_gap_brief.schema.json
    discovery_call_context_brief.md
  policy/
    data_handling_policy.md
    acknowledgement.md

.env                         ← All API keys (see Section 7)
handover.md                  ← This file
hiring_signal_brief.json     ← Sample output from D5 smoke test (Stripe)
competitor_gap_brief.json    ← Sample output from D5 smoke test (Stripe)
todos.md                     ← Full task list (Groups A–J)
```

### Not yet created (F–J)
```
agent/
  main.py                    ← Group G (FastAPI app)
  email_handler.py           ← Group F1
  sms_handler.py             ← Group F2
  hubspot_handler.py         ← Group F3
  calcom_handler.py          ← Group F4

eval/
  tau2_bench_runner.py       ← Group H2
  score_log.json             ← Group H3 (generated by runner)
  trace_log.jsonl            ← Group H4 (generated by runner)

scripts/
  generate_synthetic_interactions.py  ← Group G3

baseline.md                  ← Group I1
README.md                    ← Group I2
.gitignore                   ← Group I3
interim_report.md            ← Group J1
```

---

## 7. Full .env Contents (current, confirmed working)

```env
KILL_SWITCH_LIVE_OUTBOUND=false
OUTBOUND_SINK_EMAIL=staff-sink@program.com
OUTBOUND_SINK_SMS=+10000000000

OPENROUTER_API_KEY=sk-or-REDACTED
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEV_MODEL=qwen/qwen3-235b-a22b
DEV_MODEL_TEMPERATURE=0.0

ANTHROPIC_API_KEY=your_anthropic_api_key_here
EVAL_MODEL=claude-sonnet-4-6
EVAL_MODEL_TEMPERATURE=0.0

LANGFUSE_PUBLIC_KEY=pk-lf-REDACTED
LANGFUSE_SECRET_KEY=sk-lf-REDACTED
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PROJECT=conversion-engine

HUBSPOT_ACCESS_TOKEN=pat-REDACTED
HUBSPOT_PORTAL_ID=148328985

RESEND_API_KEY=re_REDACTED
RESEND_FROM_EMAIL=onboarding@resend.dev
RESEND_REPLY_WEBHOOK_SECRET=your_webhook_secret_here

AFRICAS_TALKING_API_KEY=atsk_REDACTED
AFRICAS_TALKING_USERNAME=sandbox
AFRICAS_TALKING_SHORTCODE=1217
AFRICAS_TALKING_WEBHOOK_URL=https://amendment-evergreen-abrasive.ngrok-free.dev/sms/webhook

CALCOM_API_KEY=cal_REDACTED
CALCOM_BASE_URL=https://api.cal.com
CALCOM_EVENT_TYPE_ID=5467186
CALCOM_SDR_EMAIL=sdr@tenacious.com

CRUNCHBASE_DATA_PATH=./data/crunchbase_odm_sample.csv
LAYOFFS_DATA_PATH=./data/layoffs_fyi.csv

TAU2_BENCH_MODEL=qwen/qwen3-235b-a22b
TAU2_BENCH_TEMPERATURE=0.0
TAU2_BENCH_DOMAIN=retail
TAU2_BENCH_DEV_SLICE_SIZE=30
TAU2_BENCH_HELD_OUT_SLICE_SIZE=20
TAU2_BENCH_TRIALS=5

APP_ENV=development
LOG_LEVEL=INFO
TRACE_OUTPUT_PATH=./eval/trace_log.jsonl
SCORE_LOG_PATH=./eval/score_log.json
```

---

## 8. Known Issues and Gotchas

### Issue 1: layoffs_enricher.py has a latent bug (line 46)
```python
if company_rows.empty:
    company_rows = df.iloc[[idx]]  # BUG: `idx` is not defined in this scope
```
This line never runs in practice (fuzzy match finds exact name rows), but it will crash if
triggered. Safe to ignore for now; fix by removing the `if company_rows.empty:` block entirely.

### Issue 2: Crunchbase CSV is a random sample of small companies
The 1,000-row CSV (`crunchbase-companies-information.csv`) does not contain Stripe, OpenAI,
Shopify, or other well-known companies. Fuzzy match returns `match_score ~65-75` which is
below the 85 threshold. This is expected behaviour — the enrichment pipeline still works
because the AI maturity scorer and competitor gap builder operate from the LLM's knowledge.

### Issue 3: Cal.com is Cloud, not self-hosted
The `infra/docker-compose.yml` exists for a self-hosted Cal.com attempt that was abandoned.
The working setup uses `CALCOM_BASE_URL=https://api.cal.com` (Cal.com Cloud free tier).
The API key format is `cal_live_...`. Use API v2 (`/v2/slots`, `/v2/bookings`).
API v1 is decommissioned on Cal.com Cloud.

### Issue 4: Playwright must be installed for job scraper
If `playwright install chromium` has not been run, the job scraper will fail (returning
`{"status": "error", ...}`). The pipeline handles this gracefully. Run:
`python -m playwright install chromium`

### Issue 5: Windows console encoding
The project runs on Windows. Use ASCII characters (not →, ✓, etc.) in print statements
inside Python scripts or they will crash with `UnicodeEncodeError` in CP1252 terminal.

### Issue 6: Langfuse credentials are real and in .env
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are the real keys for `cloud.langfuse.com`.
Traces appear in real-time at `https://cloud.langfuse.com` — confirm this is the expected
account before running large-scale tests.

---

## 9. Key Design Decisions (for report writing)

1. **OpenRouter not Anthropic for dev:** Cost target < $4 for Days 1–4. Qwen3-235B-A22B
   gives strong JSON compliance at $0 effective cost via OpenRouter's free tier.

2. **Langfuse for observability:** Every LLM call emits a trace. Required for grading
   (τ²-Bench traces + enrichment pipeline traces must appear in Langfuse).

3. **Kill switch routes to sink, not skip:** Even with kill switch off, all outbound sends
   a real email/SMS — just to the staff sink address. This proves the send path works
   without emailing real prospects.

4. **JSON schema validation on both briefs:** `jsonschema.validate()` is called on every
   pipeline output. Validation errors are traced but do not raise — the pipeline returns
   whatever it built, and the calling code decides whether to proceed.

5. **Bench check in two places:** The pipeline infers tech stack from job titles and checks
   it at enrichment time (`bench_to_brief_match` in HSB). The agent core does a second check
   against the LLM's `required_skills`. Both must pass for `bench_match_result.match = True`.

6. **Segment 4 hard gate:** The ICP definition requires AI maturity ≥ 2 for Segment 4.
   This is enforced in Python in `agent_core.py` after the LLM call — the LLM cannot
   override it.

---

## 10. Recommended Order for Remaining Time

Given ~48 hours to deadline, prioritise in this order:

1. **F1 (email_handler.py)** — required for G2 end-to-end test
2. **F3 (hubspot_handler.py)** — required for G2 end-to-end test
3. **G1 (main.py FastAPI)** — required for G2 end-to-end test
4. **G2 (end-to-end test)** — required for interim report credibility
5. **F4 (calcom_handler.py)** — book slot is required for the full sales flow
6. **F2 (sms_handler.py)** — secondary channel, simpler than email
7. **G3 (synthetic interactions)** — generates p50/p95 latency numbers for report
8. **H (τ²-Bench harness)** — requires knowing where tau2-bench is cloned on the machine
9. **I1–I4 (docs + git push)** — must push to GitHub before deadline
10. **J (PDF report)** — final step

**Minimum viable submission:** G2 working end-to-end + at least one run of τ²-Bench + README + git push.

---

*End of handover document.*
