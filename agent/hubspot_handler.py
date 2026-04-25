"""
HubSpot CRM handler (F3) — HubSpot API v3 via httpx.

create_or_update_contact: upsert by email, return contact ID.
log_email_activity: create a Note engagement linked to the contact.
log_meeting_booked: create a Meeting engagement linked to the contact.
update_sequence_step: patch outreach sequence state on a contact.

Custom contact properties that MUST be pre-created in HubSpot portal
(Settings → Properties → Create property, type: Single-line text):
  - crunchbase_id
  - ai_maturity_score
  - icp_segment
  - enrichment_timestamp
  - hiring_signal_brief
  - tenacious_status          (always "draft" — Rule 6 compliance)
  - outreach_sequence_step    (values: 0,1,2,3,reengage_1,reengage_2,reengage_3,hard_no)
  - outreach_last_sent_at     (ISO 8601 timestamp)

Standard properties (firstname, lastname, email, company, hs_lead_status,
industry) work without any setup.
"""
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from agent.utils import HUBSPOT_ACCESS_TOKEN, HUBSPOT_BASE_URL, emit_span

_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


# ── internal helpers ──────────────────────────────────────────────────────────

async def _search_contact_by_email(client: httpx.AsyncClient, email: str) -> str | None:
    """Return existing contact ID for email, or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email"],
        "limit": 1,
    }
    r = await client.post(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search",
        headers=_HEADERS,
        json=payload,
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0]["id"]
    return None


def _build_contact_properties(contact_data: dict) -> dict[str, Any]:
    """Map contact_data keys to HubSpot property names."""
    hiring_brief_raw = contact_data.get("hiring_signal_brief", {})
    hiring_brief_str = (
        json.dumps(hiring_brief_raw)
        if isinstance(hiring_brief_raw, dict)
        else str(hiring_brief_raw)
    )
    return {
        "firstname": contact_data.get("firstname", ""),
        "lastname": contact_data.get("lastname", ""),
        "email": contact_data.get("email", ""),
        "company": contact_data.get("company", ""),
        "hs_lead_status": contact_data.get("hs_lead_status", "NEW"),
        "industry": contact_data.get("industry", ""),
        # custom properties — pre-created in HubSpot portal required
        "crunchbase_id": str(contact_data.get("crunchbase_id", "")),
        "ai_maturity_score": str(contact_data.get("ai_maturity_score", "")),
        "icp_segment": str(contact_data.get("icp_segment", "")),
        "enrichment_timestamp": contact_data.get(
            "enrichment_timestamp", datetime.now(timezone.utc).isoformat()
        ),
        "hiring_signal_brief": hiring_brief_str,
        # Rule 6: all Tenacious-branded output must be marked draft
        "tenacious_status": "draft",
    }


# ── public API ────────────────────────────────────────────────────────────────

async def create_or_update_contact(contact_data: dict, trace_id: str) -> str:
    """
    Upsert a HubSpot contact by email address.

    Returns:
        HubSpot contact ID string.
    """
    t0 = time.monotonic()
    contact_id = "unknown"
    status = "created"

    async with httpx.AsyncClient(timeout=15) as client:
        existing_id = await _search_contact_by_email(
            client, contact_data.get("email", "")
        )
        properties = _build_contact_properties(contact_data)

        if existing_id:
            # update existing
            r = await client.patch(
                f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{existing_id}",
                headers=_HEADERS,
                json={"properties": properties},
            )
            contact_id = existing_id
            status = "updated"
        else:
            # create new
            r = await client.post(
                f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts",
                headers=_HEADERS,
                json={"properties": properties},
            )
            if r.status_code in (200, 201):
                contact_id = r.json().get("id", "unknown")

    latency_ms = (time.monotonic() - t0) * 1000
    result = {"contact_id": contact_id, "status": status}

    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.create_or_update_contact",
        input={"email": contact_data.get("email"), "company": contact_data.get("company")},
        output=result,
        latency_ms=latency_ms,
    )
    return contact_id


async def log_email_activity(
    contact_id: str,
    email_data: dict,
    trace_id: str = "",
) -> dict:
    """
    Log a sent email as a HubSpot Note engagement linked to the contact.

    email_data expected keys: subject, body, to, timestamp, resend_id
    """
    t0 = time.monotonic()
    note_body = (
        f"[Outreach Email]\n"
        f"To: {email_data.get('to', '')}\n"
        f"Subject: {email_data.get('subject', '')}\n"
        f"Resend ID: {email_data.get('resend_id', '')}\n\n"
        f"{email_data.get('body', '')}"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        # create note
        r = await client.post(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes",
            headers=_HEADERS,
            json={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": email_data.get(
                        "timestamp", datetime.now(timezone.utc).isoformat()
                    ),
                }
            },
        )
        note_id = r.json().get("id", "") if r.status_code in (200, 201) else ""

        # associate note -> contact
        if note_id and contact_id:
            await client.put(
                f"{HUBSPOT_BASE_URL}/crm/v3/associations/notes/{note_id}/contacts/{contact_id}/note_to_contact",
                headers=_HEADERS,
            )

    latency_ms = (time.monotonic() - t0) * 1000
    result = {"note_id": note_id, "contact_id": contact_id, "status": "logged"}

    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.log_email_activity",
        input={"contact_id": contact_id, "subject": email_data.get("subject")},
        output=result,
        latency_ms=latency_ms,
    )
    return result


async def log_meeting_booked(
    contact_id: str,
    cal_event_data: dict,
    trace_id: str = "",
) -> dict:
    """
    Log a booked Cal.com meeting as a HubSpot Meeting engagement.

    cal_event_data expected keys: uid, start_time, end_time, attendee_name,
    attendee_email, title, notes
    """
    t0 = time.monotonic()
    start_iso = cal_event_data.get("start_time", datetime.now(timezone.utc).isoformat())

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/meetings",
            headers=_HEADERS,
            json={
                "properties": {
                    "hs_meeting_title": cal_event_data.get("title", "Discovery Call"),
                    "hs_meeting_body": cal_event_data.get("notes", ""),
                    "hs_meeting_start_time": start_iso,
                    "hs_meeting_end_time": cal_event_data.get("end_time", start_iso),
                    "hs_meeting_outcome": "SCHEDULED",
                }
            },
        )
        meeting_id = r.json().get("id", "") if r.status_code in (200, 201) else ""

        # associate meeting -> contact
        if meeting_id and contact_id:
            await client.put(
                f"{HUBSPOT_BASE_URL}/crm/v3/associations/meetings/{meeting_id}/contacts/{contact_id}/meeting_to_contact",
                headers=_HEADERS,
            )

    latency_ms = (time.monotonic() - t0) * 1000
    result = {"meeting_id": meeting_id, "contact_id": contact_id, "status": "logged"}

    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.log_meeting_booked",
        input={"contact_id": contact_id, "start_time": start_iso},
        output=result,
        latency_ms=latency_ms,
    )
    return result


async def update_sequence_step(
    contact_id: str,
    step: str,
    sent_at: str,
    trace_id: str = "",
) -> dict:
    """
    Patch outreach_sequence_step and outreach_last_sent_at on an existing contact.

    step values: "0", "1", "2", "3", "reengage_1", "reengage_2", "reengage_3", "hard_no"
    sent_at: ISO 8601 timestamp string.

    Returns: {contact_id, step, sent_at, status}
    """
    t0 = time.monotonic()
    status = "ok"

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            headers=_HEADERS,
            json={
                "properties": {
                    "outreach_sequence_step": step,
                    "outreach_last_sent_at": sent_at,
                }
            },
        )
        if r.status_code not in (200, 201, 204):
            status = f"error_{r.status_code}"

    latency_ms = (time.monotonic() - t0) * 1000
    result = {"contact_id": contact_id, "step": step, "sent_at": sent_at, "status": status}

    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.update_sequence_step",
        input={"contact_id": contact_id, "new_step": step, "sent_at": sent_at},
        output=result,
        latency_ms=latency_ms,
    )
    return result


async def get_sequence_state(contact_id: str, trace_id: str = "") -> dict:
    """
    Fetch outreach_sequence_step and outreach_last_sent_at for a contact.

    Returns: {outreach_sequence_step, outreach_last_sent_at}
    Missing/null values default to "0" and "" respectively.
    """
    t0 = time.monotonic()
    state = {"outreach_sequence_step": "0", "outreach_last_sent_at": ""}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            headers=_HEADERS,
            params={"properties": "outreach_sequence_step,outreach_last_sent_at"},
        )
        if r.status_code == 200:
            props = r.json().get("properties", {})
            state["outreach_sequence_step"] = props.get("outreach_sequence_step") or "0"
            state["outreach_last_sent_at"] = props.get("outreach_last_sent_at") or ""

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.get_sequence_state",
        input={"contact_id": contact_id},
        output=state,
        latency_ms=latency_ms,
    )
    return state


async def get_contact_by_email(email: str, trace_id: str = "") -> dict:
    """
    Fetch contact properties by email address.

    Returns:
        {contact_id, firstname, lastname, company, icp_segment,
         outreach_sequence_step, hiring_signal_brief (parsed dict)}
        Returns {} if the contact is not found.
    """
    t0 = time.monotonic()
    result: dict = {}

    async with httpx.AsyncClient(timeout=15) as client:
        existing_id = await _search_contact_by_email(client, email)
        if not existing_id:
            emit_span(
                trace_id=trace_id,
                name="hubspot_handler.get_contact_by_email",
                input={"email": email},
                output={"found": False},
                latency_ms=(time.monotonic() - t0) * 1000,
            )
            return {}

        props = "email,firstname,lastname,company,icp_segment,outreach_sequence_step,hiring_signal_brief,hs_lead_status"
        r = await client.get(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{existing_id}",
            headers=_HEADERS,
            params={"properties": props},
        )
        if r.status_code == 200:
            p = r.json().get("properties", {})
            hiring_brief_raw = p.get("hiring_signal_brief") or "{}"
            try:
                hiring_brief_parsed = json.loads(hiring_brief_raw)
            except Exception:
                hiring_brief_parsed = {}
            result = {
                "id": existing_id,
                "contact_id": existing_id,
                # email is returned so callers can verify from_email == contact email
                "email": p.get("email") or email,
                "firstname": p.get("firstname") or "",
                "lastname": p.get("lastname") or "",
                "company": p.get("company") or "",
                "icp_segment": p.get("icp_segment") or "",
                "outreach_sequence_step": p.get("outreach_sequence_step") or "0",
                "hs_lead_status": p.get("hs_lead_status") or "",
                "hiring_signal_brief": hiring_brief_parsed,
                # properties sub-dict for check_reengagement_eligible compatibility
                "properties": p,
            }

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.get_contact_by_email",
        input={"email": email},
        output={"found": bool(result), "contact_id": result.get("contact_id", "")},
        latency_ms=latency_ms,
    )
    return result


async def get_contact_thread_context(contact_id: str, max_notes: int = 5, trace_id: str = "") -> str:
    """
    Fetch the most recent HubSpot notes for a single contact and return them
    as a plain-text thread context string.

    Thread state is keyed by contact_id (email address), NEVER by company domain.
    Two contacts at the same company always have fully independent thread state.

    Returns:
        A newline-separated string of note bodies (oldest first), or "" if none.
    """
    t0 = time.monotonic()
    notes: list[str] = []

    async with httpx.AsyncClient(timeout=15) as client:
        # Fetch note associations for this contact
        r = await client.get(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}/associations/notes",
            headers=_HEADERS,
            params={"limit": max_notes},
        )
        if r.status_code != 200:
            return ""

        note_ids = [item["id"] for item in r.json().get("results", [])]
        # Fetch body for each note
        for note_id in note_ids[:max_notes]:
            nr = await client.get(
                f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes/{note_id}",
                headers=_HEADERS,
                params={"properties": "hs_note_body,hs_timestamp"},
            )
            if nr.status_code == 200:
                body = nr.json().get("properties", {}).get("hs_note_body", "")
                if body:
                    notes.append(body)

    thread_context = "\n---\n".join(notes)
    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.get_contact_thread_context",
        input={"contact_id": contact_id, "max_notes": max_notes},
        output={"note_count": len(notes)},
        latency_ms=latency_ms,
    )
    return thread_context


_ALLOWED_STATUSES = frozenset({
    "NEW", "IN_PROGRESS", "REPLIED", "SCHEDULED",
    "OPTED_OUT", "DISQUALIFIED", "CLOSED", "STALLED",
})


async def get_lead_status(contact_id: str, trace_id: str = "") -> str:
    """
    Fetch the current hs_lead_status for a contact.

    Returns the status string, or "" if the contact is not found.
    """
    t0 = time.monotonic()
    status = ""

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            headers=_HEADERS,
            params={"properties": "hs_lead_status"},
        )
        if r.status_code == 200:
            status = r.json().get("properties", {}).get("hs_lead_status") or ""

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.get_lead_status",
        input={"contact_id": contact_id},
        output={"status": status},
        latency_ms=latency_ms,
    )
    return status


async def update_lead_status(
    contact_id: str,
    new_status: str,
    reason: str = "",
    trace_id: str = "",
) -> dict:
    """
    Patch hs_lead_status on an existing contact, emitting an old→new transition span.

    Allowed values: NEW, IN_PROGRESS, REPLIED, SCHEDULED, OPTED_OUT,
    DISQUALIFIED, CLOSED, STALLED.

    Returns: {contact_id, old_status, new_status, reason, http_status}
    """
    new_status_upper = new_status.upper()
    if new_status_upper not in _ALLOWED_STATUSES:
        raise ValueError(
            f"update_lead_status: '{new_status}' is not an allowed hs_lead_status. "
            f"Allowed: {sorted(_ALLOWED_STATUSES)}"
        )

    t0 = time.monotonic()
    old_status = await get_lead_status(contact_id, trace_id=trace_id)
    http_status = "ok"

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            headers=_HEADERS,
            json={"properties": {"hs_lead_status": new_status_upper}},
        )
        if r.status_code not in (200, 201, 204):
            http_status = f"error_{r.status_code}"

    latency_ms = (time.monotonic() - t0) * 1000
    result = {
        "contact_id": contact_id,
        "old_status": old_status,
        "new_status": new_status_upper,
        "reason": reason,
        "http_status": http_status,
    }

    emit_span(
        trace_id=trace_id,
        name="hubspot_handler.update_lead_status",
        input={
            "contact_id": contact_id,
            "old_status": old_status,
            "new_status": new_status_upper,
            "reason": reason,
        },
        output=result,
        latency_ms=latency_ms,
    )
    return result
