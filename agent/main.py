"""
FastAPI application (G1) — Conversion Engine orchestrator.

Endpoints:
  POST /leads/process  — full pipeline: enrich -> agent -> email -> HubSpot
  POST /email/webhook  — Resend inbound reply handler
  POST /sms/webhook    — Africa's Talking inbound SMS handler
  GET  /health         — stack status
"""
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from pydantic import BaseModel

from agent.agent_core import compose_outreach
from agent.email_handler import handle_reply_webhook, send_email
from agent.enrichment.pipeline import run_enrichment_pipeline
from agent.hubspot_handler import (
    create_or_update_contact,
    log_email_activity,
)
from agent.sms_handler import handle_inbound_webhook
from agent.utils import (
    AT_API_KEY,
    CALCOM_API_KEY,
    HUBSPOT_ACCESS_TOKEN,
    KILL_SWITCH_LIVE,
    RESEND_API_KEY,
)

app = FastAPI(title="Conversion Engine", version="1.0")


# ── request models ─────────────────────────────────────────────────────────────

class LeadRequest(BaseModel):
    company_name: str
    contact_email: str
    contact_name: str


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_subject_and_body(email_text: str) -> tuple[str, str]:
    """
    The LLM returns: <subject line>\n\n<body>.
    Extract them, falling back gracefully if the format differs.
    """
    lines = email_text.splitlines()
    if not lines:
        return "Introduction from Tenacious Consulting", ""

    subject = lines[0].strip()
    # skip blank separator line(s) after subject
    body_start = 1
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    body = "\n".join(lines[body_start:]).strip()

    return subject or "Introduction from Tenacious Consulting", body or email_text


# ── endpoints ──────────────────────────────────────────────────────────────────

@app.post("/leads/process")
async def process_lead(lead: LeadRequest):
    """
    Full pipeline for a single inbound lead:
    1. Enrich (Crunchbase + layoffs + jobs + AI maturity + competitor gap)
    2. Compose personalised outreach via Qwen3
    3. Upsert HubSpot contact with all enrichment data
    4. Send email (kill switch routes to sink if not live)
    5. Log email activity on the HubSpot contact
    """
    # ── step 1: enrichment ────────────────────────────────────────────────────
    hiring_brief, competitor_brief = await run_enrichment_pipeline(lead.company_name)
    trace_id: str = hiring_brief.get("langfuse_trace_id", "")

    # ── step 2: compose outreach ──────────────────────────────────────────────
    agent_result = await compose_outreach(hiring_brief, competitor_brief)
    trace_id = agent_result.get("langfuse_trace_id", trace_id)

    # ── step 3: upsert HubSpot contact ────────────────────────────────────────
    name_parts = lead.contact_name.strip().split(" ", 1)
    firstname = name_parts[0]
    lastname = name_parts[1] if len(name_parts) > 1 else ""

    contact_data = {
        "firstname": firstname,
        "lastname": lastname,
        "email": lead.contact_email,
        "company": lead.company_name,
        "hs_lead_status": "IN_PROGRESS",
        "industry": hiring_brief.get("industry", ""),
        "crunchbase_id": hiring_brief.get("crunchbase_id", ""),
        "ai_maturity_score": str(
            (hiring_brief.get("ai_maturity") or {}).get("score", "")
        ),
        "icp_segment": str(agent_result["icp_segment"]),
        "enrichment_timestamp": datetime.now(timezone.utc).isoformat(),
        "hiring_signal_brief": hiring_brief,
    }
    contact_id = await create_or_update_contact(contact_data, trace_id)

    # ── step 4: send email ────────────────────────────────────────────────────
    subject, body = _extract_subject_and_body(agent_result["email_to_send"])
    email_result = await send_email(
        to=lead.contact_email,
        subject=subject,
        body=body,
        trace_id=trace_id,
    )

    # ── step 5: log activity on HubSpot ──────────────────────────────────────
    await log_email_activity(
        contact_id=contact_id,
        email_data={
            "to": lead.contact_email,
            "subject": subject,
            "body": body,
            "resend_id": email_result.get("id", ""),
            "timestamp": email_result.get("timestamp", ""),
        },
        trace_id=trace_id,
    )

    return {
        "status": "ok",
        "company": lead.company_name,
        "contact_id": contact_id,
        "langfuse_trace_id": trace_id,
        "icp_segment": agent_result["icp_segment"],
        "decision_override": agent_result["decision_override"],
        "bench_match": agent_result["bench_match_result"],
        "email_status": email_result.get("status"),
        "email_routed_to": email_result.get("to"),
    }


@app.post("/email/webhook")
async def email_webhook(request: Request):
    """Receive Resend inbound/reply webhook (JSON body)."""
    payload = await request.json()
    return handle_reply_webhook(payload)


@app.post("/sms/webhook")
async def sms_webhook(request: Request):
    """
    Receive Africa's Talking inbound SMS webhook.
    AT sends form-encoded data — parse to dict.
    """
    form_data = await request.form()
    payload = dict(form_data)
    return handle_inbound_webhook(payload)


@app.get("/health")
async def health():
    """Return stack configuration status and kill switch state."""
    return {
        "status": "ok",
        "kill_switch_live": KILL_SWITCH_LIVE,
        "outbound_mode": "live" if KILL_SWITCH_LIVE else "sink",
        "services": {
            "resend": "configured" if RESEND_API_KEY else "missing_key",
            "africas_talking": "configured" if AT_API_KEY else "missing_key",
            "hubspot": "configured" if HUBSPOT_ACCESS_TOKEN else "missing_key",
            "calcom": "configured" if CALCOM_API_KEY else "missing_key",
        },
    }
