"""
HubSpot CRM handler (F3) — HubSpot API v3 via httpx.

create_or_update_contact: upsert by email, return contact ID.
log_email_activity: create a Note engagement linked to the contact.
log_meeting_booked: create a Meeting engagement linked to the contact.

Custom contact properties (crunchbase_id, ai_maturity_score, icp_segment,
enrichment_timestamp, hiring_signal_brief) must be pre-created in the HubSpot
portal under Contacts > Properties before they will persist on the record.
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
