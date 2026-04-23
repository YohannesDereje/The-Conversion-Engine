PHASE 1: INTERIM SUBMISSION

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP A — ENVIRONMENT (Manual by user, ~30 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] A1 — HubSpot private app created, access token in .env

[ ] A2 — Cal.com running on Docker localhost:3000, event type created, API key in .env

[ ] A3 — ngrok running on port 8000, webhook URLs in .env

[ ] A4 — layoffs_fyi.csv downloaded and in ./data/

[ ] A5 — Resend FROM email confirmed (sandbox or verified domain)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP B — PROJECT STRUCTURE (~15 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] B1 — Create directory tree: agent/, agent/enrichment/, eval/, infra/

[ ] B2 — Create agent/requirements.txt with all dependencies

[ ] B3 — Create all _init_.py files

[ ] B4 — Verify Python 3.11+ and pip install -r requirements.txt succeeds

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP C — DATA LAYER (~30 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] C1 — agent/enrichment/crunchbase_enricher.py

Reads CSV, fuzzy-matches company name → returns firmographics dict Output fields: name, domain, industry, employee_count, funding_total, last_funding_stage, last_funding_date, location, description, crunchbase_id
[ ] C2 — agent/enrichment/layoffs_enricher.py

Reads layoffs_fyi.csv, fuzzy-matches company name Returns: detected (bool), date, headcount_reduction, percentage_cut
[ ] C3 — Smoke test: python -c "from agent.enrichment.crunchbase_enricher import

enrich; print(enrich('Stripe'))" → valid JSON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP D — ENRICHMENT PIPELINE (~1.5 hours)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] D1 — agent/enrichment/job_post_scraper.py

Playwright-based scraper for company careers pages Returns: open_roles_today (int), role_titles (list), sources (list) Graceful fallback: if scrape fails → returns {"status": "no_data", ...}
[ ] D2 — agent/enrichment/ai_maturity_scorer.py

Uses Qwen3 via OpenRouter to score AI maturity 0-3 Input: company name + job titles + available public context Output: score (int), confidence (float), justifications (list of signal objects) MUST follow the hiring_signal_brief.schema.json signal enum exactly
[ ] D3 — agent/enrichment/competitor_gap_builder.py

Uses Qwen3 to identify 5-10 sector competitors from Crunchbase CSV Scores each competitor's AI maturity Computes gap finding Output: competitor_gap_brief.json matching schemas/competitor_gap_brief.schema.json
[ ] D4 — agent/enrichment/pipeline.py

Main orchestrator: runs C1, C2, D1, D2, D3 in sequence Validates outputs against schemas Returns: hiring_signal_brief (validated), competitor_gap_brief (validated) All steps traced to Langfuse with trace_id
[ ] D5 — Smoke test: python -m agent.enrichment.pipeline --company "Stripe"

→ produces hiring_signal_brief.json and competitor_gap_brief.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP E — AGENT CORE (~1 hour)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] E1 — agent/agent_core.py

Main LLM agent using Qwen3 via OpenRouter Takes: hiring_signal_brief + competitor_gap_brief + conversation_history Returns: composed email text + ICP segment + bench_match_result
[ ] E2 — Implement honesty constraint:

IF hiring_velocity.velocity_label == "insufficient_signal" → use "ask" language, NOT "assert" language IF segment_confidence < 0.6 → use generic exploratory email (abstain pitch) IF bench_to_brief_match.bench_available == False → flag gap, route to human, do NOT commit capacity
[ ] E3 — Implement segment-to-email-template routing:

segment_1 → seed/email_sequences/cold.md (funding-angle) segment_2 → cold.md (restructuring-angle) segment_3 → cold.md (leadership-transition-angle) segment_4 → cold.md (capability-gap-angle, ONLY if ai_maturity >= 2) abstain → generic exploratory email
[ ] E4 — System prompt must embed:

- style_guide.md tone markers (Direct, Grounded, Honest, Professional, Humility) - bench_summary.json capacity data - The honesty constraints above
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP F — INTEGRATION HANDLERS (~1.5 hours)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[x] F1 — agent/email_handler.py

send_email(to, subject, body, trace_id) via Resend API KILL_SWITCH check: if KILL_SWITCH_LIVE_OUTBOUND != "true" → route to OUTBOUND_SINK_EMAIL instead handle_reply_webhook(payload) → extract reply text, thread_id
[x] F2 — agent/sms_handler.py

send_sms(to, message, trace_id) via Africa's Talking sandbox API KILL_SWITCH check: route to OUTBOUND_SINK_SMS if not live handle_inbound_webhook(payload) → extract sender, message text
[x] F3 — agent/hubspot_handler.py

create_or_update_contact(contact_data, trace_id) → HubSpot contact ID log_email_activity(contact_id, email_data) log_meeting_booked(contact_id, cal_event_data) Required fields in contact: firstname, lastname, email, company, hs_lead_status, industry, crunchbase_id, ai_maturity_score, icp_segment, enrichment_timestamp, hiring_signal_brief (JSON string)
[x] F4 — agent/calcom_handler.py

get_available_slots(days_ahead=7) → list of available slots book_slot(slot_datetime, attendee_email, attendee_name, discovery_call_context_brief) → booking confirmation Attaches the discovery_call_context_brief as notes on the booking
[x] F5 — Add Langfuse tracing to ALL handlers:

Every function call must emit a Langfuse span with: trace_id, input, output, latency_ms, model (if LLM), cost_usd (if LLM)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP G — FASTAPI APP (~30 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[x] G1 — agent/main.py (FastAPI)

POST /leads/process → full pipeline: enrich → agent → email → HubSpot POST /email/webhook → handle Resend reply POST /sms/webhook → handle Africa's Talking inbound GET /health → returns stack status
[x] G2 — Run a single end-to-end test:

POST /leads/process with body: {"company": "Stripe", "contact_email": "test@sink.com", "contact_name": "Test User"} Verify: Langfuse trace appears, HubSpot contact created, email logged (to sink), no errors
[x] G3 — Run 20 synthetic lead interactions to generate latency data:

Use a batch script: scripts/generate_synthetic_interactions.py Load 20 companies from Crunchbase CSV, POST each to /leads/process Record p50/p95 latency from Langfuse or local timer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP H — τ²-BENCH HARNESS (~1 hour)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[x] H1 — Confirm tau2-bench is cloned and runnable

Location: somewhere on the machine (ask user where they cloned it) Required: tau2_bench_runner can import from it
[ ] H2 — eval/tau2_bench_runner.py

Wraps τ²-Bench retail domain runner Runs 5 trials on the 30-task dev slice Uses model: qwen/qwen3-235b-a22b via OpenRouter, temperature: 0.0 Each task run emits a Langfuse trace Records: pass@1 per trial, mean, Wilson 95% CI, cost_usd, p50/p95 latency
[ ] H3 — eval/score_log.json (generated by runner)

Schema: {"runs": \[{"run_id", "model", "domain", "slice", "n_tasks", "n_trials", "pass_at_1_mean", "ci_lower", "ci_upper", "cost_usd", "wall_clock_p50_s", "wall_clock_p95_s", "timestamp"}\]} Must have at least 2 entries: baseline run + reproduction check
[ ] H4 — eval/trace_log.jsonl (generated by runner)

One JSON object per line, one per τ²-Bench task run Required fields: trace_id, task_id, trial, passed (bool), turns (int), cost_usd, duration_s, model, domain
[ ] H5 — Run the harness, verify numbers appear in score_log.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP I — DOCUMENTATION (~45 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] I1 — baseline.md (max 400 words)

Sections: what was reproduced, CI result, cost per run, wall-clock p50/p95, unexpected behavior observed
[ ] I2 — README.md at repo root

Sections: Architecture (ASCII diagram), Stack components + status, Setup instructions (pip install + .env + Docker), Kill-switch documentation, How to run (uvicorn command), How to run τ²-Bench harness
[ ] I3 — .gitignore (must exclude .env, _pycache_, *.pyc)

[ ] I4 — Push everything to GitHub

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP J — PDF INTERIM REPORT (~30 min)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ ] J1 — Write interim_report.md covering all required PDF sections:

1\. Architecture overview + design decisions 2\. Stack status (all 6 components: Resend, Africa's Talking, HubSpot, Cal.com, Langfuse, τ²-Bench) 3\. Enrichment pipeline status (all 5 signals producing output) 4\. Competitor gap brief status (at least 1 test prospect) 5\. τ²-Bench baseline score + methodology 6\. p50/p95 latency from 20 interactions 7\. What's working, what's not, plan for remaining days
[ ] J2 — Convert to PDF (use pandoc or any PDF writer)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOTAL ESTIMATED: ~7-8 hours

CRITICAL PATH: A → B → C → D → E → F → G → H → I → J

