"""
Cal.com handler (F4) — Cal.com Cloud v2 API (https://api.cal.com/v2/).

get_available_slots: returns available booking slots for the next N days.
book_slot: creates a booking with attendee info and discovery call context notes.
          When contact_id is provided, automatically triggers a HubSpot
          meeting record update (integration link requirement).
"""
import time
from datetime import datetime, timedelta, timezone

import httpx

from agent.utils import (
    CALCOM_API_KEY,
    CALCOM_BASE_URL,
    CALCOM_EVENT_TYPE_ID,
    CALCOM_SDR_EMAIL,
    emit_span,
)

_HEADERS = {
    "Authorization": f"Bearer {CALCOM_API_KEY}",
    "cal-api-version": "2024-09-04",
    "Content-Type": "application/json",
}


async def get_available_slots(days_ahead: int = 7) -> list[dict]:
    """
    Fetch available calendar slots for the next `days_ahead` days.

    Returns:
        List of slot dicts {time, date, eventTypeId}. Returns [] on any API error.
    """
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    start_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    slots: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{CALCOM_BASE_URL}/v2/slots/available",
                headers=_HEADERS,
                params={
                    "startTime": start_time,
                    "endTime": end_time,
                    "eventTypeId": CALCOM_EVENT_TYPE_ID,
                },
            )
            if r.status_code == 200:
                slots_by_date = r.json().get("data", {}).get("slots", {})
                for date_key, day_slots in slots_by_date.items():
                    for slot in day_slots:
                        slots.append({
                            "time": slot.get("time", ""),
                            "date": date_key,
                            "eventTypeId": CALCOM_EVENT_TYPE_ID,
                        })
    except Exception:
        pass

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id="",
        name="calcom_handler.get_available_slots",
        input={"days_ahead": days_ahead, "eventTypeId": CALCOM_EVENT_TYPE_ID},
        output={"slot_count": len(slots)},
        latency_ms=latency_ms,
    )
    return slots


async def book_slot(
    slot_datetime: str,
    attendee_email: str,
    attendee_name: str,
    discovery_call_context_brief: str,
    trace_id: str = "",
    contact_id: str = "",
) -> dict:
    """
    Book a Cal.com slot.

    Attaches `discovery_call_context_brief` as notes on the booking so the SDR
    can see the prospect context before the call.

    When `contact_id` is provided, a completed booking automatically triggers
    a HubSpot meeting record update (CRM/Calendar integration link).

    Args:
        slot_datetime:                ISO 8601 e.g. "2024-01-15T09:00:00Z"
        attendee_email:               prospect's email
        attendee_name:                prospect's full name
        discovery_call_context_brief: context summary string (from agent output)
        trace_id:                     Langfuse trace ID
        contact_id:                   HubSpot contact ID — triggers HubSpot update if set

    Returns:
        {uid, status, start_time, attendee_email, meeting_url} or {status: "error", ...}
    """
    t0 = time.monotonic()

    payload = {
        "eventTypeId": CALCOM_EVENT_TYPE_ID,
        "start": slot_datetime,
        "attendee": {
            "name": attendee_name,
            "email": attendee_email,
            "timeZone": "UTC",
        },
        "metadata": {},
        "bookingFieldsResponses": {
            "notes": discovery_call_context_brief,
        },
    }

    result: dict = {"status": "error", "uid": "", "start_time": slot_datetime}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CALCOM_BASE_URL}/v2/bookings",
                headers=_HEADERS,
                json=payload,
            )
            body = r.json()
            if r.status_code in (200, 201):
                booking = body.get("data", body)
                result = {
                    "uid":            booking.get("uid", ""),
                    "status":         booking.get("status", "accepted"),
                    "start_time":     booking.get("start", slot_datetime),
                    "end_time":       booking.get("end", slot_datetime),
                    "attendee_email": attendee_email,
                    "meeting_url":    booking.get("meetingUrl", ""),
                    "title":          booking.get("title", "Discovery Call"),
                    "notes":          discovery_call_context_brief,
                }
            else:
                result["error_detail"] = body.get("message", str(r.status_code))
    except Exception as exc:
        result["error_detail"] = str(exc)

    latency_ms = (time.monotonic() - t0) * 1000
    emit_span(
        trace_id=trace_id,
        name="calcom_handler.book_slot",
        input={
            "slot_datetime": slot_datetime,
            "attendee_email": attendee_email,
            "eventTypeId": CALCOM_EVENT_TYPE_ID,
        },
        output=result,
        latency_ms=latency_ms,
    )

    # ── Integration link: successful booking → HubSpot meeting record ─────────
    if result.get("uid") and contact_id:
        try:
            from agent.hubspot_handler import log_meeting_booked
            await log_meeting_booked(
                contact_id=contact_id,
                cal_event_data=result,
                trace_id=trace_id,
            )
        except Exception:
            pass  # HubSpot update failure must not fail the booking confirmation

    return result
