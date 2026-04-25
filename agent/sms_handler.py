"""
SMS handler (F2) — Africa's Talking sandbox API.

SMS is a warm-lead-only channel. Cold outreach is blocked at the handler level.

send_sms             : sends via Africa's Talking (warm leads only, kill-switch routed).
handle_inbound_webhook: parses AT inbound SMS; routes to registered inbound handlers.
register_inbound_handler: attach downstream logic to inbound reply events.
"""
import logging
import time
from datetime import datetime, timezone

import africastalking

from agent.utils import (
    AT_API_KEY,
    AT_SHORTCODE,
    AT_USERNAME,
    KILL_SWITCH_LIVE,
    OUTBOUND_SINK_SMS,
    emit_span,
)

africastalking.initialize(AT_USERNAME, AT_API_KEY)
_sms = africastalking.SMS
_logger = logging.getLogger(__name__)

# ── downstream handler registry ───────────────────────────────────────────────
# External logic attaches inbound reply handlers here.

_inbound_handlers: list = []


def register_inbound_handler(handler) -> None:
    """Register a callable that receives parsed inbound SMS events."""
    _inbound_handlers.append(handler)


# ── send ──────────────────────────────────────────────────────────────────────

async def send_sms(
    to: str,
    message: str,
    trace_id: str,
    is_warm_lead: bool = False,
) -> dict:
    """
    Send an SMS via Africa's Talking.

    Channel hierarchy gate: SMS is restricted to warm leads only.
    Cold outreach MUST use the email channel instead.

    When KILL_SWITCH_LIVE_OUTBOUND is false the message is routed to
    OUTBOUND_SINK_SMS — the real recipient is never contacted.

    Args:
        to:           recipient phone number
        message:      SMS body text
        trace_id:     Langfuse trace ID for span emission
        is_warm_lead: MUST be True to allow sending. Cold outreach is blocked.

    Returns:
        {recipients, to, intended_to, status, timestamp}

    Raises:
        PermissionError: if is_warm_lead is False (cold outreach blocked).
        ValueError:      if message is empty (malformed input guard).
    """
    if not is_warm_lead:
        raise PermissionError(
            "SMS is restricted to warm leads only. "
            "Use the email channel for cold outreach."
        )

    if not message or not message.strip():
        raise ValueError("send_sms: message body is required")

    t0 = time.monotonic()
    actual_to = to if KILL_SWITCH_LIVE else OUTBOUND_SINK_SMS
    status = "sent" if KILL_SWITCH_LIVE else "sink"
    recipients: list = []

    try:
        sender = str(AT_SHORTCODE) if AT_SHORTCODE else None
        response = _sms.send(message, [actual_to], sender_id=sender)
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])
    except Exception as exc:
        status = f"error"
        _logger.error("Africa's Talking send failed to=%s: %s", actual_to, exc)

    latency_ms = (time.monotonic() - t0) * 1000
    result = {
        "recipients": recipients,
        "to": actual_to,
        "intended_to": to,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    emit_span(
        trace_id=trace_id,
        name="sms_handler.send_sms",
        input={
            "to": actual_to,
            "intended_to": to,
            "is_warm_lead": is_warm_lead,
            "kill_switch_live": KILL_SWITCH_LIVE,
        },
        output=result,
        latency_ms=latency_ms,
    )
    return result


# ── scheduling SMS ───────────────────────────────────────────────────────────

async def send_scheduling_sms(
    to: str,
    contact_name: str,
    slot_datetime: str,
    cal_link: str,
    trace_id: str,
) -> dict:
    """
    Send a slot-confirmation SMS to a warm lead after an email reply.

    Template (strict, no variation, must remain under 160 chars):
        "Hi {Name} — Elena at Tenacious. Per our email thread: {slot}?
         Cal confirms at: {cal_link}. Reply N if slot no longer works."

    Only fires for warm leads who have already replied (engaged/curious class)
    and have no confirmed Cal.com booking yet.

    Args:
        to:            recipient phone number
        contact_name:  prospect first name (used in greeting)
        slot_datetime: human-readable slot string, e.g. "Mon Apr 28 @ 3 pm EAT"
        cal_link:      Cal.com booking link
        trace_id:      Langfuse trace ID

    Returns:
        send_sms() result dict plus char_count.

    Raises:
        ValueError: if rendered message exceeds 160 characters.
    """
    first_name = contact_name.strip().split()[0] if contact_name.strip() else contact_name
    message = (
        f"Hi {first_name} — Elena at Tenacious. "
        f"Per our email thread: {slot_datetime}? "
        f"Cal confirms at: {cal_link}. "
        f"Reply N if slot no longer works."
    )
    char_count = len(message)
    if char_count > 160:
        raise ValueError(
            f"send_scheduling_sms: message is {char_count} chars (max 160). "
            f"Shorten slot_datetime or cal_link."
        )

    result = await send_sms(
        to=to,
        message=message,
        trace_id=trace_id,
        is_warm_lead=True,
    )
    result["char_count"] = char_count

    emit_span(
        trace_id=trace_id,
        name="sms_handler.send_scheduling_sms",
        input={
            "to": to,
            "contact_name": contact_name,
            "slot_datetime": slot_datetime,
            "cal_link": cal_link,
            "char_count": char_count,
        },
        output=result,
        latency_ms=0.0,
    )
    return result


# ── inbound webhook ───────────────────────────────────────────────────────────

def handle_inbound_webhook(payload: dict) -> dict:
    """
    Parse an Africa's Talking inbound SMS webhook and route to registered handlers.

    AT POST body fields (form-encoded, parsed to dict by FastAPI):
        from, text, to, date, id, linkId

    Inbound replies are dispatched to all registered inbound handlers so
    downstream logic (e.g. conversation continuity, CRM logging) can act on them.

    Returns:
        {event, from, text, to, timestamp}

    Raises:
        TypeError: if payload is not a dict (malformed input guard).
    """
    if not isinstance(payload, dict):
        raise TypeError(f"handle_inbound_webhook: expected dict, got {type(payload).__name__}")

    result = {
        "event": "inbound_sms",
        "from": payload.get("from", ""),
        "text": payload.get("text", ""),
        "to": payload.get("to", ""),
        "timestamp": payload.get("date", datetime.now(timezone.utc).isoformat()),
    }

    for handler in _inbound_handlers:
        try:
            handler(result)
        except Exception as exc:
            _logger.error("inbound SMS handler %s raised: %s", handler, exc)

    return result
