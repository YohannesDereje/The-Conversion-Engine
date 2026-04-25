"""
FastAPI application (G1) — Conversion Engine orchestrator.

Endpoints:
  POST /leads/process   — full pipeline: enrich -> agent -> email -> HubSpot
  POST /leads/reengage  — re-engagement sequence for STALLED contacts
  POST /email/webhook   — Resend inbound reply handler
  POST /sms/webhook     — Africa's Talking inbound SMS handler
  GET  /health          — stack status
"""
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from pydantic import BaseModel

from agent.agent_core import (
    compose_closing_email_3,
    compose_followup_email_2,
    compose_outreach,
)
from agent.email_handler import send_email
from agent.enrichment.pipeline import run_enrichment_pipeline
from agent.hubspot_handler import (
    create_or_update_contact,
    get_contact_by_email,
    get_contact_thread_context,
    get_lead_status,
    get_sequence_state,
    log_email_activity,
    log_meeting_booked,
    update_lead_status,
    update_sequence_step,
)
from agent.reengagement_composer import (
    check_reengagement_eligible,
    compose_reengagement_email_1,
    compose_reengagement_email_2,
    compose_reengagement_email_3,
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


class FollowupRequest(BaseModel):
    contact_id: str
    company_name: str
    contact_email: str
    contact_name: str
    original_subject: str = ""


# ── helpers ────────────────────────────────────────────────────────────────────

def _days_since_iso(iso_str: str) -> int:
    """Return days since an ISO 8601 timestamp. Returns 999 if unparseable (treat as old enough)."""
    if not iso_str:
        return 999
    try:
        from datetime import timezone as _tz
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        return (datetime.now(_tz.utc) - ts).days
    except Exception:
        return 999


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
        "segment_confidence": hiring_brief.get("segment_confidence"),
        "decision_override": agent_result["decision_override"],
        "bench_match": agent_result["bench_match_result"],
        "email_status": email_result.get("status"),
        "email_routed_to": email_result.get("to"),
        "tone_probe_result": agent_result.get("tone_probe_result"),
        "honesty_flags": agent_result.get("honesty_flags", []),
        "hiring_signal_brief": {
            "velocity_label": (hiring_brief.get("hiring_velocity") or {}).get("velocity_label"),
            "signal_confidence": (hiring_brief.get("hiring_velocity") or {}).get("signal_confidence"),
            "open_roles": (hiring_brief.get("hiring_velocity") or {}).get("open_roles"),
            "funding_event": hiring_brief.get("funding_event"),
            "layoff_event": hiring_brief.get("layoff_event"),
            "leadership_change": hiring_brief.get("leadership_change"),
            "ai_maturity": hiring_brief.get("ai_maturity"),
        },
        "competitor_gap_brief": competitor_brief,
    }


@app.post("/leads/followup")
async def followup_lead(req: FollowupRequest):
    """
    Advance the cold sequence for an existing contact.

    Routing:
      step=0  → error: email_1_not_sent_yet
      step=1, days≥5  → send Email 2, update step=2
      step=1, days<5  → error: too_soon
      step=2, days≥7  → send Email 3, update step=3, set hs_lead_status=CLOSED
      step=2, days<7  → error: too_soon
      step=3  → sequence_complete
      other   → error: invalid_state
    """
    # ── 1. Fetch sequence state from HubSpot ─────────────────────────────────
    state = await get_sequence_state(req.contact_id)
    step = state["outreach_sequence_step"]
    last_sent_iso = state["outreach_last_sent_at"]

    # ── 2. Compute days since last email ─────────────────────────────────────
    days_since = _days_since_iso(last_sent_iso)

    # ── 3. Terminal / early-exit states ──────────────────────────────────────
    if step == "0":
        return {"status": "error", "detail": "email_1_not_sent_yet"}
    if step == "3":
        return {"status": "sequence_complete"}
    if step not in ("1", "2"):
        return {"status": "error", "detail": f"invalid_state_for_followup: {step}"}

    # ── 4. Re-enrich for fresh signals ────────────────────────────────────────
    hiring_brief, competitor_brief = await run_enrichment_pipeline(req.company_name)
    trace_id: str = hiring_brief.get("langfuse_trace_id", "")
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 5a. Email 2 (day 5) ───────────────────────────────────────────────────
    if step == "1":
        if days_since < 5:
            return {"status": "error", "detail": f"too_soon: {days_since} days since Email 1 (need 5)"}

        composed = await compose_followup_email_2(
            hiring_brief, competitor_brief, req.original_subject, trace_id
        )
        email_result = await send_email(
            to=req.contact_email,
            subject=composed["subject"],
            body=composed["body"],
            trace_id=trace_id,
        )
        await log_email_activity(
            contact_id=req.contact_id,
            email_data={
                "to": req.contact_email,
                "subject": composed["subject"],
                "body": composed["body"],
                "resend_id": email_result.get("id", ""),
                "timestamp": now_iso,
            },
            trace_id=trace_id,
        )
        await update_sequence_step(req.contact_id, "2", now_iso, trace_id)

        return {
            "status": "ok",
            "step_sent": "2",
            "email_routed_to": email_result.get("to"),
            "subject": composed["subject"],
            "word_count": composed["word_count"],
            "honesty_flags": composed["honesty_flags"],
            "tone_probe_result": composed["tone_probe_result"],
        }

    # ── 5b. Email 3 (day 12) ─────────────────────────────────────────────────
    if step == "2":
        if days_since < 7:
            return {"status": "error", "detail": f"too_soon: {days_since} days since Email 2 (need 7)"}

        composed = await compose_closing_email_3(
            hiring_brief, req.original_subject, req.contact_name, trace_id
        )
        email_result = await send_email(
            to=req.contact_email,
            subject=composed["subject"],
            body=composed["body"],
            trace_id=trace_id,
        )
        await log_email_activity(
            contact_id=req.contact_id,
            email_data={
                "to": req.contact_email,
                "subject": composed["subject"],
                "body": composed["body"],
                "resend_id": email_result.get("id", ""),
                "timestamp": now_iso,
            },
            trace_id=trace_id,
        )
        await update_sequence_step(req.contact_id, "3", now_iso, trace_id)
        await update_lead_status(req.contact_id, "CLOSED", reason="Email 3 (gracious close) sent", trace_id=trace_id)

        return {
            "status": "ok",
            "step_sent": "3",
            "email_routed_to": email_result.get("to"),
            "subject": composed["subject"],
            "word_count": composed["word_count"],
            "honesty_flags": composed["honesty_flags"],
            "tone_probe_result": composed["tone_probe_result"],
        }


@app.post("/email/webhook")
async def email_webhook(request: Request):
    """
    Receive Resend inbound/reply webhook and run the full warm reply pipeline.

    Pipeline (P3-F5):
      1. Parse webhook → reply_text, from_email, original_subject
      2. Look up HubSpot contact by from_email
      3. Classify reply (P3-F1)
      4. Detect handoff triggers (P3-F3)
      5. Route:
         hard_no   → handle_hard_no, no reply
         ambiguous → handle_ambiguous_reply, no reply
         handoff   → compose_handoff_message, send, update HubSpot REPLIED
         engaged   → compose_engaged_reply + Cal slots, send
         curious   → compose_curious_reply + Cal slots, send
         soft_defer→ compose_soft_defer_reply, send, update HubSpot STALLED
         objection → compose_objection_reply + Cal slots, send
      6. Log email activity to HubSpot, update hs_lead_status=REPLIED
    """
    from agent.reply_classifier import classify_reply
    from agent.reply_composer import (
        compose_curious_reply,
        compose_engaged_reply,
        compose_handoff_message,
        compose_objection_reply,
        compose_soft_defer_reply,
        detect_handoff_triggers,
        handle_ambiguous_reply,
        handle_hard_no,
    )
    from agent.calcom_handler import get_available_slots

    # ── 1. Parse webhook ──────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid_json"}

    data = payload.get("data", payload)
    event_type = payload.get("type", "email.reply")

    # Bounce events: still parse but don't run warm pipeline
    if any(k in event_type for k in ("bounced", "delayed", "complained", "delivery")):
        return {
            "status": "bounce_event",
            "event_type": event_type,
            "recipient": data.get("to") or data.get("email", ""),
        }

    headers = data.get("headers", {})
    reply_text: str = data.get("text") or data.get("html") or ""
    from_email: str = data.get("from", "")
    original_subject: str = (
        data.get("subject")
        or headers.get("Subject")
        or headers.get("subject")
        or ""
    )
    # Strip "Re: Re: ..." chains down to the first subject
    original_subject = original_subject.lstrip()

    if not reply_text or not from_email:
        return {"status": "error", "detail": "missing_reply_text_or_from_email"}

    # ── 2. Look up HubSpot contact (keyed by from_email, NOT company domain) ──
    # Multi-thread safeguard (P3-J1): contact lookup and all downstream thread
    # state is scoped to the exact from_email address. Two contacts at the same
    # company domain are always treated as independent threads.
    contact_info = await get_contact_by_email(from_email)
    contact_id: str = contact_info.get("contact_id", "")

    # Verify the HubSpot contact's email matches from_email exactly.
    # This prevents a domain-lookup collision from mixing two threads.
    if contact_id and contact_info.get("email", "").lower() != from_email.lower():
        contact_id = ""
        contact_info = {}

    contact_name: str = (
        f"{contact_info.get('firstname', '')} {contact_info.get('lastname', '')}".strip()
    )
    hiring_brief: dict = contact_info.get("hiring_signal_brief", {})
    competitor_brief: dict = {}  # not cached; use empty dict — grounded in hiring_brief only

    # Use a fresh trace_id for this warm reply span group
    from agent.utils import get_langfuse
    lf = get_langfuse()
    trace_id: str = lf.create_trace_id()

    # Fetch thread context scoped exclusively to this contact_id.
    # Never fetch notes by company domain — that would mix threads.
    thread_context: str = ""
    if contact_id:
        thread_context = await get_contact_thread_context(contact_id, max_notes=5, trace_id=trace_id)

    # ── 3. Classify reply ─────────────────────────────────────────────────────
    classification = await classify_reply(reply_text, thread_context=thread_context, trace_id=trace_id)
    reply_class: str = classification["class"]

    # ── 4. Detect handoff triggers (only relevant for warm classes) ───────────
    handoff = {"handoff": False, "trigger": "", "reason": ""}
    if reply_class in ("engaged", "curious", "objection"):
        handoff = detect_handoff_triggers(reply_text, contact_info)

    # ── 5. Route ──────────────────────────────────────────────────────────────

    # hard_no — no email, mark opted-out
    if reply_class == "hard_no":
        result = await handle_hard_no(contact_id, from_email, reply_text, trace_id)
        return {
            "status": "handled",
            "reply_class": reply_class,
            "action": result["action"],
            "contact_id": contact_id,
        }

    # ambiguous — no email, route to human
    if reply_class == "ambiguous":
        result = await handle_ambiguous_reply(contact_id, reply_text, trace_id)
        return {
            "status": "routed_to_human",
            "reply_class": reply_class,
            "action": result["action"],
            "contact_id": contact_id,
        }

    # handoff — fixed template + update HubSpot
    if handoff["handoff"]:
        composed = compose_handoff_message(contact_name, original_subject)
        email_result = await send_email(
            to=from_email,
            subject=composed["subject"],
            body=composed["body"],
            trace_id=trace_id,
        )
        if contact_id:
            await update_lead_status(contact_id, "REPLIED", reason="engaged/curious/objection reply sent", trace_id=trace_id)
            await log_email_activity(
                contact_id=contact_id,
                email_data={
                    "to": from_email,
                    "subject": composed["subject"],
                    "body": composed["body"],
                    "resend_id": email_result.get("id", ""),
                    "timestamp": email_result.get("timestamp", ""),
                },
                trace_id=trace_id,
            )
        return {
            "status": "ok",
            "reply_class": reply_class,
            "action": "handoff_sent",
            "handoff_triggered": True,
            "handoff_trigger": handoff["trigger"],
            "email_routed_to": email_result.get("to"),
            "reply_body": composed["body"],
            "contact_id": contact_id,
        }

    # ── P3-I2: re-enrichment on engaged reply ────────────────────────────────
    # Re-run three enrichers with fresh data and update HubSpot if segment changes.
    segment_update: dict = {}
    if reply_class == "engaged" and contact_info.get("company"):
        company_name: str = contact_info["company"]
        old_segment: str = contact_info.get("icp_segment", "")
        try:
            from agent.enrichment.crunchbase_enricher import enrich as crunchbase_enrich
            from agent.enrichment.job_post_scraper import scrape_job_postings
            from agent.enrichment.layoffs_enricher import check_layoffs
            fresh_crunchbase = crunchbase_enrich(company_name)
            fresh_layoffs = check_layoffs(company_name)
            fresh_jobs = await scrape_job_postings(company_name, trace_id=trace_id)
            # Merge into hiring_brief so compose_engaged_reply sees fresh signals
            hiring_brief = {
                **hiring_brief,
                **fresh_crunchbase,
                "layoffs": fresh_layoffs,
                "job_postings": fresh_jobs,
            }
            # Re-classify segment based on fresh data
            from agent.enrichment.pipeline import _classify_segment  # type: ignore
            new_segment = _classify_segment(hiring_brief)
            if new_segment != old_segment and new_segment:
                if contact_id:
                    async with __import__("httpx").AsyncClient(timeout=15) as _client:
                        from agent.utils import HUBSPOT_ACCESS_TOKEN, HUBSPOT_BASE_URL
                        await _client.patch(
                            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
                            headers={
                                "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
                                "Content-Type": "application/json",
                            },
                            json={"properties": {"icp_segment": new_segment}},
                        )
                from agent.utils import emit_span
                emit_span(
                    trace_id=trace_id,
                    name="main.re_enrichment_segment_update",
                    input={"old_segment": old_segment, "company": company_name},
                    output={"new_segment": new_segment},
                    latency_ms=0.0,
                )
                segment_update = {"old_segment": old_segment, "new_segment": new_segment}
        except Exception:
            pass

    # warm reply — fetch Cal slots for replies that include a booking ask
    cal_slots: list = []
    if reply_class in ("engaged", "curious", "objection"):
        try:
            cal_slots = await get_available_slots(days_ahead=7)
        except Exception:
            cal_slots = []

    # compose the appropriate reply
    composed = {}
    if reply_class == "engaged":
        composed = await compose_engaged_reply(
            hiring_brief, competitor_brief, reply_text,
            contact_name, original_subject, cal_slots, trace_id,
        )
    elif reply_class == "curious":
        composed = await compose_curious_reply(
            hiring_brief, reply_text, contact_name,
            original_subject, cal_slots, trace_id,
        )
    elif reply_class == "soft_defer":
        composed = await compose_soft_defer_reply(
            reply_text, contact_name, original_subject, trace_id,
        )
    elif reply_class == "objection":
        obj_type = classification.get("objection_type") or "other"
        composed = await compose_objection_reply(
            hiring_brief, competitor_brief, reply_text, obj_type,
            contact_name, original_subject, cal_slots, trace_id,
        )
    else:
        # Unknown class — treat as ambiguous
        result = await handle_ambiguous_reply(contact_id, reply_text, trace_id)
        return {"status": "routed_to_human", "reply_class": reply_class, "contact_id": contact_id}

    # ── 6. Send + update HubSpot ──────────────────────────────────────────────
    email_result = await send_email(
        to=from_email,
        subject=composed["subject"],
        body=composed["body"],
        trace_id=trace_id,
    )

    new_status = "STALLED" if reply_class == "soft_defer" else "REPLIED"
    if contact_id:
        await update_lead_status(contact_id, new_status, reason=f"reply classified as {reply_class}", trace_id=trace_id)
        await log_email_activity(
            contact_id=contact_id,
            email_data={
                "to": from_email,
                "subject": composed["subject"],
                "body": composed["body"],
                "resend_id": email_result.get("id", ""),
                "timestamp": email_result.get("timestamp", ""),
            },
            trace_id=trace_id,
        )

    return {
        "status": "ok",
        "reply_class": reply_class,
        "classification_confidence": classification["confidence"],
        "email_routed_to": email_result.get("to"),
        "subject": composed.get("subject"),
        "word_count": composed.get("word_count"),
        "honesty_flags": composed.get("honesty_flags", []),
        "tone_probe_result": composed.get("tone_probe_result"),
        "contact_id": contact_id,
        "langfuse_trace_id": trace_id,
        **({"reengage_month": composed["reengage_month"]} if reply_class == "soft_defer" else {}),
        **({"segment_update": segment_update} if segment_update else {}),
    }


class ReengageRequest(BaseModel):
    contact_id: str
    company_name: str
    contact_email: str
    contact_name: str


@app.post("/leads/reengage")
async def reengage_lead(req: ReengageRequest):
    """
    Re-engagement sequence for STALLED contacts (P3-H2).

    1. check_reengagement_eligible() — returns 404-style if not eligible
    2. Determine which email based on outreach_sequence_step
    3. Email 1: re-run enrichment (job_post_scraper only) for fresh signal
    4. Send via send_email() with kill switch
    5. Update sequence step and HubSpot
    """
    contact = await get_contact_by_email(req.contact_email)
    if not contact:
        return {"status": "not_found", "reason": "contact not found in HubSpot"}

    eligible, reason = check_reengagement_eligible(contact)
    if not eligible:
        return {"status": "not_eligible", "reason": reason}

    props = contact.get("properties", contact)
    seq_step = str(props.get("outreach_sequence_step", "") or "")
    contact_id = contact.get("id", req.contact_id)

    # derive which reengage email to send (1 if never sent, 2 or 3 based on step)
    if "reengage_2" in seq_step:
        reengage_step = 3
    elif "reengage_1" in seq_step:
        reengage_step = 2
    else:
        reengage_step = 1

    from agent.utils import get_langfuse
    lf = get_langfuse()
    trace_id = lf.create_trace_id() if hasattr(lf, "create_trace_id") else ""

    composed: dict = {}
    new_step = f"reengage_{reengage_step}"

    if reengage_step == 1:
        # re-run job scraper for fresh signal
        try:
            from agent.enrichment.job_post_scraper import scrape_job_postings
            fresh_jobs = await scrape_job_postings(req.company_name, trace_id=trace_id)
        except Exception:
            fresh_jobs = {}

        hiring_brief = {
            "company_name": req.company_name,
            "industry": props.get("industry", ""),
            "job_postings": fresh_jobs,
        }
        original_subject = str(props.get("outreach_last_subject", "our earlier conversation") or "our earlier conversation")
        composed = await compose_reengagement_email_1(
            hiring_brief=hiring_brief,
            competitor_brief={},
            original_subject=original_subject,
            trace_id=trace_id,
        )

    elif reengage_step == 2:
        original_topic = str(props.get("outreach_last_subject", "engineering staffing") or "engineering staffing")
        composed = await compose_reengagement_email_2(
            contact_name=req.contact_name,
            original_topic=original_topic,
            trace_id=trace_id,
        )

    else:
        original_topic = str(props.get("outreach_last_subject", "engineering staffing") or "engineering staffing")
        composed = await compose_reengagement_email_3(
            contact_name=req.contact_name,
            original_topic=original_topic,
            trace_id=trace_id,
        )

    email_result = await send_email(
        to=req.contact_email,
        subject=composed["subject"],
        body=composed["body"],
        trace_id=trace_id,
    )

    await update_sequence_step(
        contact_id=contact_id,
        step=new_step,
        trace_id=trace_id,
    )

    # Email 3 closes the thread → mark CLOSED
    if reengage_step == 3:
        await update_lead_status(
            contact_id=contact_id,
            new_status="CLOSED",
            reason="re-engagement email 3 sent — thread parked 180 days",
            trace_id=trace_id,
        )

    return {
        "status": "ok",
        "step_sent": new_step,
        "email_routed_to": email_result.get("to"),
        "subject": composed["subject"],
        "word_count": composed.get("word_count", 0),
        "langfuse_trace_id": trace_id,
    }


@app.post("/sms/webhook")
async def sms_webhook(request: Request):
    """
    Receive Africa's Talking inbound SMS webhook.
    AT sends form-encoded data — parse to dict.
    """
    form_data = await request.form()
    payload = dict(form_data)
    return handle_inbound_webhook(payload)


@app.post("/calcom/webhook")
async def calcom_webhook(request: Request):
    """
    Receive Cal.com booking confirmation webhook.

    State machine transition: any status → SCHEDULED when a booking is confirmed.
    Cal.com sends BOOKING_CREATED event with attendee email.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "detail": "invalid_json"}

    event_type = payload.get("triggerEvent", "")
    if event_type not in ("BOOKING_CREATED", "booking.created"):
        return {"status": "ignored", "event_type": event_type}

    attendee_email = ""
    booking_data = payload.get("payload", payload)
    attendees = booking_data.get("attendees", [])
    if attendees:
        attendee_email = attendees[0].get("email", "")
    if not attendee_email:
        attendee_email = booking_data.get("attendeeEmail", "")

    if not attendee_email:
        return {"status": "error", "detail": "no_attendee_email"}

    contact = await get_contact_by_email(attendee_email)
    contact_id = contact.get("contact_id", "")
    if not contact_id:
        return {"status": "not_found", "attendee_email": attendee_email}

    from agent.utils import get_langfuse
    lf = get_langfuse()
    trace_id = lf.create_trace_id()

    await update_lead_status(
        contact_id, "SCHEDULED",
        reason="Cal.com booking confirmed via webhook",
        trace_id=trace_id,
    )

    # Log meeting on HubSpot contact
    await log_meeting_booked(
        contact_id=contact_id,
        cal_event_data={
            "uid": booking_data.get("uid", ""),
            "start_time": booking_data.get("startTime", ""),
            "end_time": booking_data.get("endTime", ""),
            "attendee_name": attendees[0].get("name", "") if attendees else "",
            "attendee_email": attendee_email,
            "title": booking_data.get("title", "Discovery Call"),
            "notes": booking_data.get("description", ""),
        },
        trace_id=trace_id,
    )

    return {
        "status": "ok",
        "contact_id": contact_id,
        "attendee_email": attendee_email,
        "new_status": "SCHEDULED",
        "langfuse_trace_id": trace_id,
    }


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
