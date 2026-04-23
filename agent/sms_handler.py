"""
SMS handler (F2) — Africa's Talking sandbox API.

send_sms: sends via Africa's Talking; routes to sink when kill switch is off.
handle_inbound_webhook: parses Africa's Talking inbound SMS webhook payload.
"""
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


async def send_sms(
    to: str,
    message: str,
    trace_id: str,
) -> dict:
    """
    Send an SMS via Africa's Talking.

    When KILL_SWITCH_LIVE_OUTBOUND is false the message is routed to
    OUTBOUND_SINK_SMS — the real recipient is never contacted.

    Returns:
        {recipients, to, intended_to, status, timestamp}
    """
    t0 = time.monotonic()
    actual_to = to if KILL_SWITCH_LIVE else OUTBOUND_SINK_SMS
    status = "sent" if KILL_SWITCH_LIVE else "sink"
    recipients: list = []

    try:
        sender = str(AT_SHORTCODE) if AT_SHORTCODE else None
        response = _sms.send(message, [actual_to], sender_id=sender)
        recipients = (
            response.get("SMSMessageData", {}).get("Recipients", [])
        )
    except Exception as exc:
        status = f"error:{exc}"

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
            "kill_switch_live": KILL_SWITCH_LIVE,
        },
        output=result,
        latency_ms=latency_ms,
    )
    return result


def handle_inbound_webhook(payload: dict) -> dict:
    """
    Extract sender and message text from an Africa's Talking inbound SMS webhook.

    Africa's Talking POST body (form-encoded, parsed to dict by FastAPI):
        from, text, to, date, id, linkId

    Returns:
        {from, text, to, timestamp}
    """
    return {
        "from": payload.get("from", ""),
        "text": payload.get("text", ""),
        "to": payload.get("to", ""),
        "timestamp": payload.get("date", datetime.now(timezone.utc).isoformat()),
    }
